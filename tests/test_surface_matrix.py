"""The object × operation matrix against the surfaces the code has.

The census (``surface_matrix.census``) reads four inventories discovered
here: the argument parser (walked by parser identity), the gateway tool's
command table, the ``/cm`` verb table and the TUI action table. Nothing
builds a Runtime, reads a home or starts anything. Every option is
classified, value-taking ones included, and every TUI mapping cites a
dispatch fixture: a test that drives the action's own screen (calls one of
its reviewed entry points), feeds the action's key to the input driver
(FakeWindow's key script or a PTY child's send) and asserts something after
that press. The negative tests inject an operation through each source (a
switch, a value-taking option, an advertised action without its handler), a
fixture whose key is only an expected label, another screen's fixture with
the same key, and a required operation no source has, to show the census is
not a restatement of itself.
"""

from __future__ import annotations

import argparse
import ast
import functools
import io
import json
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _layout import REPO_ROOT
from claude_multi import entrypoints, lineup, lineup_files, proxy, surface_matrix as sm
from claude_multi.cli import parser as cli_parser
from claude_multi.cli import streams
import claude_multi.cli.screens.actions as tui_actions
import claude_multi.cli.screens.gateway_actions as gateway_actions


def parser_inventory(parser: argparse.ArgumentParser) -> tuple[set[tuple[str, ...]], dict]:
    """(command paths that run something, path -> {option: (switch, canonical option)}).

    A parser whose sub-commands are optional runs something itself (the bare
    launch, the bare model listing); aliases share one parser object and are
    visited once."""

    leaves: set[tuple[str, ...]] = set()
    flags: dict[tuple[str, ...], dict[str, tuple[bool, str]]] = {}
    seen: set[int] = set()

    def walk(node: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        subs = [action for action in node._actions if isinstance(action, argparse._SubParsersAction)]
        if not subs or not any(action.required for action in subs):
            leaves.add(path)
        options: dict[str, tuple[bool, str]] = {}
        for action in node._actions:
            if isinstance(action, (argparse._SubParsersAction, argparse._HelpAction)) or not action.option_strings:
                continue
            canonical = max(action.option_strings, key=len)
            for option in action.option_strings:
                options[option] = (action.nargs == 0, canonical)
        flags[path] = options
        for action in subs:
            for name, child in action.choices.items():
                walk(child, (*path, name))

    walk(parser, ())
    return leaves, flags


def proxy_inventory(commands=None) -> dict[str, tuple[str, ...]]:
    return {command.name: tuple(command.operations) for command in (commands or proxy.PROXY_COMMANDS)}


def tui_inventory(actions=None) -> list[str]:
    return [row.id for rows in (actions or tui_actions.ACTIONS).values() for row in rows]


@functools.lru_cache(maxsize=None)
def _test_module(module: str) -> ast.Module | None:
    """``tests/<module>.py`` parsed once (never imported)."""

    path = REPO_ROOT / "tests" / f"{module}.py"
    return ast.parse(path.read_text()) if path.is_file() else None


def _test_classes(module: str) -> dict[str, ast.ClassDef] | None:
    """The top-level classes of ``tests/<module>.py``."""

    tree = _test_module(module)
    return None if tree is None else {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


@functools.lru_cache(maxsize=None)
def _imported(module: str) -> dict[str, str]:
    """The test modules ``tests/<module>.py`` imports by name: local name ->
    module (``import test_cli``, ``from test_screens_card import _CardCase``)."""

    names: dict[str, str] = {}
    for node in (_test_module(module) or ast.Module(body=[])).body:
        if isinstance(node, ast.Import):
            names.update({alias.asname or alias.name: alias.name for alias in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.update({alias.asname or alias.name: node.module for alias in node.names})
    return names


def _base_class(module: str, base: ast.expr) -> tuple[str, str] | None:
    """Where a base class is defined: in the same test module, imported from
    another one by name, or spelled ``test_module.Class``."""

    if isinstance(base, ast.Name):
        if base.id in (_test_classes(module) or {}):
            return module, base.id
        source = _imported(module).get(base.id)
        return (source, base.id) if source and _test_classes(source) else None
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        source = _imported(module).get(base.value.id, base.value.id)
        return (source, base.attr) if _test_classes(source) else None
    return None


def _method(module: str, cls: str, name: str, depth: int = 0) -> ast.FunctionDef | None:
    """``cls.name`` in ``tests/<module>.py`` or in a base class defined in a
    test module."""

    node = (_test_classes(module) or {}).get(cls)
    if node is None or depth > 5:
        return None
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == name:
            return item
    for base in node.bases:
        where = _base_class(module, base)
        if where is not None and (found := _method(*where, name, depth + 1)) is not None:
            return found
    return None


def _function(module: str, name: str) -> ast.FunctionDef | None:
    tree = _test_module(module)
    return next((node for node in (tree.body if tree else ()) if isinstance(node, ast.FunctionDef)
                 and node.name == name), None)


# How a fixture presses a key that is not one character: the key strings
# FakeWindow and the PTY driver read, and the test modules' key constants.
KEY_TOKENS = {
    "Enter": ({"\n", "\r", "enter"}, {"ENTER", "KEY_ENTER"}),
    "Esc": ({"\x1b", "esc"}, {"ESC"}),
    "Tab": ({"\t", "tab"}, {"TAB"}),
    "Space": ({" "}, {"SPACE"}),
    "← →": ({"left", "right"}, {"LEFT", "RIGHT"}),
    "^S": ({"\x13"}, {"CTRL_S"}),
}
ACTION_KEYS = {row.id: row.key for rows in tui_actions.ACTIONS.values() for row in rows}

# Where a fixture's keys reach the input driver: FakeWindow's key script (its
# first argument or keys=), and a PTY child's send or feed (JourneyPTY.keys
# sends each item it is given). A key anywhere else (an expected label, an
# assertion's argument, a comparison) is not pressed.
DRIVER_CLASS = "FakeWindow"
PTY_SENDS = frozenset({"send", "feed", "keys"})

# The reviewed entry points of every screen of the TUI action table: the
# product's functions and classes a fixture calls to drive that screen. A
# fixture of ``<screen>.<verb>`` must call one of them (in the test or a
# helper it reaches), so a test of another screen that happens to press the
# same key is not evidence. The resume card is the card's class run on a
# prepared resume.
SCREEN_ENTRY_POINTS: dict[str, frozenset[str]] = {
    "card": frozenset({"_LaunchCardScreen", "launch_card"}),
    "resume-card": frozenset({"_LaunchCardScreen", "launch_card", "_resume_card_result"}),
    "sessions": frozenset({"_SessionsScreen", "_sessions_list_tui"}),
    "lineup-dialog": frozenset({"_LineupDialog"}),
    "direct": frozenset({"_DirectScreen", "run_direct_screen"}),
    "providers": frozenset({"_ProvidersScreen", "run_providers_screen"}),
    "models": frozenset({"_ModelsScreen", "run_models_screen"}),
    "settings": frozenset({"_SettingsScreen", "run_settings_screen"}),
    "profiles": frozenset({"_ProfilesScreen", "run_profiles_screen"}),
    "get-started": frozenset({"_GetStartedScreen", "run_get_started"}),
    "editor": frozenset({"ProfileEditorScreen", "run_profile_editor", "_run_profile_editor"}),
    "binding-picker": frozenset({"BindingPicker"}),
    "named-bindings": frozenset({"NamedBindingsScreen", "run_named_bindings"}),
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _is_self_call(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self")


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.With):
        return any(isinstance(item.context_expr, ast.Call) and _is_assertion(item.context_expr)
                   for item in node.items)
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith(("assert", "fail")))


class _Fixture:
    """A test method read for its dispatch: which keys reach the input driver,
    whether an assertion follows the first press, and which product entry
    points it calls. Helpers are the ``self.<name>(...)`` methods (inherited
    ones included) and the module's own functions it calls, two levels deep."""

    DEPTH = 2

    def __init__(self, module: str, cls: str) -> None:
        self.module, self.cls = module, cls

    def helper(self, call: ast.Call) -> ast.FunctionDef | None:
        if _is_self_call(call):
            return _method(self.module, self.cls, call.func.attr)
        if isinstance(call.func, ast.Name):
            return _function(self.module, call.func.id)
        return None

    @staticmethod
    def params(fn: ast.FunctionDef, bound: bool) -> list[str]:
        names = [arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args)]
        return names[1:] if bound and names and names[0] in ("self", "cls") else names

    @staticmethod
    def assigned(fn: ast.FunctionDef) -> dict[str, list[ast.AST]]:
        """name -> the expressions assigned to it in ``fn`` (loop iterables included)."""

        found: dict[str, list[ast.AST]] = {}

        def bind(target: ast.AST, value: ast.AST) -> None:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    found.setdefault(node.id, []).append(value)

        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    bind(target, node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                bind(node.target, node.value)
            elif isinstance(node, (ast.For, ast.comprehension)):
                bind(node.target, node.iter)
            elif isinstance(node, ast.NamedExpr):
                bind(node.target, node.value)
        return found

    def carried(self, fn: ast.FunctionDef, expr: ast.AST, depth: int, seen: set[str] | None = None
                ) -> tuple[set[str], set[str], set[str]]:
        """(the strings, the names and the parameters of ``fn`` an expression
        of ``fn`` carries): through local assignments and the return values of
        the helpers it calls."""

        seen = set() if seen is None else seen
        strings: set[str] = set()
        names: set[str] = set()
        params: set[str] = set()
        assigns = self.assigned(fn)
        own = set(self.params(fn, bound=False)) | {a.arg for a in (fn.args.vararg, fn.args.kwarg) if a} \
            | {a.arg for a in fn.args.kwonlyargs}
        for node in ast.walk(expr):
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                strings.add(node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
                if node.id in own:
                    params.add(node.id)
                if node.id in assigns and node.id not in seen:
                    seen.add(node.id)
                    for value in assigns[node.id]:
                        more = self.carried(fn, value, depth, seen)
                        strings |= more[0]
                        names |= more[1]
                        params |= more[2]
            elif isinstance(node, ast.Call) and depth < self.DEPTH and (helper := self.helper(node)) is not None:
                for ret in ast.walk(helper):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        more = self.carried(helper, ret.value, depth + 1)
                        strings |= more[0]
                        names |= more[1]
        return strings, names, params

    @staticmethod
    def sink(call: ast.Call) -> list[ast.AST] | None:
        """The arguments of a call to the input driver, or None: FakeWindow's
        key script, every argument of a PTY child's send."""

        name = _call_name(call)
        if name == DRIVER_CLASS:
            return [*call.args[:1], *(kw.value for kw in call.keywords if kw.arg == "keys")]
        if name in PTY_SENDS and isinstance(call.func, ast.Attribute) and not _is_self_call(call):
            return list(call.args)
        return None

    def bound(self, helper: ast.FunctionDef, call: ast.Call) -> list[tuple[str, ast.AST]]:
        """(helper parameter, argument) for each argument of ``call``."""

        params = self.params(helper, bound=_is_self_call(call))
        vararg = helper.args.vararg.arg if helper.args.vararg else ""
        pairs = [(params[index] if index < len(params) else vararg, arg) for index, arg in enumerate(call.args)]
        return pairs + [(kw.arg, kw.value) for kw in call.keywords if kw.arg]

    def carries(self, fn: ast.FunctionDef, expr: ast.AST, key: str, incoming: frozenset[str], depth: int) -> bool:
        strings, names, params = self.carried(fn, expr, depth)
        return _matches(key, strings, names) or bool(params & incoming)

    def press_line(self, fn: ast.FunctionDef, key: str, incoming: frozenset[str] = frozenset(),
                   depth: int = 0) -> tuple[int, bool] | None:
        """(the line of ``fn`` where ``key`` first reaches the driver, whether
        the helper that pressed it asserts after its own press), or None.
        ``incoming``: the parameters of ``fn`` that carry the key."""

        best: tuple[int, bool] | None = None
        for call in sorted((node for node in ast.walk(fn) if isinstance(node, ast.Call)), key=lambda n: n.lineno):
            if best is not None and call.lineno >= best[0]:
                break
            sink = self.sink(call)
            if sink is not None:
                if any(self.carries(fn, arg, key, incoming, depth) for arg in sink):
                    best = (call.lineno, False)
                continue
            helper = self.helper(call) if depth < self.DEPTH else None
            if helper is None:
                continue
            carrying = frozenset(param for param, arg in self.bound(helper, call)
                                 if param and self.carries(fn, arg, key, incoming, depth))
            found = self.press_line(helper, key, carrying, depth + 1)
            if found is not None:
                best = (call.lineno, found[1] or self.asserts_after(helper, found[0], depth + 1))
        return best

    def asserts_after(self, fn: ast.FunctionDef, line: int, depth: int) -> bool:
        """Whether ``fn`` asserts something after the press on ``line``: an
        assertion that ends there or later, or a later call of a helper that
        asserts."""

        for node in ast.walk(fn):
            if _is_assertion(node) and (node.end_lineno or 0) >= line:
                return True
            if (isinstance(node, ast.Call) and node.lineno > line and depth < self.DEPTH
                    and (helper := self.helper(node)) is not None
                    and self.asserts_after(helper, helper.lineno, depth + 1)):
                return True
        return False

    def calls(self, fn: ast.FunctionDef, names: frozenset[str], depth: int = 0, seen: set[int] | None = None) -> bool:
        """Whether ``fn`` or a helper it reaches calls one of ``names``."""

        seen = set() if seen is None else seen
        seen.add(id(fn))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) in names:
                return True
            if (depth < self.DEPTH and (helper := self.helper(node)) is not None and id(helper) not in seen
                    and self.calls(helper, names, depth + 1, seen)):
                return True
        return False


def _matches(key: str, strings: set[str], names: set[str]) -> bool:
    """Whether a key reaches the driver as these strings or key constants (a
    one-character key only as its own string)."""

    if key in KEY_TOKENS:
        texts, constants = KEY_TOKENS[key]
        return bool(strings & texts or names & constants)
    return key in strings or key.lower() in strings


def fixture_check(dotted: str, action: str, keys=None, entry_points=None) -> str | None:
    """Why ``tests.module.Class.method`` is not a dispatch fixture of
    ``action`` (read without importing it), or None. It must exist, drive the
    action's own screen (call one of its entry points), feed the action's key
    to the input driver (FakeWindow's key script or a PTY child's send), and
    assert something after that press."""

    parts = dotted.split(".")
    if len(parts) != 4 or parts[0] != "tests":
        return "does not exist"
    method = _method(parts[1], parts[2], parts[3])
    if method is None:
        return "does not exist"
    key = (ACTION_KEYS if keys is None else keys).get(action)
    if key is None:
        return f"drives {action}, which no screen offers"
    screen = action.split(".", 1)[0]
    entries = (SCREEN_ENTRY_POINTS if entry_points is None else entry_points).get(screen)
    if not entries:
        return f"drives the {screen} screen, which has no reviewed entry points"
    fixture = _Fixture(parts[1], parts[2])
    if not fixture.calls(method, entries):
        return f"never drives the {screen} screen (calls none of {', '.join(sorted(entries))})"
    pressed = fixture.press_line(method, key)
    if pressed is None:
        return f"never feeds {key} to the input driver ({action})"
    line, helper_asserted = pressed
    if not (helper_asserted or fixture.asserts_after(method, line, 0)):
        return f"asserts nothing after it presses {key}"
    return None


SUB_ATTRS = {"compose": "compose_command", "sessions": "sessions_command", "profile": "profile_command",
             "providers": "providers_command", "models": "models_command", "custom": "custom_command"}


def stream_of(path: tuple[str, ...], flag: str | None) -> str:
    """Where ``cli/streams.py`` sends the result of this spelling (the
    ``--version`` action prints to stdout itself, before any command)."""

    if not path and flag == "--version":
        return "stdout"
    args = argparse.Namespace(command=path[0] if path else None)
    for command, attr in SUB_ATTRS.items():
        setattr(args, attr, path[1] if len(path) > 1 and path[0] == command else None)
    args.print_launch = flag == "--print-launch"
    args.doctor_rotate_token = flag == "--rotate-token"
    tty, std = io.StringIO(), io.StringIO()
    return "stdout" if streams._report_output_stream(args, tty, std) is std else "terminal"


def census(**overrides) -> list[str]:
    with redirect_stdout(io.StringIO()):
        leaves, flags = parser_inventory(overrides.pop("parser", None) or cli_parser.build_parser())
    sources = dict(
        parser_leaves=leaves, parser_flags=flags, proxy_commands=proxy_inventory(),
        proxy_usage=proxy.PROXY_USAGE, cm_verbs=lineup_files.CM_VERB_NAMES, tui_actions=tui_inventory(),
        fixture_check=fixture_check, stream_of=stream_of,
    )
    sources.update(overrides)
    operations = sources.pop("operations", sm.OPERATIONS)
    return sm.census(operations, **sources)


class CensusTests(unittest.TestCase):
    def test_every_surface_is_classified_and_every_row_is_real(self) -> None:
        self.assertEqual(census(), [])

    def test_every_required_operation_has_a_row(self) -> None:
        rows = sm.rows()
        self.assertEqual(sorted(set(sm.REQUIRED) - set(rows)), [])
        self.assertEqual(len(sm.REQUIRED), len(set(sm.REQUIRED)))
        # Every inventory group is represented.
        self.assertEqual({rows[op].object for op in sm.REQUIRED}, set(sm.OBJECTS))

    def test_the_gateway_tool_operations_are_rows(self) -> None:
        rows = sm.rows()
        for command in proxy.PROXY_COMMANDS:
            for operation in command.operations:
                with self.subTest(command=command.name, operation=operation):
                    self.assertIn(command.name, rows[operation].proxy)
                    self.assertEqual(rows[operation].exit, "proxy")

    def test_the_tui_leaves_the_gateway_cli_only_operations_with_their_reasons(self) -> None:
        rows = sm.rows()
        for verb, row in (("gateway stop", "gateway.stop"), ("gateway clear-hold", "gateway.clear-hold"),
                          ("gateway service uninstall", "gateway-service.uninstall")):
            with self.subTest(verb=verb):
                self.assertEqual(rows[row].absent["tui"], gateway_actions.CLI_ONLY[verb])
                self.assertEqual(rows[row].tui, ())
        # Service uninstall is not product uninstall.
        self.assertNotEqual(rows["gateway-service.uninstall"].cli, rows["install.uninstall"].cli)

    def test_the_window_ceiling_rows(self) -> None:
        rows = sm.rows()
        self.assertEqual(rows["settings.ceiling-show"].cli, ("window-ceiling",))
        self.assertEqual(rows["settings.ceiling-reset"].cli, ("window-ceiling --reset",))
        for row in ("settings.ceiling-show", "settings.ceiling-set", "settings.ceiling-reset"):
            with self.subTest(row=row):
                self.assertTrue(rows[row].tui)
        self.assertIn("show", rows["settings.role-windows"].cm)
        self.assertIn("--print-launch", rows["settings.role-windows-launch"].cli)

    def test_the_ceiling_reset_is_reversible_not_destructive(self) -> None:
        # The result names the previous ceiling and the command that sets it
        # again, so the reset asks nothing at the terminal and is no
        # destructive operation; the reason is part of the row.
        row = sm.rows()["settings.ceiling-reset"]
        self.assertFalse(row.destructive)
        self.assertIsNone(row.confirm)
        self.assertIn("window-ceiling VALUE", row.reversible or "")
        document = next(op for op in sm.document()["operations"] if op["id"] == "settings.ceiling-reset")
        self.assertEqual((document["destructive"], document["reversible"]), (False, row.reversible))

    def test_a_reversible_row_is_never_destructive_and_states_a_reason(self) -> None:
        rows = [replace(op, reversible="the previous value comes back with one command")
                if op.id == "sessions.forget" else op for op in sm.OPERATIONS]
        self.assertIn("row sessions.forget: destructive and reversible at once", census(operations=rows))
        rows = [replace(op, reversible="later") if op.id == "settings.ceiling-reset" else op
                for op in sm.OPERATIONS]
        self.assertTrue(any(problem.startswith("row settings.ceiling-reset: the reversible reason")
                            for problem in census(operations=rows)))

    def test_every_destructive_row_names_its_confirmation(self) -> None:
        for op in sm.OPERATIONS:
            if op.destructive:
                with self.subTest(row=op.id):
                    self.assertIn(op.confirm, sm.CONFIRMS)

    def test_the_document_is_deterministic_json(self) -> None:
        first = json.dumps(sm.document(), sort_keys=True)
        self.assertEqual(first, json.dumps(sm.document(), sort_keys=True))
        document = json.loads(first)
        self.assertEqual(len(document["operations"]), len(sm.OPERATIONS))
        paths = [t["path"] for op in document["operations"] for t in op["tui"]]
        self.assertTrue(paths and all(path for path in paths))

    def test_the_census_reads_no_home_and_builds_no_runtime(self) -> None:
        import claude_multi.cli.runtime as runtime_mod

        with mock.patch.object(runtime_mod.Runtime, "__init__", side_effect=AssertionError("no Runtime")), \
                mock.patch("claude_multi.paths.home", side_effect=AssertionError("no home")):
            self.assertEqual(census(), [])


class NegativeCensusTests(unittest.TestCase):
    """An operation injected through each source is found unclassified."""

    def test_a_new_command_is_unclassified(self) -> None:
        with redirect_stdout(io.StringIO()):
            parser = cli_parser.build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        commands.add_parser("frobnicate")
        self.assertIn("command frobnicate: no row classifies it", census(parser=parser))

    def test_a_new_switch_is_unclassified(self) -> None:
        with redirect_stdout(io.StringIO()):
            parser = cli_parser.build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        sessions = commands.choices["sessions"]
        listing = next(a for a in sessions._actions if isinstance(a, argparse._SubParsersAction)).choices["list"]
        listing.add_argument("--purge", action="store_true")
        self.assertIn("option sessions list --purge: no row classifies it", census(parser=parser))

    def test_a_value_taking_option_is_unclassified(self) -> None:
        with redirect_stdout(io.StringIO()):
            parser = cli_parser.build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        sessions = commands.choices["sessions"]
        listing = next(a for a in sessions._actions if isinstance(a, argparse._SubParsersAction)).choices["list"]
        listing.add_argument("--archive-to", metavar="FILE", help="write the sessions to FILE")
        self.assertIn("option sessions list --archive-to: no row classifies it", census(parser=parser))
        # Classified as an input of the listing, it is accepted.
        self.assertNotIn("option sessions list --archive-to: no row classifies it",
                         census(parser=parser, option_arguments={**sm.OPTION_ARGUMENTS,
                                                                  "sessions list --archive-to": "an archive file"}))

    def test_an_input_classification_without_its_option_is_found(self) -> None:
        problems = census(option_arguments={**sm.OPTION_ARGUMENTS, "sessions list --archive-to": "an archive file"})
        self.assertIn("option sessions list --archive-to: classified as an input, but no command takes it", problems)

    def test_an_advertised_action_without_a_handler_cites_no_dispatch(self) -> None:
        # A key a bar advertises (Z) with no handler: the only test a row
        # could name is an unrelated one, which never presses it.
        keys = {**ACTION_KEYS, "providers.frobnicate": "Z"}
        unrelated = "tests.test_tui_dispatch.ProvidersDispatchTests.test_q_shows_the_details"
        row = sm.Op("providers.frobnicate", "providers", "frobnicate a provider",
                    absent={"cli": "a provider is never frobnicated from a shell"},
                    tui=(sm.Tui("providers.frobnicate", "card → G → Z", sm.ALWAYS, unrelated),))
        problems = census(operations=[*sm.OPERATIONS, row], tui_actions=[*tui_inventory(), "providers.frobnicate"],
                          fixture_check=lambda dotted, action: fixture_check(dotted, action, keys))
        self.assertIn(f"row providers.frobnicate: TUI fixture {unrelated} never feeds Z to the input driver "
                      "(providers.frobnicate)", problems)

    def test_a_row_naming_a_test_that_never_presses_its_key_is_found(self) -> None:
        unrelated = "tests.test_screens_editor.PickerTests.test_help_and_the_effort_line"
        rows = [replace(op, tui=tuple(replace(t, fixture=unrelated) if t.action == "binding-picker.details" else t
                                      for t in op.tui)) for op in sm.OPERATIONS]
        self.assertIn(f"row models.show: TUI fixture {unrelated} never feeds V to the input driver "
                      "(binding-picker.details)", census(operations=rows))

    def test_a_key_only_in_the_expected_labels_is_not_pressed(self) -> None:
        # The card's key test feeds g m o d s e h v; "P" is only in the keybar
        # labels it compares, so it is no fixture of card.profiles.
        fixture = "tests.test_screens_card.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"
        rows = [replace(op, tui=tuple(replace(t, fixture=fixture) if t.action == "card.profiles" else t
                                      for t in op.tui)) for op in sm.OPERATIONS]
        self.assertIn(f"row profiles.list: TUI fixture {fixture} never feeds P to the input driver (card.profiles)",
                      census(operations=rows))
        # The keys it does feed keep it the fixture of their actions.
        self.assertIsNone(fixture_check(fixture, "card.providers"))

    def test_another_screens_fixture_with_the_same_key_is_refused(self) -> None:
        # An unimplemented Providers action on V cites the Models screen's V
        # test: the key is fed and asserted on, but on another screen.
        keys = {**ACTION_KEYS, "providers.inspect-new": "V"}
        models_v = "tests.test_tui_dispatch.ModelsDispatchTests.test_v_shows_the_full_details"
        row = sm.Op("providers.inspect-new", "providers", "inspect a new provider",
                    absent={"cli": "a new provider is inspected on its screen"},
                    tui=(sm.Tui("providers.inspect-new", "card → G → V", sm.ALWAYS, models_v),))
        problems = census(operations=[*sm.OPERATIONS, row], tui_actions=[*tui_inventory(), "providers.inspect-new"],
                          fixture_check=lambda dotted, action: fixture_check(dotted, action, keys))
        self.assertIn(f"row providers.inspect-new: TUI fixture {models_v} never drives the providers screen "
                      "(calls none of _ProvidersScreen, run_providers_screen)", problems)
        # On its own screen the same test is the fixture of the Models V.
        self.assertIsNone(fixture_check(models_v, "models.details"))

    SYNTHETIC = """
class Fixture:
    def press(self, screen, *keys):
        screen.run(FakeWindow([*keys, ESC]))

    def label(self, key):
        return f"{key} details"

    def test_asserts_only_before_it_presses(self):
        screen = _ProvidersScreen(self.runtime)
        self.assertTrue(screen.rows)
        screen.run(FakeWindow(["q", ESC]))

    def test_names_the_key_only_where_it_compares(self):
        screen = _ProvidersScreen(self.runtime)
        win = FakeWindow([ESC])
        screen.run(win)
        self.assertEqual(screen.last, "q")
        self.assertIn(self.label("q"), win.text())

    def test_presses_through_a_helper(self):
        screen = _ProvidersScreen(self.runtime)
        self.press(screen, "q")
        self.assertEqual(screen.message, "")
"""

    def synthetic(self, method: str) -> str | None:
        """``fixture_check`` of ``Fixture.<method>`` in :attr:`SYNTHETIC` (read, never run)."""

        real = _test_module
        with mock.patch(f"{__name__}._test_module",
                        side_effect=lambda module: ast.parse(self.SYNTHETIC) if module == "synthetic" else real(module)):
            return fixture_check(f"tests.synthetic.Fixture.{method}", "providers.details")

    def test_the_press_and_the_assertion_after_it_are_required(self) -> None:
        self.assertEqual(self.synthetic("test_asserts_only_before_it_presses"), "asserts nothing after it presses Q")
        self.assertEqual(self.synthetic("test_names_the_key_only_where_it_compares"),
                         "never feeds Q to the input driver (providers.details)")
        # A key passed to a helper that feeds the driver is pressed.
        self.assertIsNone(self.synthetic("test_presses_through_a_helper"))

    def test_every_screen_names_entry_points_the_product_defines(self) -> None:
        self.assertEqual(set(SCREEN_ENTRY_POINTS), set(tui_actions.ACTIONS))
        defined: set[str] = set()
        for path in (REPO_ROOT / "src" / "claude_multi").rglob("*.py"):
            defined |= {node.name for node in ast.parse(path.read_text()).body
                        if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        for screen, names in SCREEN_ENTRY_POINTS.items():
            with self.subTest(screen=screen):
                self.assertTrue(names)
                self.assertLessEqual(set(names), defined)

    def test_a_dispatch_fixture_fails_without_its_handler(self) -> None:
        import test_tui_dispatch
        import claude_multi.cli.screens.providers as providers_screen

        def run() -> unittest.TestResult:
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName(
                "ProvidersDispatchTests.test_q_shows_the_details", test_tui_dispatch).run(result)
            return result

        self.assertTrue(run().wasSuccessful())
        with mock.patch.object(providers_screen._ProvidersScreen, "_details", lambda _self, _win: None):
            self.assertFalse(run().wasSuccessful(), "the fixture asserts what Q shows")

    def test_a_new_gateway_tool_command_is_unclassified(self) -> None:
        commands = {**proxy_inventory(), "frobnicate": ("gateway.frobnicate",)}
        problems = census(proxy_commands=commands)
        self.assertIn("claude-multi-proxy frobnicate: no row classifies it", problems)
        self.assertIn("claude-multi-proxy frobnicate: missing from its usage", problems)
        self.assertIn("claude-multi-proxy frobnicate: operation gateway.frobnicate has no row", problems)

    def test_a_new_cm_verb_is_unclassified(self) -> None:
        problems = census(cm_verbs=(*lineup_files.CM_VERB_NAMES, "teleport"))
        self.assertIn("/cm teleport: no row classifies it", problems)

    def test_a_new_tui_action_is_unclassified(self) -> None:
        problems = census(tui_actions=[*tui_inventory(), "providers.frobnicate"])
        self.assertIn("TUI action providers.frobnicate: no row classifies it", problems)

    def test_a_required_operation_absent_from_every_source(self) -> None:
        problems = census(required=(*sm.REQUIRED, "sessions.teleport"))
        self.assertIn("required operation sessions.teleport: no row", problems)

    def test_a_row_without_a_surface_or_reason(self) -> None:
        rows = [*sm.OPERATIONS, sm.Op("sessions.teleport", "sessions", "move a session elsewhere")]
        self.assertIn("row sessions.teleport: neither a surface nor a reason", census(operations=rows))

    def test_reasons_that_lean_on_a_plan_or_an_owner_are_refused(self) -> None:
        for reason in ("TODO", "later", "the docs owner adds it later", "once 071 lands",
                       "not yet: see the follow-up"):
            with self.subTest(reason=reason):
                rows = [*sm.OPERATIONS, sm.Op("sessions.teleport", "sessions", "move a session",
                                             absent={"cli": reason, "tui": "a session never moves elsewhere"})]
                self.assertTrue(any(p.startswith("row sessions.teleport: the cli absence reason")
                                    for p in census(operations=rows)), reason)

    def test_a_user_command_without_a_tui_path_or_reason(self) -> None:
        rows = [replace(op, absent={}) if op.id == "portability.export" else op for op in sm.OPERATIONS]
        self.assertIn("row portability.export: a user command with neither a TUI path nor its reason",
                      census(operations=rows))

    def test_a_destructive_row_without_its_confirmation(self) -> None:
        rows = [replace(op, confirm=None) if op.id == "sessions.forget" else op for op in sm.OPERATIONS]
        self.assertIn("row sessions.forget: destructive without its confirmation", census(operations=rows))

    def test_a_stream_class_that_disagrees_with_the_stream_table(self) -> None:
        rows = [replace(op, stream="terminal") if op.id == "sessions.forget" else op for op in sm.OPERATIONS]
        self.assertIn("row sessions.forget: 'sessions forget' writes to stdout, the row says terminal",
                      census(operations=rows))

    def test_a_missing_endpoint_action_or_fixture(self) -> None:
        missing = sm.Op("sessions.teleport", "sessions", "move a session", cli=("sessions teleport",),
                        tui=(sm.Tui("sessions.teleport", "card → S → Z", "always",
                                    "tests.test_screens_sessions.Nowhere.test_nothing"),),
                        stream="stdout", exit="user")
        problems = census(operations=[*sm.OPERATIONS, missing])
        self.assertIn("row sessions.teleport: 'sessions teleport' names no command", problems)
        self.assertIn("TUI action sessions.teleport: a row names an action no screen offers", problems)
        self.assertIn("row sessions.teleport: TUI fixture tests.test_screens_sessions.Nowhere.test_nothing "
                      "does not exist", problems)

    def test_a_tui_mapping_without_its_parts(self) -> None:
        bare = sm.Op("sessions.teleport", "sessions", "move a session",
                     tui=(sm.Tui("sessions.details", "", "", ""),), absent={"cli": "a session never moves elsewhere"})
        problems = census(operations=[*sm.OPERATIONS, bare])
        for part in ("path", "available", "fixture"):
            self.assertIn(f"row sessions.teleport: a TUI mapping without its {part}", problems)

    def test_a_gateway_tool_row_keeps_the_tools_exit_statuses(self) -> None:
        rows = [replace(op, exit="user") if op.id == "gateway.run" else op for op in sm.OPERATIONS]
        self.assertIn("row gateway.run: a gateway-tool command keeps the tool's own exit statuses",
                      census(operations=rows))


class PublicSurfaceTests(unittest.TestCase):
    """What the root help lists is deliberate: compatibility commands and
    hooks are public only where the matrix says so."""

    # Commands kept in the root help on purpose although their rows are
    # compatibility: the recovery of earlier formats, documented for the
    # few installations that still hold them.
    PUBLIC_COMPATIBILITY = frozenset({"custom", "migrate", "restore-2x"})

    def test_hidden_commands_are_compatibility_or_internal(self) -> None:
        shown = {name for _title, rows in cli_parser.COMMAND_GROUPS for name, _summary in rows}
        with redirect_stdout(io.StringIO()):
            leaves, _flags = parser_inventory(cli_parser.build_parser())
        tops = {leaf[0] for leaf in leaves if leaf}
        audiences: dict[str, set[str]] = {}
        for op in sm.OPERATIONS:
            for spelling in op.cli:
                path, _flags = sm.split_endpoint(spelling)
                if path:
                    audiences.setdefault(path[0], set()).add(op.audience)
        for top in sorted(tops - shown):
            with self.subTest(hidden=top):
                self.assertTrue(audiences[top] <= {"compatibility", "internal"}, (top, audiences[top]))
        for top in sorted(shown):
            with self.subTest(shown=top):
                public = audiences[top] - {"compatibility", "internal"}
                self.assertTrue(public or top in self.PUBLIC_COMPATIBILITY, (top, audiences[top]))

    def test_root_help_lists_no_companion_launcher(self) -> None:
        with redirect_stdout(io.StringIO()):
            text = cli_parser.build_parser().format_help()
        self.assertNotIn("claude-gateway", text)
        self.assertNotIn("claude-multi-dev", text)


class InstalledEntryPointTests(unittest.TestCase):
    """The installed public commands are the packaging manifest's launchers,
    in the bundle and in the Nix package; the developer tool is a source
    checkout's console script only."""

    def test_the_installed_set_is_the_manifest(self) -> None:
        manifest = json.loads((REPO_ROOT / "packaging" / "product.json").read_text())
        self.assertEqual(manifest["launchers"], ["claude-multi", "claude-multi-proxy"])
        scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertEqual(sorted(scripts), sorted([*manifest["launchers"], "claude-multi-dev"]))
        self.assertEqual(set(entrypoints.NAMES), set(scripts))
        self.assertEqual(set(entrypoints.PROGRAMS), set(scripts))
        source = REPO_ROOT / "bin"
        if source.is_dir():
            self.assertEqual({path.name for path in source.iterdir() if path.is_file()}, set(scripts))
        for name in ("claude-gateway",):
            self.assertNotIn(name, scripts)
            self.assertNotIn(name, manifest["launchers"])
            self.assertNotIn(name, entrypoints.NAMES)

    def test_the_nix_package_installs_the_manifest_launchers_only(self) -> None:
        manifest = json.loads((REPO_ROOT / "packaging" / "product.json").read_text())
        self.assertNotIn("claude-multi-dev", manifest["launchers"])
        package = (REPO_ROOT / "nix" / "package.nix").read_text()
        phase = package.split("installPhase = ''", 1)[1].split("\n  '';", 1)[0]
        # One wrapper loop over the launchers (the proxy has its own), none over
        # every console script.
        self.assertIn("launchers = (builtins.fromJSON (builtins.readFile ../packaging/product.json)).launchers;",
                      package)
        self.assertIn("launcherScripts = builtins.filter (name: name != proxyScript) launchers;", package)
        self.assertNotIn("(name: name != proxyScript) scripts", package)
        self.assertEqual(phase.count("makeWrapper "), 2, phase)
        self.assertIn("for entry in ${pkgs.lib.concatStringsSep \" \" launcherScripts}; do", phase)
        # The sandbox check compares what the built package installs with the
        # manifest, and never runs the developer tool from it.
        check = (REPO_ROOT / "tests" / "default.nix").read_text()
        self.assertIn("(builtins.fromJSON (builtins.readFile ../packaging/product.json)).launchers;", check)
        self.assertIn('test "$installed" = "${pkgs.lib.concatStringsSep " " launchers} "', check)
        self.assertNotIn("claude-multi-dev", check)


class DispatchTests(unittest.TestCase):
    """Each listed /cm verb and gateway-tool command dispatches."""

    # (valid words, invalid words) per verb.
    CM_GRAMMAR = {
        "show": ([], ["show", "extra"]),
        "profiles": (["profiles"], ["profiles", "extra"]),
        "profile": (["profile", "balanced"], ["profile"]),
        "set": (["set", "cm-explorer=sol:high"], ["set", "cm-nobody=sol"]),
        "unset": (["unset", "cm-explorer"], ["unset"]),
        "direct": (["direct", "sol:high"], ["direct", "a", "b"]),
        "pin": (["pin"], ["pin", "extra"]),
        "follow": (["follow"], ["follow", "extra"]),
        "fallback": (["fallback", "openai"], ["fallback"]),
        "review": (["review", "high-stakes"], ["review", "a", "b", "c"]),
        "quota": (["quota"], ["quota", "extra"]),
    }

    def test_every_cm_verb_parses_and_refuses_its_invalid_grammar(self) -> None:
        self.assertEqual(set(self.CM_GRAMMAR), set(lineup_files.CM_VERB_NAMES))
        for verb, (valid, invalid) in self.CM_GRAMMAR.items():
            with self.subTest(verb=verb):
                self.assertEqual(lineup.parse_request(valid).verb, verb)
                with self.assertRaises(lineup.LineupRefusal):
                    lineup.parse_request(invalid)
                self.assertIn(f"/cm {verb}", lineup.HELP_LINE)
        writes = {verb.name for verb in lineup_files.CM_VERBS if verb.writes}
        self.assertEqual(writes, set(lineup_files.CM_VERB_NAMES) - lineup_files.CM_READ_VERBS)

    def test_every_gateway_tool_command_dispatches_to_its_handler(self) -> None:
        handlers = {"init": "cmd_init", "status": "cmd_status", "rotate-management-key": "cmd_rotate_management_key",
                    "disable-management-key": "cmd_disable_management_key", "run": "cmd_run",
                    "claude-login": "cmd_login", "codex-device-login": "cmd_login",
                    "snapshot-auth": "cmd_snapshot_auth"}
        self.assertEqual(set(handlers), set(proxy.PROXY_COMMAND_NAMES))
        for name, handler in handlers.items():
            with self.subTest(command=name), \
                    mock.patch.object(proxy, "_entry_refusal", return_value=None), \
                    mock.patch.object(proxy, handler, return_value=0) as called, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                descriptor = next(command for command in proxy.PROXY_COMMANDS if command.name == name)
                with mock.patch.object(proxy, "_PROXY_COMMANDS",
                                       {**proxy._PROXY_COMMANDS,
                                        name: replace(descriptor, check=lambda args, environ: None)}):
                    self.assertEqual(proxy.main([name], environ={"HOME": "/nonexistent"}), 0)
                self.assertTrue(called.called)
                self.assertIn(f"  {name}", proxy.PROXY_USAGE)


if __name__ == "__main__":
    unittest.main()
