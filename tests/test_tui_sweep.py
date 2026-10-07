"""Every TUI screen at 80×24 and at 120×40: no action is cut off.

The sweep draws each screen ``_tui_render.GOLDENS`` builds, plus the card
states whose notices carry a remedy (an ignored ``CLAUDE_CONFIG_DIR``, a new
model line, implementers outside a Git repository, a missing sign-in on the
fresh and the resume card), the sign-in screens, the gateway actions and
their log, every Get started step, the needs-a-choice chooser, the profile
editor's forms and the transition and propagation confirmations, at both
sizes. Every text a screen cuts to fit (``views.clip`` and the last-cell
clip of ``tui.safe_add``) is recorded, and an action it names — a key route
such as ``G → L``, a ``claude-multi`` command, ``git init``, a ``/cm`` verb,
``V details`` — must still be on the screen. ``table()`` is the recorded
sweep: one row per screen and size.

:data:`INVENTORY` is the explicit list of every screen: each class that
draws one and each flow that composes its own dialogs. It is checked
against the code (no drawing class or flow is missing from it) and against
the sweep (each one was drawn at both sizes).
"""

from __future__ import annotations

import ast
import contextlib
import copy
import importlib
import inspect
import json
import pkgutil
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock

from claude_multi import catalog, cli, tui, views

import _tui_fixture as fx
import _tui_render as R
from _catalog import FIXTURE_ROOT
from test_tui import ESC, RIGHT, FakeWindow

ENTER = "\n"

SIZES = ((80, 24), (120, 40))

# What a person acts on: a key route, a command, a verb.
ACTION = re.compile(
    r"\b(?:[A-Z]|Enter|Esc|Tab|Space) → (?:[A-Z]\b|Enter|Esc|Space)"
    r"|claude-multi [a-z][a-z-]*(?: [a-z][a-z-]*)?"
    r"|\bgit init\b|\bV details\b|/cm [a-z]+|\bpress [A-Z]\b"
    r"|\bEnter (?:admits|revokes|approves|inspects|declares|links)\b"
)


def _with_a_new_line(root: Path) -> Path:
    """A copy of the fixture assets with one more line, New and off: a copy
    of the first line on a keyed direct provider."""

    assets = root / "assets-new"
    shutil.copytree(FIXTURE_ROOT, assets)
    path = assets / "catalog" / "models.json"
    document = json.loads(path.read_text())
    providers = json.loads((assets / "catalog" / "providers.json").read_text())["providers"]
    source = next(key for key in sorted(document["models"])
                  if providers[document["models"][key]["provider"]]["transport"]["kind"] == "direct")
    line = copy.deepcopy(document["models"][source])
    line.update(status="new", display="Sweep New Line", wire_model="sweep-new-wire")
    if isinstance(line.get("efforts"), dict):
        for spec in line["efforts"].values():
            if "selector" in spec:
                spec["selector"] = "sweep-new-" + spec["selector"]
    if "selector" in line:
        line["selector"] = "sweep-new-" + line["selector"]
    document["models"]["sweep-new"] = line
    path.write_text(json.dumps(document, indent=2) + "\n")
    return assets


def _without_sign_ins(runtime: cli.Runtime) -> None:
    auth = Path(runtime.environ["HOME"]) / runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
    auth.mkdir(parents=True, exist_ok=True)


def card_notices(root: Path) -> str:
    """The card with every notice of its own: an ignored CLAUDE_CONFIG_DIR,
    a new line, implementers outside a Git repository."""

    with R.golden_runtime(root / "run", asset_root=_with_a_new_line(root),
                          environ_extra={"CLAUDE_CONFIG_DIR": "/srv/claude-elsewhere"}) as runtime, \
            mock.patch("claude_multi.compiler.git_work_tree", lambda _cwd: False):
        return R.render(lambda: R.card_screen(runtime, R.profile_target(runtime, catalog.DEFAULT_SEED)))


