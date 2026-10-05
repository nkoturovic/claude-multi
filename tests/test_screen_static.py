"""Static checks over the screen code.

- Every ``*KEYBAR*`` binding-tuple constant in ``cli.py``/``tui.py`` ends
  ``("?", …), ("Esc", …)``.
- No function of the screen regions (:data:`SCREEN_REGIONS`) or of
  ``views.py`` calls ``subprocess`` or ``os.exec*`` itself.  The
  subprocesses a screen can reach are reached through calls, never made in
  the scanned code, except ``$EDITOR``: the editor's ``^G`` runs it in
  ``tui.ProfileEditorScreen._default_opener``, the one process call the
  tui editor stack (:data:`TUI_REGIONS`) may make.  The update flow and
  the doctor repair offer (``_CardHealthActions._run_health`` →
  ``_doctor_repair_all``), token rotation
  (``_SettingsScreen._rotate`` → ``_doctor_rotate_token``) and the journal
  read (``_read_gateway_journal``: captured, bounded, metadata-only) are
  reached through calls.  Two
  more are internal to the APIs the screens call:
  ``compiler.git_work_tree`` through ``Runtime.prepare``, and the
  verified-binary ``claude stop`` through ``_stop_runtime`` (the kept
  ``_stop_live`` flow, now ``_SessionsScreen._stop_live`` in the sessions
  region, and the resume-gate modal).
- No string constant containing ``systemctl`` in ``views.py``, ``tui.py`` or
  the screen regions (remedies go through
  ``cli.gateway_service_hint``).
- ``views.py`` imports only its allowed set; ``cli.py`` and ``views.py`` never
  import ``curses``/``termios`` (also in ``test_tui.CursesAbsentTests``).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _layout import REPO_ROOT

SRC = REPO_ROOT / "src" / "claude_multi"
SPEC = "the screen static contract"

# The screen regions of cli.py, by screen family (classes and module functions).
SCREEN_REGIONS: dict[str, tuple[str, ...]] = {
    "catalog": (
        "_ModelsScreen",
        "run_models_screen",
        "_ProvidersScreen",
        "run_providers_screen",
        "_SettingsScreen",
        "run_settings_screen",
        "_screen_too_small",
        "_help_modal",
        "_default_effective",
    ),
    "editor": (
        "_TextReportScreen",
        "_PropagationScreen",
        "run_propagation_screen",
        "_curses_ok",
        "_state_error_text",
        "_editor_bindings",
        "_profile_editor_callbacks",
        "_profile_editor_state",
        "_run_profile_editor",
    ),
    "card": (
        "_CardHealthActions",
        "_LaunchCardScreen",
        "launch_card",
        "_perform_card_result",
        "profile_pick_order",
        "_profile_recency",
        "_card_action",
        "_record_resume_target",
        "_DirectScreen",
        "run_direct_screen",
        "_direct_marks",
        "_direct_reason",
        "_direct_selector_line",
        "_direct_detail_reserve",
        "_wrap_help_lines",
        "_ScrollModal",
    ),
    "sessions": (
        "_liveness",
        "_profile_choice_items",
        "_run_choice_list",
        "_resume_card_result",
        "_NeedsChoiceChooser",
        "_PerAgentScreen",
        "_LineupDialog",
        "_SessionsScreen",
        "_run_screen_curses",
        "_sessions_list_tui",
        "_needs_choice_tui",
    ),
}
# The 3.0 editor stack in tui.py.  Only ``ProfileEditorScreen._default_opener``
# (the ``^G`` ``$EDITOR`` run, under ``suspended_curses``) may start a process.
TUI_REGIONS = (
    "ProfileEditorState",
    "ProfileEditorScreen",
    "_GeneralForm",
    "_NativeForm",
    "BindingPicker",
    "RoutingPreviewScreen",
    "NamedBindingsScreen",
    "run_profile_editor",
    "_too_small",
    "_reflow",
    "_help",
    "_clip_at",
)
TUI_EDITOR_SPAWN = ("ProfileEditorScreen", "_default_opener")
# The only reaching calls allowed from the regions (see the module docstring).
# ``_stop_runtime`` (the verified-binary ``claude stop``) from the
# sessions screen's E (``_SessionsScreen._stop_live``, the kept 2.x flow).
ALLOWED_REACH = {"_read_gateway_journal", "_doctor_rotate_token", "_doctor_repair_all", "_stop_runtime"}
VIEWS_IMPORTS = {"catalog", "compiler", "profile", "quota", "scope", "sessions", "settings", "strict_json"}


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"), filename=name)


SCREEN_OWNERS = {'_ModelsScreen': 'claude_multi.cli.screens.models',
 'run_models_screen': 'claude_multi.cli.screens.models',
 '_ProvidersScreen': 'claude_multi.cli.screens.providers',
 'run_providers_screen': 'claude_multi.cli.screens.providers',
 '_SettingsScreen': 'claude_multi.cli.screens.settings',
 'run_settings_screen': 'claude_multi.cli.screens.settings',
 '_screen_too_small': 'claude_multi.cli.screens.common',
 '_help_modal': 'claude_multi.cli.screens.common',
 '_default_effective': 'claude_multi.cli.screens.common',
 '_TextReportScreen': 'claude_multi.cli.screens.common',
 '_PropagationScreen': 'claude_multi.cli.screens.propagation',
 'run_propagation_screen': 'claude_multi.cli.screens.propagation',
 '_curses_ok': 'claude_multi.cli.screens.common',
 '_state_error_text': 'claude_multi.cli.screens.common',
 '_editor_bindings': 'claude_multi.cli.selection',
 '_profile_editor_callbacks': 'claude_multi.cli.screens.profile_editor',
 '_profile_editor_state': 'claude_multi.cli.screens.profile_editor',
 '_run_profile_editor': 'claude_multi.cli.screens.profile_editor',
 '_CardHealthActions': 'claude_multi.cli.screens.launch_sessions',
 '_LaunchCardScreen': 'claude_multi.cli.screens.launch_sessions',
 'launch_card': 'claude_multi.cli.screens.launch_sessions',
 '_perform_card_result': 'claude_multi.cli.launch_flow',
 'profile_pick_order': 'claude_multi.cli.selection',
 '_profile_recency': 'claude_multi.cli.selection',
 '_card_action': 'claude_multi.cli.selection',
 '_record_resume_target': 'claude_multi.cli.selection',
 '_DirectScreen': 'claude_multi.cli.screens.direct',
 'run_direct_screen': 'claude_multi.cli.screens.direct',
 '_direct_marks': 'claude_multi.cli.screens.direct',
 '_direct_reason': 'claude_multi.cli.screens.direct',
 '_direct_selector_line': 'claude_multi.cli.screens.direct',
 '_direct_detail_reserve': 'claude_multi.cli.screens.direct',
 '_wrap_help_lines': 'claude_multi.cli.screens.common',
 '_ScrollModal': 'claude_multi.cli.screens.common',
 '_liveness': 'claude_multi.cli.session_facts',
 '_profile_choice_items': 'claude_multi.cli.selection',
 '_run_choice_list': 'claude_multi.cli.screens.common',
 '_resume_card_result': 'claude_multi.cli.screens.launch_sessions',
 '_NeedsChoiceChooser': 'claude_multi.cli.screens.launch_sessions',
 '_PerAgentScreen': 'claude_multi.cli.screens.launch_sessions',
 '_LineupDialog': 'claude_multi.cli.screens.launch_sessions',
 '_SessionsScreen': 'claude_multi.cli.screens.launch_sessions',
 '_run_screen_curses': 'claude_multi.cli.screens.common',
 '_sessions_list_tui': 'claude_multi.cli.screens.launch_sessions',
 '_needs_choice_tui': 'claude_multi.cli.screens.launch_sessions',
 'ConnectActions': 'claude_multi.cli.screens.providers',
 '_GetStartedScreen': 'claude_multi.cli.screens.get_started',
 'run_get_started': 'claude_multi.cli.screens.get_started',
 '_ProviderPicker': 'claude_multi.cli.screens.get_started',
 'run_picker': 'claude_multi.cli.screens.get_started',
 'run_sign_in_flow': 'claude_multi.cli.screens.signin',
 'sign_out_flow': 'claude_multi.cli.screens.signin',
 'account_modal': 'claude_multi.cli.screens.signin',
 '_ProfilesScreen': 'claude_multi.cli.screens.profiles',
 'run_profiles_screen': 'claude_multi.cli.screens.profiles'}

def _regions(tree: ast.Module) -> list[ast.AST]:
    found = []
    for name, owner in SCREEN_OWNERS.items():
        relative = owner.removeprefix("claude_multi.").replace(".", "/")
        path = relative + ("/__init__.py" if owner == "claude_multi.cli" else ".py")
        matches = [node for node in _tree(path).body
                   if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name]
        if len(matches) != 1:
            raise AssertionError(f"{SPEC}: {owner}.{name} resolves {len(matches)} times")
        found.extend(matches)
    return found


def _tui_regions() -> list[ast.AST]:
    tree = _tree("tui.py")
    found = [
        node for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in TUI_REGIONS
    ]
    missing = set(TUI_REGIONS) - {node.name for node in found}
    if missing:
        raise AssertionError(f"{SPEC}: editor regions not found in tui.py: {sorted(missing)}")
    return found


def _process_calls(node: ast.AST) -> list[str]:
    """``subprocess.*`` / ``os.exec*`` / ``os.spawn*`` / ``os.system`` calls under ``node``."""

    hits = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if not isinstance(owner, ast.Name):
            continue
        attr = call.func.attr
        if owner.id == "subprocess" or (
            owner.id == "os" and (attr.startswith(("exec", "spawn", "posix_spawn")) or attr in ("system", "popen"))
        ):
            hits.append(f"{owner.id}.{attr} (line {call.lineno})")
    return hits


def _strings(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class KeyBarConstantTests(unittest.TestCase):
    def test_every_keybar_constant_ends_with_help_then_esc(self) -> None:
        checked = 0
        for name in ("cli/text.py", "tui.py"):
            for node in _tree(name).body:
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name) or "KEYBAR" not in target.id:
                    continue
                if not isinstance(node.value, ast.Tuple):
                    continue
                pairs = [ast.literal_eval(element) for element in node.value.elts]
                with self.subTest(constant=target.id):
                    self.assertGreaterEqual(len(pairs), 2)
                    self.assertEqual(pairs[-2][0], "?", f"{SPEC}: {target.id}")
                    self.assertEqual(pairs[-1][0], "Esc", f"{SPEC}: {target.id}")
                checked += 1
        self.assertGreaterEqual(checked, 5)


class SubprocessAllowListTests(unittest.TestCase):
    def test_no_screen_region_or_view_spawns_a_process(self) -> None:
        for node in _regions(_tree("cli/__init__.py")):
            with self.subTest(region=node.name):
                self.assertEqual(_process_calls(node), [], f"{SPEC} sc[11]")
        self.assertEqual(_process_calls(_tree("views.py")), [], f"{SPEC}: views is pure")

    def test_the_editor_stack_spawns_only_the_editor_run(self) -> None:
        owner, method = TUI_EDITOR_SPAWN
        allowed = 0
        for node in _tui_regions():
            if node.name == owner:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        calls = _process_calls(item)
                        if item.name == method:
                            self.assertEqual(len(calls), 1, f"{SPEC} sc[11]: {owner}.{method}")
                            allowed += 1
                        else:
                            with self.subTest(region=f"{owner}.{item.name}"):
                                self.assertEqual(calls, [], f"{SPEC} sc[11]")
            else:
                with self.subTest(region=node.name):
                    self.assertEqual(_process_calls(node), [], f"{SPEC} sc[11]")
        self.assertEqual(allowed, 1)

    def test_the_editor_stack_never_imports_cli_lineup_sessions_or_launch(self) -> None:
        # The tui editor imports profile, settings and views only.
        imported: set[str] = set()
        for node in ast.walk(_tree("tui.py")):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                imported |= {alias.name for alias in node.names} if node.module is None else {node.module}
        self.assertFalse(imported & {"cli", "lineup", "sessions", "launch"}, imported)
        self.assertLessEqual({"profile", "settings", "views"}, imported)

    def test_the_reaching_calls_are_the_named_ones(self) -> None:
        reached = set()
        for node in _regions(_tree("cli/__init__.py")):
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", getattr(call.func, "attr", None))
                    if name in ALLOWED_REACH:
                        reached.add(name)
        self.assertEqual(reached, ALLOWED_REACH)

    def test_the_scanner_finds_a_spawn(self) -> None:
        tree = ast.parse("def f():\n    subprocess.run(['x'])\n    os.execvp('x', ['x'])\n")
        self.assertEqual(len(_process_calls(tree)), 2)


class SystemctlLiteralTests(unittest.TestCase):
    def test_no_new_systemctl_literal(self) -> None:
        for node in _regions(_tree("cli/__init__.py")):
            with self.subTest(region=node.name):
                self.assertFalse([s for s in _strings(node) if "systemctl" in s], f"{SPEC}: AD E3")
        for node in _tui_regions():
            with self.subTest(region=node.name):
                self.assertFalse([s for s in _strings(node) if "systemctl" in s], f"{SPEC}: AD E3")
        for name in ("views.py", "tui.py"):
            with self.subTest(module=name):
                self.assertFalse([s for s in _strings(_tree(name)) if "systemctl" in s], f"{SPEC}: AD E3")


class ImportTests(unittest.TestCase):
    def test_views_imports_only_the_section_3_5_set(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(_tree("views.py")):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                imported |= {alias.name for alias in node.names} if node.module is None else {node.module}
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("claude_multi"):
                imported.add(node.module)
        self.assertLessEqual(imported, VIEWS_IMPORTS, f"{SPEC}: views imports")

    def test_cli_and_views_never_import_curses_or_termios(self) -> None:
        for name in ["views.py", *[str(p.relative_to(SRC)) for p in (SRC / "cli").rglob("*.py")]]:
            for node in ast.walk(_tree(name)):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                with self.subTest(module=name):
                    self.assertFalse({m.split(".")[0] for m in modules} & {"curses", "_curses", "termios"})


if __name__ == "__main__":
    unittest.main()
