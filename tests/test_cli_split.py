"""Canonical ownership, import boundaries and portable seams."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT
from _layout import checkout_replica
from claude_multi import assets, endpoint, errors, paths, sessions, termtext, tui
from claude_multi import cli

SRC = REPO_ROOT / "src" / "claude_multi"


def module_imports(path: Path) -> set[str]:
    """Exact module-level imports, excluding future annotation syntax."""

    imports = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            if node.module == "claude_multi":
                imports.update("claude_multi" if a.name == "__version__" else f"claude_multi.{a.name}"
                               for a in node.names)
            else:
                imports.add(node.module)
    return imports


class FoundationTests(unittest.TestCase):
    def test_exact_static_leaf_imports(self):
        self.assertEqual(module_imports(SRC / "cli/text.py"), {"claude_multi.termtext"})
        self.assertEqual(module_imports(SRC / "cli/parser.py"), {
            "argparse", "pathlib", "claude_multi.lineup_files", "claude_multi.cli.text", "claude_multi.layout",
        })
        # The installation authority is a leaf: the hook path imports it.
        self.assertEqual(module_imports(SRC / "layout.py"), {
            "os", "stat", "pathlib", "claude_multi", "claude_multi.errors",
        })

    def test_no_foundation_imports_the_facade_or_ui(self):
        for path in (SRC / "cli").rglob("*.py"):
            if path.name in ("__init__.py", "__main__.py"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "claude_multi.cli", str(path))
                elif isinstance(node, ast.Import):
                    self.assertNotIn("claude_multi.cli", [a.name for a in node.names], str(path))
        imports = module_imports(SRC / "cli/runtime.py")
        self.assertFalse(imports & {"claude_multi.tui", "claude_multi.cli.doctor"})
        self.assertFalse(any(name.startswith("claude_multi.cli.screens") for name in imports))

    def test_every_extracted_module_imports_in_a_fresh_process(self):
        modules = ["claude_multi.assets", "claude_multi.endpoint", "claude_multi.termtext"]
        modules += ["claude_multi.platform." + p.stem for p in (SRC / "platform").glob("*.py")
                    if p.name != "__init__.py"]
        modules += ["claude_multi." + ".".join(p.relative_to(SRC).with_suffix("").parts)
                    for p in (SRC / "cli").rglob("*.py") if p.name != "__init__.py"]
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "HOME": tmp,
                   "XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp, "XDG_DATA_HOME": tmp}
            for module in modules:
                with self.subTest(module=module):
                    result = subprocess.run([sys.executable, "-B", "-c", f"import {module}"],
                                            env=env, text=True, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_compatibility_identity_and_terminal_leaf(self):
        from claude_multi import lineup_files, scope
        from claude_multi.cli import runtime, types

        self.assertIs(cli.Runtime, runtime.Runtime)
        self.assertIs(cli.CLIError, errors.CLIError)
        self.assertTrue(issubclass(errors.CLIError, RuntimeError))
        self.assertTrue(issubclass(errors.CLIError, errors.ClaudeMultiError))
        self.assertTrue(issubclass(types.LaunchPlanError, errors.CLIError))
        self.assertTrue(issubclass(types.NeedsChoiceError, errors.CLIError))
        self.assertIs(tui.visible_text, termtext.visible_text)
        self.assertIs(tui.visible_message, termtext.visible_message)
        self.assertEqual(scope.HOOK_PROTOCOL, lineup_files.HOOK_PROTOCOL)

    def test_moved_patched_names_have_only_one_binding(self):
        owners = {
            "Runtime": "cli/runtime.py", "default_asset_root": "assets.py",
            "resources_root": "__init__.py", "profile_pick_order": "cli/selection.py",
            "_live_background_prefixes": "cli/session_facts.py",
            "_original_cwd_for_adopt": "cli/session_facts.py",
            "_record_view_is_live": "cli/session_facts.py",
            "_session_record_scan": "cli/session_facts.py",
            "_session_records": "cli/session_facts.py",
            "_open_tty_streams": "cli/streams.py", "_stdio_streams_are_ttys": "cli/streams.py",
            "_stop_runtime": "cli/session_actions.py",
            "_reconcile_session_start": "cli/session_events.py",
            # The gateway-facts, doctor and doctor-action owners.
            "_doctor_now": "cli/gateway_facts.py", "_read_gateway_journal": "cli/gateway_facts.py",
            "_oauth_credential_records": "cli/gateway_facts.py",
            "_doctor_continuity": "cli/gateway_facts.py", "_ordinary_unavailable": "cli/gateway_facts.py",
            "_doctor_managed_report": "cli/doctor.py", "_doctor_retirement_radar": "cli/doctor.py",
            "_doctor_rotate_token": "cli/doctor_actions.py",
        }
        for path in (SRC / "cli").rglob("*.py"):
            for node in ast.parse(path.read_text()).body:
                names = set()
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    names.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names.update(a.asname or a.name for a in node.names)
                elif isinstance(node, ast.Assign):
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
                for name in names & owners.keys():
                    self.assertEqual(path.relative_to(SRC).as_posix(), owners[name])


class DoctorSplitTests(unittest.TestCase):
    """Gateway facts, doctor reports and doctor actions have one owner each."""

    OWNERS = {"gateway_facts": "cli/gateway_facts.py", "doctor": "cli/doctor.py",
              "doctor_actions": "cli/doctor_actions.py"}

    @staticmethod
    def definitions(path: Path) -> list[str]:
        names = []
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        return names

    # New observations and doctor checks, not moved
    # definitions; the compatibility facade (kept through 3.1) names only
    # moved ones.
    NEW_051 = frozenset({
        # Later additions are not part of the compatibility surface.
        "_report_journal", "report_events", "report_journal_facts", "_event_journal_facts",
        "_collect_doctor_report_lines", "HOOK_LOG_UNAVAILABLE", "_hook_error_observation", "doctor_report",
        "LAN_PROBE_TIMEOUT", "LAN_UNREACHABLE_REMEDY", "_LAN_REASONS", "LanReachability", "lan_host",
        "probe_lan", "lan_reachability", "discovery_known", "pinned_registry", "served_extras_info",
        "_doctor_lan_report",
        "_doctor_prune_preview",  # New action, not a compatibility export
        "_AGENT_DRIFT", "_agent_frontmatter", "_catalog_agent_drift",
        "oauth_record_observation", "_lan_live_bindings", "_doctor_readiness_report",
        "pending_served_plan", "_pending_served_change", "_repair_all_records",
        # The local-files, platform and gateway facts of doctor.
        "_SECRET_FILES", "_PROXY_VARIABLES", "_entry_problem",
        "local_files_report", "platform_report", "gateway_runtime_report",
        # The installation and supervised-service facts of doctor.
        "install_report", "service_report", "_doctor_install_report",
        "_doctor_pin_report",  # the pinned Claude Code facts: a new check, not a move
        "inhibition_report",  # the gateway inhibition: a new check, not a move
        "SIGN_IN_ANY", "sign_in_command",  # the public sign-in remedy: new, not a move
        "_init_problem_line",  # why a doctor ran read-only
        "Finding", "WSL_SEPARATE_CONFIG", "GATEWAY_NOT_SET_UP",  # typed doctor findings
        "HOLD_COVERAGE_TEXT",  # a hold from incomplete log coverage
        "_trust_line", "_sessions_line", "_settings_denies", "_deny_drift_note", "_generated_path_denies",
        "CONFIG_DIR_IGNORED",
        "_doctor_config_dir_report", "_provider_secret_names", "_doctor_env_keep_report",
        "_doctor_new_lines_report", "_diagnostic", "_health_backend",  # doctor facts: new checks, not moves
        "DOCTOR_SUMMARY", "DETAIL_PREFIXES", "detail_line", "repair_offer",  # the report's shared rendering
    })

    def test_every_moved_definition_resolves_once_in_its_owner(self):
        from claude_multi.cli import doctor, doctor_actions, gateway_facts

        modules = {"gateway_facts": gateway_facts, "doctor": doctor, "doctor_actions": doctor_actions}
        facade = set(self.definitions(SRC / "cli/__init__.py"))
        seen = {}
        added = set()
        for alias, relative in self.OWNERS.items():
            names = self.definitions(SRC / relative)
            self.assertEqual(len(names), len(set(names)), relative)
            for name in names:
                if name in self.NEW_051:
                    added.add(name)
                    self.assertFalse(hasattr(cli, name) and name in cli._COMPAT_EXPORTS, name)
                    continue
                seen.setdefault(name, []).append(alias)
                self.assertNotIn(name, facade)
                # The compatibility facade forwards to the one owner object.
                self.assertIs(getattr(cli, name), getattr(modules[alias], name))
        self.assertEqual({n for n, owners in seen.items() if len(owners) > 1}, set())
        # _doctor_operator_report is a new doctor check, not a move.
        self.assertEqual(len(seen), 74)
        self.assertEqual(added, set(self.NEW_051))

    def test_layering_reports_actions_and_observations(self):
        facts = module_imports(SRC / "cli/gateway_facts.py")
        self.assertFalse({m for m in facts if m.startswith("claude_multi.cli.")} - {"claude_multi.cli.text"})
        reports = module_imports(SRC / "cli/doctor.py")
        self.assertNotIn("claude_multi.cli.doctor_actions", reports)
        for relative in self.OWNERS.values():
            imports = module_imports(SRC / relative)
            with self.subTest(module=relative):
                self.assertFalse(imports & {"claude_multi.tui", "claude_multi.cli.screens", "claude_multi.cli"})
                self.assertFalse(any(m.startswith("claude_multi.cli.screens") for m in imports))

    def test_facade_calls_moved_names_only_through_their_owner(self):
        moved = set()
        for relative in self.OWNERS.values():
            moved.update(self.definitions(SRC / relative))
        for path in (SRC / "cli").rglob("*.py"):
            tree = ast.parse(path.read_text())
            own = set(self.definitions(path))
            bare = sorted({node.id for node in ast.walk(tree)
                           if isinstance(node, ast.Name) and node.id in moved - own})
            with self.subTest(module=path.relative_to(SRC).as_posix()):
                self.assertEqual(bare, [])


class AssetSeamTests(unittest.TestCase):
    def test_explicit_override_default_matrix(self):
        import claude_multi
        from claude_multi import proxy

        # The packaged default is the package's own data directory.
        self.assertEqual(claude_multi.resources_root(), RESOURCES_ROOT)

        for env in ({}, {"CLAUDE_MULTI_ASSETS": ""}, {"CLAUDE_MULTI_ASSETS": "/overlay"}):
            with self.subTest(env=env):
                expected = Path(env["CLAUDE_MULTI_ASSETS"]) if env.get("CLAUDE_MULTI_ASSETS") else claude_multi.resources_root()
                self.assertEqual(assets.root(environ=env), expected)
                self.assertEqual(assets.default_asset_root(environ=env), expected)
                self.assertEqual(proxy.assets_root(env), expected)
                self.assertEqual(assets.root("/explicit", environ=env), Path("/explicit"))

    def test_inherited_override_never_reaches_the_suite(self):
        env = {**os.environ, "CLAUDE_MULTI_ASSETS": "/absent/inherited-assets",
               "PYTHONPATH": str(REPO_ROOT / "src")}
        code = "import tests, os; assert 'CLAUDE_MULTI_ASSETS' not in os.environ; import claude_multi.cli"
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO_ROOT,
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The entry points as a source checkout (the sandbox tree is none).
        checkout = checkout_replica(Path(tempfile.mkdtemp(prefix="cm-checkout-")) / "checkout")
        self.addCleanup(shutil.rmtree, checkout.parent, True)
        for name in ("claude-multi", "claude-multi-proxy", "claude-multi-dev"):
            result = subprocess.run([sys.executable, str(checkout / "bin" / name), "--help" if name == "claude-multi-dev" else "--version"],
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, (name, result.stderr))
            # Observe the environment pop itself (the version/help
            # smoke above reads no asset for two of the three scripts).
            probe = ("import os, runpy, sys; runpy.run_path(sys.argv[1], run_name='probe'); "
                     "assert 'CLAUDE_MULTI_ASSETS' not in os.environ, os.environ['CLAUDE_MULTI_ASSETS']")
            result = subprocess.run([sys.executable, "-B", "-c", probe, str(checkout / "bin" / name)],
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, (name, result.stderr))

    STUBS = ("claude-multi", "claude-multi-proxy", "claude-multi-dev")

    def test_source_marker_is_staged_and_never_installed(self):
        # The Nix sandbox checks stage tests/source-tree.nix (no flake.nix)
        # and an installation carries pyproject.toml's installed paths; the
        # launchers must recognise the first as a source tree and never the
        # second.
        from _layout import installed_paths, staged_tops
        from claude_multi import entrypoints

        markers = entrypoints.SOURCE_MARKERS
        installed = installed_paths()

        def installs(path: str) -> bool:
            return any(path == item or path.startswith(item + "/") for item in installed)

        for marker in markers:
            with self.subTest(marker=marker):
                self.assertTrue((REPO_ROOT / marker).is_file())
                self.assertIn(marker.split("/", 1)[0], staged_tops())
        self.assertTrue(any(not installs(marker) for marker in markers), markers)
        # Both trees, built from those lists: the staged-like tree drops an
        # inherited override; a launcher copied anywhere else keeps what the
        # installation that placed it there states.
        probe = ("import os, runpy, sys; runpy.run_path(sys.argv[1], run_name='probe'); "
                 "print(os.environ.get('CLAUDE_MULTI_ASSETS', '<popped>'))")
        with tempfile.TemporaryDirectory() as tmp:
            for layout, extra in (("installed", ()), ("staged", markers)):
                root = Path(tmp) / layout
                for item in installed:
                    source, target = REPO_ROOT / item, root / item
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    else:
                        shutil.copy(source, target)
                shutil.copytree(REPO_ROOT / "bin", root / "bin")
                for marker in extra:
                    (root / marker).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(REPO_ROOT / marker, root / marker)
                resources = root / "src" / "claude_multi" / "data"
                env = {**os.environ, "HOME": tmp, "CLAUDE_MULTI_ASSETS": str(resources)}
                for name in self.STUBS:
                    with self.subTest(layout=layout, stub=name):
                        result = subprocess.run([sys.executable, "-B", "-c", probe, str(root / "bin" / name)],
                                                env=env, text=True, capture_output=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "<popped>" if extra else str(resources))

    def test_different_release_override_and_source_installed_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            overlay = tmp / "overlay"
            shutil.copytree(FIXTURE_ROOT, overlay)
            version = json.loads((overlay / "version.json").read_text())
            version["launcher_version"] = "99.0.0"
            (overlay / "version.json").write_text(json.dumps(version))
            (overlay / "CHEATSHEET.md").write_text("overlay documentation")
            (overlay / "registry").mkdir()
            # Schema markers prove the implicit readers used the overlay,
            # including profile's once-per-process import-time cache.
            for path in (overlay / "schemas").glob("*.json"):
                schema = json.loads(path.read_text())
                schema.setdefault("properties", {})["_049_asset_probe"] = {"const": "049-overlay"}
                path.write_text(json.dumps(schema))
            # The installed layout: the package (with its data/) in
            # site-packages, the documents in the prefix's share path.
            installed = tmp / "prefix"
            site = installed / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            shutil.copytree(SRC, site / "claude_multi", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (installed / "share" / "claude-multi").mkdir(parents=True)
            (installed / "share" / "claude-multi" / "CHEATSHEET.md").write_text("installed documentation")
            code = '''
import claude_multi
from pathlib import Path
from claude_multi import assets, catalog, continuity, custom, gateway_service, layout, profile, proxy, scope
from claude_multi import sessions, settings, upgrade
from claude_multi.cli import parser
import os
root = Path(os.environ['CLAUDE_MULTI_ASSETS'])
assert assets.root() == root == proxy.assets_root()
assert claude_multi.__version__ != '99.0.0'
assert upgrade.running_release().launcher_version == claude_multi.__version__
assert claude_multi.resources_root() != root
# Documents, executables and the service spec belong to the installation:
# the resource override moves none of them.
assert parser._doc_pointer('CHEATSHEET.md') != str(root / 'CHEATSHEET.md')
assert parser._doc_pointer('CHEATSHEET.md') == str(layout.document('CHEATSHEET.md'))
assert not scope.resolve_hook_command().startswith(str(root))
assert gateway_service.load_spec() == gateway_service.load_spec(claude_multi.resources_root())
assert catalog.registry_dir({}) is None or catalog.registry_dir({}) != root / 'registry'
assert catalog.registry_dir() == root / 'registry'
assert profile._PROFILE_SCHEMA['properties']['_049_asset_probe']['const'] == '049-overlay'
assert profile._BINDINGS_SCHEMA['properties']['_049_asset_probe']['const'] == '049-overlay'
assert settings._schema()['properties']['_049_asset_probe']['const'] == '049-overlay'
assert sessions.default_schema()['properties']['_049_asset_probe']['const'] == '049-overlay'
assert continuity._schema()['properties']['_049_asset_probe']['const'] == '049-overlay'
assert custom._schema()['properties']['_049_asset_probe']['const'] == '049-overlay'
os.environ.pop('CLAUDE_MULTI_ASSETS')
assert profile._PROFILE_SCHEMA['properties']['_049_asset_probe']['const'] == '049-overlay'
'''
            for path in (REPO_ROOT / "src", site):
                env = {**os.environ, "HOME": str(tmp), "CLAUDE_MULTI_ASSETS": str(overlay),
                       "PYTHONPATH": str(path)}
                env.pop("CLAUDE_MULTI_REGISTRY_DIR", None)
                env.pop("CLAUDE_MULTI_HOOK_COMMAND", None)
                result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)


class PathEndpointTests(unittest.TestCase):
    def test_home_xdg_and_empty_home_semantics(self):
        for home in ("/home/fixture", "/Users/fixture", ""):
            for xdg in (False, True):
                env = {"HOME": home, "CLAUDE_CONFIG_DIR": "/other/claude"}
                if xdg:
                    env.update(XDG_CONFIG_HOME="/other/config", XDG_STATE_HOME="/other/state",
                               XDG_DATA_HOME="/other/data")
                with self.subTest(home=home, xdg=xdg):
                    config = Path("/other/config") if xdg else Path(home) / ".config"
                    state = Path("/other/state") if xdg else Path(home) / ".local/state"
                    self.assertEqual(paths.config_root(env), config / "claude-multi")
                    self.assertEqual(paths.state_root(env), state / "claude-multi")
                    self.assertEqual(sessions.config_root(env), paths.config_root(env))
                    self.assertEqual(sessions.state_root(env), paths.state_root(env))
                    display_home = Path(home) if home else Path.home()
                    self.assertEqual(paths.gateway_config_dir(env), display_home / ".config/claude-multi")
                    self.assertEqual(paths.data_root(env), display_home / ".local/share/claude-multi")
                    self.assertEqual(paths.secret_env_default(env),
                                     display_home / ".config/claude-multi/secrets/provider-keys.env")
                    self.assertEqual(paths.native_projects(display_home), display_home / ".claude/projects")
                    # The managed client's settings directory; every managed
                    # launch unsets CLAUDE_CONFIG_DIR, so it never moves it.
                    self.assertEqual(paths.claude_settings_dir(env), display_home / ".claude")

    def test_endpoint_preserves_exact_catalog_fields_and_lookup_errors(self):
        gateway = {"gateway": {"base_url": "http://127.0.0.1:12345/v1//", "health_path": "/health//?a=1"}}
        value = endpoint.gateway_endpoint(gateway)
        self.assertEqual(value.base_url, gateway["gateway"]["base_url"])
        self.assertEqual(value.health_path, gateway["gateway"]["health_path"])
        partial = endpoint.gateway_endpoint({"gateway": {"base_url": "unchanged"}})
        self.assertEqual(partial.base_url, "unchanged")
        with self.assertRaises(KeyError):
            _ = partial.health_path


# ---------------------------------------------------------------------------
# Screens, commands, entry and the lazy facade.

CLI_OWNERS = {
    # Owner-qualified patch names resolve to their implementation owner.
    "_LineupDialog": "cli/screens/launch_sessions.py", "_SessionsScreen": "cli/screens/launch_sessions.py",
    "_sessions_list_tui": "cli/screens/launch_sessions.py", "launch_card": "cli/screens/launch_sessions.py",
    "_run_resume_gate_modal": "cli/screens/launch_sessions.py",
    "_SettingsScreen": "cli/screens/settings.py", "run_settings_screen": "cli/screens/settings.py",
    "run_models_screen": "cli/screens/models.py", "run_direct_screen": "cli/screens/direct.py",
    "run_providers_screen": "cli/screens/providers.py",
    "run_propagation_screen": "cli/screens/propagation.py",
    "_run_profile_editor": "cli/screens/profile_editor.py",
    "_transition_confirm": "cli/screens/transition.py",
    "_edit_profile": "cli/commands/profile.py", "_provider_call_tty": "cli/commands/discover.py",
    "_transition_preflight_problems": "cli/launch_flow.py", "handle_command": "cli/dispatch.py",
}


def _bindings(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class StageCOwnershipTests(unittest.TestCase):
    """Every moved definition has one owner; the facade holds none of them."""

    def test_moved_patched_names_have_only_their_owner_binding(self):
        seen = {}
        for path in (SRC / "cli").rglob("*.py"):
            for name in _bindings(path) & CLI_OWNERS.keys():
                seen.setdefault(name, []).append(path.relative_to(SRC).as_posix())
        self.assertEqual(seen, {name: [owner] for name, owner in CLI_OWNERS.items()})

    def test_the_facade_is_lazy_and_explicit(self):
        tree = ast.parse((SRC / "cli/__init__.py").read_text())
        kinds = [type(node).__name__ for node in tree.body]
        # docstring, the future import, the map, __getattr__: nothing else.
        self.assertEqual(kinds, ["Expr", "ImportFrom", "Assign", "FunctionDef"])
        self.assertEqual(tree.body[1].module, "__future__")
        self.assertEqual(tree.body[2].targets[0].id, "_COMPAT_EXPORTS")
        exports = ast.literal_eval(tree.body[2].value)
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.alias) and n.name == "*"])
        for name in ("main", "Runtime", "CLIError", "LaunchTarget", "PreparedLaunch", "LaunchPlanError",
                     "NeedsChoiceError", "build_parser", "split_passthrough", "default_asset_root"):
            self.assertIn(name, exports)
        code = ("import sys, warnings\nwarnings.simplefilter('error')\nimport claude_multi.cli as cli\n"
                "print(sorted(m for m in sys.modules if m.startswith('claude_multi')))\n"
                "import importlib\n"
                "for name, owner in cli._COMPAT_EXPORTS.items():\n"
                "    assert getattr(cli, name) is getattr(importlib.import_module(owner), name), name\n")
        result = _child(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "['claude_multi', 'claude_multi.cli']")

    def test_no_module_imports_the_facade(self):
        for path in (SRC / "cli").rglob("*.py"):
            if path.name == "__init__.py" and path.parent.name == "cli":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    if node.module == "claude_multi":
                        modules += [f"claude_multi.{a.name}" for a in node.names]
                    if node.level:
                        modules = ["<relative>"]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                with self.subTest(module=path.relative_to(SRC).as_posix()):
                    self.assertNotIn("claude_multi.cli", modules)
                    self.assertNotIn("<relative>", modules)

    def test_screens_never_import_commands_dispatch_or_entry(self):
        for path in (SRC / "cli/screens").glob("*.py"):
            imports = module_imports(path)
            with self.subTest(module=path.name):
                self.assertFalse({m for m in imports if m.startswith(("claude_multi.cli.commands",
                                                                       "claude_multi.cli.launch_flow"))})
                self.assertFalse(imports & {"claude_multi.cli.dispatch", "claude_multi.cli.entry"})
        for path in [*(SRC / "cli/commands").glob("*.py"), SRC / "cli/launch_flow.py"]:
            with self.subTest(module=path.name):
                self.assertFalse(module_imports(path) & {"claude_multi.cli.dispatch", "claude_multi.cli.entry"})


def _child(code: str, *args: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "XDG_"))}
    base.update(env or {})
    base["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run([sys.executable, "-B", "-c", code, *args], env=base, input=stdin,
                          text=True, capture_output=True, timeout=60)


ENTRY_IMPORTS = {
    "claude_multi", "claude_multi.assets", "claude_multi.cli", "claude_multi.cli.entry",
    "claude_multi.cli.parser", "claude_multi.cli.streams", "claude_multi.cli.text", "claude_multi.errors",
    "claude_multi.hooks", "claude_multi.layout", "claude_multi.lineup_files", "claude_multi.lineup_log",
    "claude_multi.paths",
    "claude_multi.platform", "claude_multi.platform.observation", "claude_multi.platform.posix_fs",
    "claude_multi.sessions", "claude_multi.state", "claude_multi.strict_json", "claude_multi.termtext",
    "claude_multi.validate",
    # The hooks' pin check: two leaves that read one contract file.
    "claude_multi.client_check", "claude_multi.pin",
}
PARSER_IMPORTS = {"claude_multi", "claude_multi.cli", "claude_multi.cli.parser", "claude_multi.cli.text",
                  "claude_multi.errors", "claude_multi.layout", "claude_multi.lineup_files",
                  "claude_multi.termtext"}
TEXT_IMPORTS = {"claude_multi", "claude_multi.cli", "claude_multi.cli.text", "claude_multi.termtext"}
# Never on any hook path: the TUI, doctor, screens, commands, launch flows.
HOOK_FORBIDDEN = ("claude_multi.tui", "claude_multi.cli.doctor", "claude_multi.cli.doctor_actions",
                  "claude_multi.cli.gateway_facts", "claude_multi.cli.dispatch", "claude_multi.cli.launch_flow",
                  "claude_multi.cli.screens", "claude_multi.cli.commands", "claude_multi.cli.selection",
                  "claude_multi.upgrade", "claude_multi.dev")
# Scope-only events (and every path that ends before Runtime): no Runtime/catalog chain.
SCOPE_ONLY_FORBIDDEN = HOOK_FORBIDDEN + (
    "claude_multi.cli.runtime", "claude_multi.cli.session_events", "claude_multi.catalog",
    "claude_multi.compiler", "claude_multi.profile", "claude_multi.scope", "claude_multi.launch",
    "claude_multi.transition", "claude_multi.proxy", "claude_multi.views", "claude_multi.render")
HOOK_CHILD = (
    "import json, sys\n"
    "from claude_multi.cli import main\n"
    "try:\n"
    "    code = main()\n"
    "finally:\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.write('\\n@@MODULES@@' + json.dumps(sorted(m for m in sys.modules if m.startswith('claude_multi'))))\n"
    "raise SystemExit(code)\n"
)


class EntryImportPinTests(unittest.TestCase):
    """Exact static and fresh-process pins for entry, parser and text."""

    def test_exact_static_entry_imports(self):
        self.assertEqual(module_imports(SRC / "cli/entry.py"), {
            "os", "sys", "typing", "claude_multi.assets", "claude_multi.errors", "claude_multi.hooks",
            "claude_multi.lineup_files", "claude_multi.sessions", "claude_multi.termtext",
            "claude_multi.cli.parser", "claude_multi.cli.streams", "claude_multi.cli.text",
        })

    def test_fresh_process_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            for module, expected in (("claude_multi.cli.entry", ENTRY_IMPORTS),
                                     ("claude_multi.cli.parser", PARSER_IMPORTS),
                                     ("claude_multi.cli.text", TEXT_IMPORTS)):
                with self.subTest(module=module):
                    result = _child(f"import sys, {module}\n"
                                    "print(json.dumps(sorted(m for m in sys.modules if m.startswith('claude_multi'))))"
                                    .replace("import sys,", "import json, sys,"), env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(set(json.loads(result.stdout)), expected)


class HookEntryImportTests(unittest.TestCase):
    """The 23 ledger hook paths through the installed entry shape.

    Each case runs ``from claude_multi.cli import main; main()`` in a fresh
    process on a private fixture root (the fixture catalog through
    CLAUDE_MULTI_ASSETS), and pins the exit status and the modules loaded.
    """

    @classmethod
    def setUpClass(cls):
        import bless
        from claude_multi import migrate, scope, sessions as sessions_mod, state as state_mod
        from test_migrate import M1, NOW, SCHEMA, _cli_environ, v3_managed

        cls.tmp = Path(tempfile.mkdtemp(prefix="cm-hook-entry-"))
        os.chmod(cls.tmp, 0o700)
        cls.environ = {**_cli_environ(cls.tmp), "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}
        cls.root = sessions_mod.state_root(cls.environ)
        state_mod.ensure_private_dir(cls.root)
        sessions_mod.write_state_marker(cls.root)
        cls.scoped = bless.FIXED_SESSION
        scope.write_scope(cls.root, cls.scoped, bless.v2_scope_plan("managed"))
        cls.valid = M1
        store = sessions_mod.SessionStore(cls.root, SCHEMA)
        store.save(migrate.convert_record(v3_managed(M1), cat=_lineup_cat(), now=NOW).record)
        cls.record_path = store.root / "sessions" / f"{M1}.json"
        # A second root whose state marker is newer than this launcher (private file).
        cls.newer_environ = {**cls.environ, "XDG_STATE_HOME": str(cls.tmp / "newer-state")}
        newer = sessions_mod.state_root(cls.newer_environ)
        state_mod.ensure_private_dir(newer)
        state_mod.atomic_write(newer / "state-version", b"99\n")
        cls.newer_open_environ = {**cls.environ, "XDG_STATE_HOME": str(cls.tmp / "newer-open")}
        newer_open = sessions_mod.state_root(cls.newer_open_environ)
        newer_open.mkdir(parents=True)
        (newer_open / "state-version").write_text("99\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def cases(self):
        rid = "22222222-2222-4222-8222-222222222222"
        unknown = "44444444-4444-4444-8444-444444444444"

        def argv(event, mid, *extra, protocol=True):
            out = ["session-event", event, "--managed-id", mid, "--launch-epoch", "0"]
            return out + (["--hook-protocol", "3"] if protocol else []) + list(extra)

        def payload(event, **fields):
            return json.dumps({"session_id": rid, "hook_event_name": event, **fields})

        start = payload("SessionStart", source="startup", model="claude-opus-4-1")
        end = payload("SessionEnd", reason="exit")
        prompt = payload("UserPromptSubmit", prompt="hello")
        s, v, e = self.scoped, self.valid, self.environ
        scope_only = SCOPE_ONLY_FORBIDDEN
        # name: (argv, stdin, environ, exit status, forbidden prefixes)
        return {
            "v3-prompt-fixture": (argv("prompt", s), prompt, e, 0, scope_only),
            "v3-premodel-fixture": (argv("premodel", s), payload("PreModelSwitch", model="x"), e, 0, scope_only),
            "v3-postmodel-fixture": (argv("postmodel", s), payload("PostModelSwitch", model="x"), e, 0, scope_only),
            "v3-subagent-fixture": (argv("subagent", s), payload("SubagentStart", agent_type="cm-x"), e, 0, scope_only),
            "v3-start-fixture": (argv("start", s), start, e, None, HOOK_FORBIDDEN),
            "v3-end-fixture": (argv("end", s), end, e, 1, HOOK_FORBIDDEN),
            "v2-start-fixture": (argv("start", s, protocol=False), start, e, 1, HOOK_FORBIDDEN),
            "v2-end-fixture": (argv("end", s, protocol=False), end, e, 1, HOOK_FORBIDDEN),
            "v3-prompt-damaged-input": (argv("prompt", s), "{not json", e, 0, scope_only),
            "v3-prompt-missing-scope": (argv("prompt", unknown), prompt, e, 0, scope_only),
            "v3-premodel-invalid-argv": (argv("premodel", s, "--bogus"), "{}", e, 0, scope_only),
            "v3-prompt-newer-marker": (argv("prompt", s), prompt, self.newer_open_environ, 0, scope_only),
            "v3-start-passthrough": (argv("start", s, "--", "x"), start, e, None, HOOK_FORBIDDEN),
            "v2-start-valid-record": (argv("start", v, protocol=False), start, e, None, HOOK_FORBIDDEN),
            "v2-end-valid-record": (argv("end", v, protocol=False), end, e, None, HOOK_FORBIDDEN),
            "v3-start-valid-record": (argv("start", v), start, e, None, HOOK_FORBIDDEN),
            "v3-end-valid-record": (argv("end", v), end, e, None, HOOK_FORBIDDEN),
            "v3-prompt-missing-scope-valid-id": (argv("prompt", v), prompt, e, 0, scope_only),
            "v3-prompt-newer-private-marker": (argv("prompt", s), prompt, self.newer_environ, 0, scope_only),
            "v2-end-passthrough-refused": (argv("end", v, "--", "x", protocol=False), end, e, 1, HOOK_FORBIDDEN),
            "v3-end-passthrough-refused": (argv("end", v, "--", "x"), end, e, 1, HOOK_FORBIDDEN),
            "v3-start-passthrough-valid-record": (argv("start", v, "--", "x"), start, e, None, HOOK_FORBIDDEN),
            "v3-prompt-passthrough": (argv("prompt", s, "--", "x"), prompt, e, 0, scope_only),
        }

    def run_case(self, argv, stdin, environ):
        return _child(HOOK_CHILD, *argv, env=environ, stdin=stdin)

    def test_every_hook_path_loads_no_tui_doctor_screen_or_command_code(self):
        cases = self.cases()
        self.assertEqual(len(cases), 23)
        for name, (argv, stdin, environ, status, forbidden) in cases.items():
            with self.subTest(case=name):
                # Shims a lifecycle case refreshed must not mask a scope-only write.
                shutil.rmtree(Path(environ["XDG_STATE_HOME"]) / "claude-multi" / "bin", True)
                result = self.run_case(argv, stdin, environ)
                stderr, _, modules = result.stderr.rpartition("\n@@MODULES@@")
                self.assertNotIn("Traceback", stderr)
                loaded = json.loads(modules)
                self.assertEqual([m for m in loaded if m.startswith(forbidden)], [], name)
                if status is not None:
                    self.assertEqual(result.returncode, status, stderr)
                if forbidden is SCOPE_ONLY_FORBIDDEN:
                    # No Runtime: no shim refresh, no store setup.
                    self.assertFalse((Path(environ["XDG_STATE_HOME"]) / "claude-multi" / "bin").exists(), name)

    def test_passthrough_parity(self):
        """Runtime first, then the refusal (stderr, exit 1, empty stdout); start ignores it."""

        before = self.record_path.read_bytes()
        for event, protocol in (("end", False), ("end", True)):
            with self.subTest(event=event, protocol=protocol):
                argv = ["session-event", event, "--managed-id", self.valid, "--launch-epoch", "0"]
                argv += ["--hook-protocol", "3"] if protocol else []
                shims = self.root / "bin"
                shutil.rmtree(shims, True)
                result = self.run_case([*argv, "--", "x"], "{}", self.environ)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith(
                    "claude-multi: passthrough arguments are accepted only for launch\n"), result.stderr)
                # Runtime was built before the refusal (shims refreshed); the record is untouched.
                self.assertTrue((shims / "claude-multi-hook").exists())
                self.assertEqual(self.record_path.read_bytes(), before)
        # 2.x start refuses too (Runtime first); protocol-3 start and prompt ignore passthrough.
        start = json.dumps({"session_id": "22222222-2222-4222-8222-222222222222",
                            "hook_event_name": "SessionStart", "source": "startup"})
        result = self.run_case(["session-event", "start", "--managed-id", self.valid, "--launch-epoch", "0",
                                "--", "x"], start, self.environ)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("passthrough arguments are accepted only for launch", result.stderr)
        result = self.run_case(["session-event", "start", "--managed-id", self.scoped, "--launch-epoch", "0",
                                "--hook-protocol", "3", "--", "x"], start, self.environ)
        self.assertNotIn("passthrough arguments", result.stderr)


def _lineup_cat():
    from test_migrate import LCAT

    return LCAT


class FacadePatchGateTests(unittest.TestCase):
    """No test mutates the facade; patches target the owners."""

    # The dedicated compatibility suite, and this gate's own scanner fixtures.
    ALLOWED = {"CompatibilityFacadeTests", "FacadePatchGateTests"}

    def test_no_facade_patch_assignment_or_setattr(self):
        hits = []
        for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)  # a test's own string escapes
                tree = ast.parse(path.read_text())
            if path.name == "test_cli_split.py":
                tree.body = [node for node in tree.body
                             if not (isinstance(node, ast.ClassDef) and node.name in self.ALLOWED)]
            for node in ast.walk(tree):
                hits.extend(f"{path.name}:{line}" for line in _facade_mutations(node))
            for token in _strings(tree):
                if re.search(r"patch\.object\(\s*cli\b|(?<![\w.])cli\.[A-Za-z_][\w.]*\s*=[^=]|setattr\(\s*cli\b"
                             r"|patch\(\s*['\"]claude_multi\.cli\.(?!(?:" + "|".join(_SUBMODULES) + r")\b)",
                             token):
                    hits.append(f"{path.name}:<embedded> {token[:60]!r}")
        self.assertEqual(hits, [])

    def test_the_scanner_finds_each_form(self):
        tree = ast.parse("mock.patch.object(cli, 'x')\nmock.patch.object(cli.tui, 'x')\ncli.x = 1\n"
                         "setattr(cli, 'x', 1)\nmock.patch('claude_multi.cli.main')\n"
                         "mock.patch('claude_multi.cli.entry.main')\n")
        self.assertEqual(len([line for node in ast.walk(tree) for line in _facade_mutations(node)]), 5)


_SUBMODULES = sorted({p.stem if p.name != "__init__.py" else p.parent.name
                      for p in (SRC / "cli").rglob("*.py")} - {"cli", "__main__"})


def _chain_root(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _facade_mutations(node):
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "object" and node.args and _chain_root(node.args[0]) == "cli":
            yield node.lineno
        elif name in ("setattr", "delattr") and node.args and _chain_root(node.args[0]) == "cli":
            yield node.lineno
        elif name == "patch" and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and node.args[0].value.startswith("claude_multi.cli."):
            head = node.args[0].value.removeprefix("claude_multi.cli.").split(".")[0]
            if head not in _SUBMODULES:
                yield node.lineno
    elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and _chain_root(target) == "cli":
                yield node.lineno


def _strings(tree):
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class CompatibilityFacadeTests(unittest.TestCase):
    """The dedicated compatibility suite: old names resolve to their one owner object."""

    def test_names_resolve_to_the_owner_objects(self):
        from claude_multi.cli import dispatch, entry, launch_flow, parser, runtime
        from claude_multi.cli.screens import launch_sessions

        self.assertIs(cli.main, entry.main)
        self.assertIs(cli.handle_command, dispatch.handle_command)
        self.assertIs(cli.launch_card, launch_sessions.launch_card)
        self.assertIs(cli._resume_flow, launch_flow._resume_flow)
        self.assertIs(cli.build_parser, parser.build_parser)
        self.assertIs(cli.Runtime, runtime.Runtime)
        with self.assertRaises(AttributeError):
            cli.no_such_name  # noqa: B018

    def test_facade_patch_does_not_reach_the_owner(self):
        # Why tests patch owners: a facade attribute is a separate binding.
        from claude_multi.cli import dispatch

        with mock.patch.object(cli, "handle_command", "sentinel"):
            self.assertIsNot(dispatch.handle_command, "sentinel")