def card_sign_in(root: Path, *, resume: bool = False) -> str:
    with R.golden_runtime(root) as runtime:
        _without_sign_ins(runtime)
        target = R.profile_target(runtime, catalog.DEFAULT_SEED)
        kwargs: dict[str, Any] = {}
        if resume:
            record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
            prepared = runtime.prepare(cli._record_resume_target(record), action="resume",
                                       passthrough=[], session_id=fx.FIXED_ID)
            target = prepared.target
            kwargs.update(action="resume", prepared=prepared)
        return R.render(lambda: R.card_screen(runtime, target, **kwargs))


def editor_on_a_slot(root: Path) -> str:
    """The profile editor with its first agent row selected (its role)."""

    with R.golden_runtime(root) as runtime:
        return R.render(lambda: tui.ProfileEditorScreen(R.editor_state(runtime), palette=tui.MONO_PALETTE,
                                                        initial_focus=catalog.AGENT_ROLE_IDS[0]))


def named_bindings(root: Path) -> str:
    with R.golden_runtime(root) as runtime:
        state = R.editor_state(runtime)
        key = R.default_lineup(runtime).lead.binding.key
        bindings = {"sweep-binding": {"model": key, "effort": state.cat.lines[key]["default_effort"]}}
        return R.render(lambda: tui.NamedBindingsScreen(state.cat, state.effective, palette=tui.MONO_PALETTE,
                                                        bindings=bindings))


def per_agent(root: Path) -> str:
    """The lineup dialog's per-agent edit of a session on the default seed."""

    from claude_multi.cli.screens import launch_sessions

    with R.golden_runtime(root) as runtime:
        record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        return R.render(lambda: launch_sessions._PerAgentScreen(runtime, record, palette=tui.MONO_PALETTE,
                                                                eff=runtime.current_effective()))


# ------------------------------------------------- screens met along a flow


@contextlib.contextmanager
def _case(cls: type) -> Iterator[Any]:
    """A fixture test case of another module, set up for one builder."""

    case = cls()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def _drive(run: Callable[[FakeWindow], Any], keys: list) -> str:
    """``run`` on a window of the size the sweep draws at, fed ``keys``:
    every frame it draws is swept; the last one is returned."""

    width, height = R.current_size()
    win = FakeWindow(list(keys), height=height, width=width)
    run(win)
    return win.frames[-1] if win.frames else ""


def provider_form(root: Path, *, kind: str) -> str:
    """Draw every step of the mixed-kind endpoint draft without declaring it."""

    from claude_multi.cli.screens import common

    fields = views.provider_form_fields(kind=kind)
    form = common._ProviderForm("new provider", fields, palette=tui.MONO_PALETTE)
    return _drive(form.run, [ENTER] * (len(fields) - 1) + [ESC])


def _account_provider(*, methods: int | None = None) -> tuple[str, str]:
    """``(provider id, pool)`` of the first account provider (derived),
    with a sign-in that opens ``methods`` ways when given."""

    from claude_multi.setup import signin

    return next((pid, pool) for pid, pool in sorted(signin.ACCOUNT_POOLS.items())
                if methods is None or len(signin.methods(pool)) == methods)


def sign_in_account(root: Path, *, signed_in: bool = False) -> str:
    """L on an account provider: who is signed in, sign in (again), sign out."""

    import claude_multi.cli.screens.signin as screens_signin
    import test_screens_providers_lifecycle as lifecycle
    from claude_multi import account_pools

    with _case(lifecycle.AccountTests) as case:
        pid, pool = _account_provider()
        if signed_in:
            case.record(account_pools.pool(pool).record_prefix + "-fixture.person@example.com.json")
        return _drive(lambda win: screens_signin.account_modal(case.runtime, win, tui.MONO_PALETTE, pid), [ESC])


def sign_in_steps(root: Path, *, step: str) -> str:
    """The sign-in in the full-screen UI: its typed acknowledgement, the
    choice of how it opens (a pool that opens two ways) and its result."""

    import claude_multi.cli.screens.signin as screens_signin
    import test_screens_providers_lifecycle as lifecycle
    from claude_multi.setup import model, signin, texts

    with _case(lifecycle.SignInFlowTests) as case, mock.patch.object(signin, "pool_offered", return_value=True):
        typed = [*texts.ACK_WORD, ENTER, ENTER]  # the word, then Continue
        if step in ("acknowledgement", "method"):
            # A sign-in that opens two ways, stopped once it is chosen.
            pid, _pool = _account_provider(methods=2)
            with mock.patch.object(signin, "plan_sign_in", side_effect=model.Refused("stopped before it runs")):
                return _drive(lambda win: screens_signin.run_sign_in_flow(case.runtime, win, tui.MONO_PALETTE, pid),
                              [ESC] if step == "acknowledgement" else [*typed, ESC])
        # The result: a stand-in sign-in that writes the account's record.
        pid, pool = _account_provider(methods=1)

        def runner(_invocation: Any) -> int:
            from claude_multi import account_pools

            case.record(account_pools.pool(pool).record_prefix + "-fixture.person@example.com.json")
            return 0

        case.runtime.signin_runner = runner
        return _drive(lambda win: screens_signin.run_sign_in_flow(case.runtime, win, tui.MONO_PALETTE, pid),
                      [*typed, ENTER])


def sign_out_result(root: Path) -> str:
    """The sign-out's result, after its confirmation."""

    import claude_multi.cli.screens.signin as screens_signin
    import test_screens_providers_lifecycle as lifecycle
    from claude_multi import account_pools
    from claude_multi.setup import signin

    with _case(lifecycle.AccountTests) as case:
        pid, pool = _account_provider()
        name = account_pools.pool(pool).record_prefix + "-fixture.person@example.com.json"
        case.record(name)
        outcome = signin.SignOutOutcome("stopped", (name,), (), "backup",
                                        ("Signed out of fixture.person@example.com",), "reloaded")
        with mock.patch.object(signin, "apply_sign_out", return_value=outcome):
            return _drive(lambda win: screens_signin.sign_out_flow(case.runtime, win, tui.MONO_PALETTE, pid),
                          [RIGHT, ENTER, ENTER])


LONG_LOG_LINE = ("[2026-10-02 12:00:00] [a1b2c3d4] [warn ] [conductor.go:412] the upstream refused the request "
                 "with status 529: overloaded; the request is retried with the next credential of the pool")


def gateway_actions_view(root: Path, *, state: str) -> str:
    """The gateway's actions (H, W): a stopped gateway with the service to
    install, one that a transaction owner holds, and the log tail."""

    import claude_multi.cli.screens.gateway_actions as screens_gateway
    import test_gateway_doctor
    import test_tui_dispatch
    from claude_multi import gateway_inhibition

    with _case(test_gateway_doctor._GatewayDoctor) as case:
        if state == "log":
            case.ready("abababababababab")
            case.log("abababababababab", [LONG_LOG_LINE] * 3)
            title, lines = screens_gateway.log_view(screens_gateway.observe(case.runtime))
            return R.render(lambda: tui.TextView(title, lines, palette=tui.MONO_PALETTE))
        if state == "inhibited":
            gateway_inhibition.begin(case.state_root, owner="installer", purpose="install claude-multi 1.0.0",
                                     phase="replace", remedy="claude-multi gateway service install")
        service = test_tui_dispatch._Service()
        with mock.patch.object(type(case.runtime), "gateway_service", return_value=service), \
                mock.patch.object(tui, "suspended_curses", mock.MagicMock()):
            return _drive(lambda win: screens_gateway.dialog(case.runtime, win, tui.MONO_PALETTE), [ESC])


def get_started_step(root: Path, *, step: str) -> str:
    """Get started on a later step: the list with each step selected, and
    each step's own view (the gateway's log and outbound proxy too)."""

    import test_screens_get_started as get_started_tests
    from claude_multi.setup import external

    with _case(get_started_tests.GetStartedCase) as case, \
            mock.patch.object(external, "ensure_gateway", return_value=None), \
            mock.patch.object(external, "gateway_log_tail", return_value=(LONG_LOG_LINE,)):
        if step == "list":
            last = ""
            for step_id in case.ids():
                last = R.render(lambda step_id=step_id: case.screen(focus=step_id))
            return last
        if step == "test":
            case.apply_and_serve()
        to = case.steps_to(None, "gateway" if step in ("gateway-log", "proxy") else step)
        opened = {"gateway-log": [ENTER, RIGHT, RIGHT, ENTER, ESC, ESC], "proxy": [ENTER, RIGHT, ENTER, ESC],
                  "test": [ENTER, ENTER, "n"]}.get(step, [ENTER, ESC])
        return _drive(lambda win: case.screen().run(win), [*to, *opened, ESC])


def needs_choice(root: Path) -> str:
    """The needs-a-choice chooser of a session whose lead left the catalog."""

    with R.golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, needs_choice=True, ended=True)
        return _drive(lambda win: cli._NeedsChoiceChooser(runtime, [record], palette=tui.MONO_PALETTE).run(win),
                      [ESC])


def editor_form(root: Path, *, form: str) -> str:
    """The profile editor's General and Native forms."""

    with R.golden_runtime(root) as runtime:
        cls = tui._GeneralForm if form == "general" else tui._NativeForm
        return R.render(lambda: cls(R.editor_state(runtime), tui.MONO_PALETTE))


def transition(root: Path, *, confirm: bool = False) -> str:
    """A relaunch's transition screen (the session still running in the
    background) and its exited-confirmation."""

    import claude_multi.cli.launch_flow as launch_flow
    import claude_multi.cli.text as cli_text

    with R.golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")
        target = cli.LaunchTarget("relaunch", None, R.relaunch_seed(runtime), True, f"Relaunch {fx.FIXED_ID[:8]}")
        preview = launch_flow._relaunch_preview(runtime, fx.FIXED_ID, target)
        note = cli_text.TRANSITION_LIVE_NOTE.format(mid=fx.FIXED_ID)
        screen = cli._TransitionScreen(preview, palette=tui.MONO_PALETTE, live_note=note)
        if not confirm:
            return R.render(lambda: screen)
        return _drive(screen.run, [ENTER, ESC, ESC])


def propagation_report(root: Path) -> str:
    """The propagation prompt's L: the report of what each follower got."""

    with R.golden_runtime(root) as runtime:
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True, title="probe fixes")
        screen = cli._PropagationScreen(runtime, tui.MONO_PALETTE, [catalog.DEFAULT_SEED])
        return _drive(screen.run, ["l", ESC])


EXTRA: dict[str, Callable[[Path], str]] = {
    "card-notices": card_notices,
    "card-sign-in": card_sign_in,
    "card-sign-in-resume": lambda root: card_sign_in(root, resume=True),
    "editor-slot": editor_on_a_slot,
    "editor-general": lambda root: editor_form(root, form="general"),
    "editor-native": lambda root: editor_form(root, form="native"),
    "named-bindings": named_bindings,
    "per-agent": per_agent,
    "needs-choice": needs_choice,
    "signin-account": sign_in_account,
    "signin-account-signed-in": lambda root: sign_in_account(root, signed_in=True),
    "signin-acknowledgement": lambda root: sign_in_steps(root, step="acknowledgement"),
    "signin-method": lambda root: sign_in_steps(root, step="method"),
    "signin-result": lambda root: sign_in_steps(root, step="result"),
    "provider-form-anthropic": lambda root: provider_form(root, kind="anthropic-compatible"),
    "provider-form-openai": lambda root: provider_form(root, kind="openai-compatible"),
    "provider-form-lan": lambda root: provider_form(root, kind="openai-compatible-lan"),
    "signout-result": sign_out_result,
    "gateway-actions": lambda root: gateway_actions_view(root, state="stopped"),
    "gateway-actions-inhibited": lambda root: gateway_actions_view(root, state="inhibited"),
    "gateway-log": lambda root: gateway_actions_view(root, state="log"),
    "get-started-steps": lambda root: get_started_step(root, step="list"),
    "get-started-preflight": lambda root: get_started_step(root, step="preflight"),
    "get-started-claude": lambda root: get_started_step(root, step="claude"),
    "get-started-gateway": lambda root: get_started_step(root, step="gateway"),
    "get-started-gateway-log": lambda root: get_started_step(root, step="gateway-log"),
    "get-started-proxy": lambda root: get_started_step(root, step="proxy"),
    "get-started-providers": lambda root: get_started_step(root, step="providers"),
    "get-started-test": lambda root: get_started_step(root, step="test"),
    "get-started-profile": lambda root: get_started_step(root, step="profile"),
    "get-started-check": lambda root: get_started_step(root, step="check"),
    "transition": transition,
    "transition-confirm": lambda root: transition(root, confirm=True),
    "propagation-report": propagation_report,
}


# --------------------------------------------------------------- the inventory

_SCREENS = "claude_multi.cli.screens"
# Every screen a person can see: each class that draws one (``module:Class``)
# and each flow that composes its own dialogs (``module:function`` or
# ``module:Class.method``). The sweep draws every one at both sizes.
INVENTORY: tuple[str, ...] = (
    f"{_SCREENS}.common:_TextReportScreen",
    f"{_SCREENS}.common:_ScrollModal",
    f"{_SCREENS}.common:_ProviderForm",
    f"{_SCREENS}.direct:_DirectScreen",
    f"{_SCREENS}.get_started:_StepView",
    f"{_SCREENS}.get_started:_ProviderPicker",
    f"{_SCREENS}.get_started:_GetStartedScreen",
    f"{_SCREENS}.get_started:_GetStartedScreen._preflight",
    f"{_SCREENS}.get_started:_GetStartedScreen._claude",
    f"{_SCREENS}.get_started:_GetStartedScreen._gateway",
    f"{_SCREENS}.get_started:_GetStartedScreen._proxy",
    f"{_SCREENS}.get_started:_GetStartedScreen._providers",
    f"{_SCREENS}.get_started:_GetStartedScreen._test",
    f"{_SCREENS}.get_started:_GetStartedScreen._profile",
    f"{_SCREENS}.get_started:_GetStartedScreen._check",
    f"{_SCREENS}.launch_sessions:_LaunchCardScreen",
    f"{_SCREENS}.launch_sessions:_SessionsScreen",
    f"{_SCREENS}.launch_sessions:_LineupDialog",
    f"{_SCREENS}.launch_sessions:_PerAgentScreen",
    f"{_SCREENS}.launch_sessions:_NeedsChoiceChooser.run",
    f"{_SCREENS}.models:_ModelsScreen",
    f"{_SCREENS}.profiles:_ProfilesScreen",
    f"{_SCREENS}.propagation:_PropagationScreen",
    f"{_SCREENS}.providers:_ProvidersScreen",
    f"{_SCREENS}.settings:_SettingsScreen",
    f"{_SCREENS}.transition:_TransitionScreen",
    f"{_SCREENS}.signin:account_modal",
    f"{_SCREENS}.signin:run_sign_in_flow",
    f"{_SCREENS}.signin:sign_out_flow",
    f"{_SCREENS}.gateway_actions:dialog",
    f"{_SCREENS}.gateway_actions:log_view",
    "claude_multi.tui:SelectList",
    "claude_multi.tui:Modal",
    "claude_multi.tui:TextView",
    "claude_multi.tui:OnboardingForm",
    "claude_multi.tui:ProfileEditorScreen",
    "claude_multi.tui:BindingPicker",
    "claude_multi.tui:RoutingPreviewScreen",
    "claude_multi.tui:NamedBindingsScreen",
    "claude_multi.tui:_GeneralForm",
    "claude_multi.tui:_NativeForm",
)


def _drawing(node: ast.ClassDef) -> bool:
    methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    return "_draw" in methods or {"draw", "run"} <= methods


def discovered() -> set[str]:
    """What the code says the inventory must hold: every class of the
    screens package and of ``tui`` that draws a screen, the sign-in and
    gateway flows (a public function that takes the window), the Get started
    steps (the handlers its Enter opens) and the needs-a-choice chooser."""

    import claude_multi.cli.screens as screens

    found: set[str] = set()
    names = ["claude_multi.tui", *(f"{_SCREENS}.{info.name}" for info in pkgutil.iter_modules(screens.__path__))]
    for name in names:
        tree = ast.parse(inspect.getsource(importlib.import_module(name)))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and _drawing(node):
                found.add(f"{name}:{node.name}")
            if (isinstance(node, ast.FunctionDef) and name.rsplit(".", 1)[1] in ("signin", "gateway_actions")
                    and not node.name.startswith("_") and "win" in {arg.arg for arg in node.args.args}):
                found.add(f"{name}:{node.name}")
            if isinstance(node, ast.ClassDef) and node.name == "_GetStartedScreen":
                opened = next(item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "_open")
                handlers = next(item for item in ast.walk(opened) if isinstance(item, ast.Dict))
                found.update(f"{name}:_GetStartedScreen.{value.attr}" for value in handlers.values
                             if isinstance(value, ast.Attribute))
    found.add(f"{_SCREENS}.launch_sessions:_NeedsChoiceChooser.run")
    return found


def _unit(entry: str) -> tuple[Any, str]:
    """``(owner, attribute)`` whose call draws (or composes) an inventory entry."""

    module_name, _, qualname = entry.partition(":")
    owner: Any = importlib.import_module(module_name)
    parts = qualname.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    target = getattr(owner, parts[-1])
    if isinstance(target, type):
        return target, "_draw" if "_draw" in vars(target) else "draw"
    return owner, parts[-1]


@contextlib.contextmanager
def _tracing(seen: set[str]) -> Iterator[None]:
    """Record each inventory entry that draws while the block runs."""

    with contextlib.ExitStack() as stack:
        for entry in INVENTORY:
            owner, name = _unit(entry)
            original = vars(owner)[name] if isinstance(owner, type) else getattr(owner, name)

            def traced(*args: Any, _entry: str = entry, _original: Any = original, **kwargs: Any) -> Any:
                seen.add(_entry)
                return _original(*args, **kwargs)

            stack.enter_context(mock.patch.object(owner, name, traced))
        yield


def screens() -> dict[str, Callable[[Path], str]]:
    """Every swept screen: the goldens' builders (the wide ones once, at
    their own size class) and the card states above."""

    names = {name: builder for name, builder in R.GOLDENS.items() if name not in R.WIDE_SCREENS
             and name not in R.GOLDEN_SIZES}
    return {**names, **EXTRA}


@contextlib.contextmanager
def _recording(cut: list[tuple[str, str]]) -> Iterator[None]:
    """Record every ``(text, shown)`` a screen cuts to fit."""

    real_clip, real_add = views.clip, tui.safe_add

    def clip(text: str, width: int) -> str:
        shown = real_clip(text, width)
        if shown != text and width > 0:
            cut.append((text, shown))
        return shown

    def safe_add(win: Any, row: int, col: int, text: str, attr: int = 0) -> None:
        try:
            height, width = win.getmaxyx()
        except (AttributeError, tui.CursesError):
            height = width = 0
        visible = tui.visible_text(text)
        room = width - max(col, 0) - 1
        if 0 <= row < height and col < width - 1 and 0 < room < len(visible):
            cut.append((visible, visible[:room]))
        real_add(win, row, col, text, attr)

    with mock.patch.object(views, "clip", clip), mock.patch.object(tui, "safe_add", safe_add):
        yield


def lost_actions(text: str, shown: str) -> list[str]:
    """The actions ``text`` names that ``shown`` no longer carries."""

    return [match.group() for match in ACTION.finditer(text) if match.group() not in shown]


def sweep(names: dict[str, Callable[[Path], str]] | None = None) -> list[dict[str, Any]]:
    rows = []
    for name, builder in (names or screens()).items():
        for size in SIZES:
            cut: list[tuple[str, str]] = []
            drawn: set[str] = set()
            root = Path(tempfile.mkdtemp(prefix=f"cm-sweep-{name}-"))
            try:
                with _recording(cut), _tracing(drawn):
                    R.at_size(builder, size)(root)
            finally:
                shutil.rmtree(root, ignore_errors=True)
            unique = dict.fromkeys(cut)
            lost = [(text, shown, actions) for text, shown in unique
                    if (actions := lost_actions(text, shown))]
            rows.append({"screen": name, "size": f"{size[0]}x{size[1]}", "cut": len(unique),
                         "drawn": sorted(drawn),
                         "lost": [{"shown": shown, "actions": actions} for _text, shown, actions in lost]})
    return rows


def table(rows: list[dict[str, Any]] | None = None) -> str:
    """The recorded sweep as a table: screen, size, texts cut to fit, and
    the actions lost (none)."""

    rows = sweep() if rows is None else rows
    lines = ["| screen | size | cut to fit | actions lost |", "| --- | --- | --- | --- |"]
    for row in rows:
        lost = "; ".join(", ".join(item["actions"]) for item in row["lost"]) or "none"
        lines.append(f"| {row['screen']} | {row['size']} | {row['cut']} | {lost} |")
    return "\n".join(lines)


class SweepTests(unittest.TestCase):
    maxDiff = None

    def test_no_screen_cuts_off_an_action_at_80x24_or_120x40(self) -> None:
        rows = sweep()
        self.assertEqual({row["size"] for row in rows}, {"80x24", "120x40"})
        self.assertGreaterEqual(len({row["screen"] for row in rows}), len(EXTRA) + 40)
        lost = [f"{row['screen']} {row['size']}: {item['shown']!r} lost {item['actions']}"
                for row in rows for item in row["lost"]]
        self.assertEqual(lost, [])
        # Every screen of the inventory was drawn at both sizes.
        sizes: dict[str, set[str]] = {entry: set() for entry in INVENTORY}
        for row in rows:
            for entry in row["drawn"]:
                sizes[entry].add(row["size"])
        self.assertEqual({entry: sorted(found) for entry, found in sizes.items() if found != {"80x24", "120x40"}},
                         {})

    def test_the_inventory_is_every_screen_the_code_draws(self) -> None:
        self.assertEqual(sorted(discovered() - set(INVENTORY)), [], "screens the inventory misses")
        for entry in INVENTORY:
            with self.subTest(entry=entry):
                owner, name = _unit(entry)
                self.assertTrue(callable(getattr(owner, name)))
        self.assertEqual(len(set(INVENTORY)), len(INVENTORY))

    def test_the_sweep_sees_a_cut_action(self) -> None:
        # The recorder and the classifier together: a cut that drops a key
        # route is reported, a cut that keeps it is not.
        cut: list[tuple[str, str]] = []
        with _recording(cut):
            views.clip("no record — G → L signs in", 12)
        self.assertEqual(lost_actions(*cut[0]), ["G → L"])
        self.assertEqual(lost_actions("x — G → L", views.clip_keeping_remedy("x" * 30 + " — G → L", 25)), [])
        self.assertEqual(lost_actions("explore replace → native", "explore …"), [])


if __name__ == "__main__":
    unittest.main()
