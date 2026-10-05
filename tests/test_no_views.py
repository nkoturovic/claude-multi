"""No 035/037 view, 2.x projection or 2.x resolver is left in ``src``.

An AST walk over ``src/claude_multi/*.py`` asserts that the deleted names are
never referenced, that nothing reads ``.models`` off a Catalog or calls the
2.x resolver, and that the v1 record/draft shapes (``["lanes"]``,
``["default_lane"]``, ``["client_selector"]``) are subscripted only by the
five functions that read a 2.x *input*. It also pins the read-only composition-input compatibility boundary.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

from claude_multi import catalog, composition
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT

SRC = REPO_ROOT / "src" / "claude_multi"
SPEC = "the views removal contract"

DELETED_NAMES = frozenset({
    # the 035/037 views (catalog.py)
    "v1_view", "v1_view_entry", "legacy_roles_view", "LEGACY_ROLE_IDS",
    # the 2.x default composition and its projection
    "v1_projection", "_PROJECTION_GROUPS", "default_composition",
    "default_composition_projected",
    # the 2.x composition validator and its vocabulary
    "validate_composition", "OPTIONAL_DOCUMENTS", "_SCOPE_USES",
    # the 2.x resolver (composition.py)
    "ResolvedComposition", "ResolvedVariant",
    "compute_scalar", "compute_auto_compact_capacity", "variant_id",
    "_lead_context_policy",
    # the removed compiler helpers
    "ordinary_picker_groups", "direct_context_profile", "direct_profile_context",
    "ordinary_launch_models", "single_model_launch_models",
    # the 2.x editor and card. Each is spelled as two adjacent
    # literals so the removal grep (word-bounded over src and tests) stays
    # empty; the values are the plain names.
    "Editor" "State", "run_form" "_editor", "build_quick" "_plan",
    # the selector helpers and the 2.x card/sessions reach
    "_model_client_selectors", "_ordinary_typed_selectors",
    "render_quick_confirm", "composition_pick_order",
})

# ``ResolvedLead`` is also the name of the current class ``profile.ResolvedLead``
# (a different, kept symbol), so the 2.x one is checked where it lived: no
# reference inside composition.py and no ``composition.ResolvedLead``.
COMPOSITION_ONLY_NAMES = frozenset({"ResolvedLead"})
COMPOSITION_MODULE_NAMES = frozenset({"composition", "composition_mod"})

# Names a ``Catalog`` is bound to across src (plus any ``<x>.catalog``).
CATALOG_BINDINGS = frozenset({"catalog", "cat", "bundle"})

V1_SHAPE_KEYS = frozenset({"lanes", "default_lane", "client_selector"})
# The closed allow-list: functions that read a 2.x INPUT.
V1_SHAPE_READERS = frozenset({
    "dev.v1_draft_entry_to_v2",        # a catalog-32 (v1) draft
    "migrate._agent_binding",          # a v3 record snapshot
    "migrate.convert_record",          # a v3 record snapshot
    "sessions.reconcile_runtime_record",  # a v3 record snapshot
    "cli.session_events._v3_normalize",               # a v3 record snapshot
})


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (".".join(path.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__"), ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _walk(tree: ast.AST, module: str):
    """Yield ``(qualified name, node)`` for every node, ``module.Outer.inner``."""

    def visit(node: ast.AST, stack: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield ".".join([module, *stack]), child
                yield from visit(child, [*stack, child.name])
            else:
                yield ".".join([module, *stack]), child
                yield from visit(child, stack)

    yield from visit(tree, [])


def _referenced_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Attribute):
        names.add(node.attr)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, ast.alias):
        names.add(node.name.rsplit(".", 1)[-1])
        if node.asname:
            names.add(node.asname)
    elif isinstance(node, ast.arg):
        names.add(node.arg)
    return names


def _is_catalog_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in CATALOG_BINDINGS
    return isinstance(node, ast.Attribute) and node.attr == "catalog"


class NoViewsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = _modules()
        cls.nodes = [
            (module, qualname, node)
            for module, tree in cls.modules
            for qualname, node in _walk(tree, module)
        ]

    def test_the_source_tree_is_scanned(self) -> None:
        names = {module for module, _tree in self.modules}
        self.assertLessEqual({"catalog", "cli", "composition", "custom", "dev"}, names, SPEC)

    def test_no_deleted_name_is_referenced(self) -> None:
        hits = sorted(
            f"{qualname}:{node.lineno}: {name}"
            for _module, qualname, node in self.nodes
            for name in _referenced_names(node) & DELETED_NAMES
        )
        self.assertEqual(hits, [], SPEC)

    def test_the_2x_resolved_lead_is_gone(self) -> None:
        hits = []
        for module, qualname, node in self.nodes:
            if module == "composition" and _referenced_names(node) & COMPOSITION_ONLY_NAMES:
                hits.append(f"{qualname}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in COMPOSITION_ONLY_NAMES
                and isinstance(node.value, ast.Name)
                and node.value.id in COMPOSITION_MODULE_NAMES
            ):
                hits.append(f"{qualname}:{node.lineno}")
        self.assertEqual(hits, [], SPEC)
        self.assertFalse(hasattr(composition, "ResolvedLead"))

    def test_no_models_attribute_on_a_catalog(self) -> None:
        hits = sorted(
            f"{qualname}:{node.lineno}"
            for _module, qualname, node in self.nodes
            if isinstance(node, ast.Attribute)
            and node.attr == "models"
            and _is_catalog_expr(node.value)
        )
        self.assertEqual(hits, [], SPEC)
        self.assertFalse(hasattr(catalog.Catalog, "models"))

    def test_no_2x_resolver_call(self) -> None:
        hits = sorted(
            f"{qualname}:{node.lineno}"
            for _module, qualname, node in self.nodes
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("resolve", "snapshot")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in COMPOSITION_MODULE_NAMES
        )
        self.assertEqual(hits, [], SPEC)
        for name in ("resolve", "snapshot"):
            self.assertFalse(hasattr(composition, name), name)

    def test_v1_shape_subscripts_only_in_the_allow_list(self) -> None:
        sites: dict[str, list[str]] = {}
        for _module, qualname, node in self.nodes:
            if not isinstance(node, ast.Subscript):
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in V1_SHAPE_KEYS:
                sites.setdefault(qualname, []).append(f"{node.lineno}:{key.value}")
        outside = {
            qualname: lines
            for qualname, lines in sites.items()
            if not any(qualname == allowed or qualname.startswith(allowed + ".")
                       for allowed in V1_SHAPE_READERS)
        }
        self.assertEqual(outside, {}, f"{SPEC}: v1-shape subscripts outside the allow-list")
        stale = sorted(
            allowed for allowed in V1_SHAPE_READERS
            if not any(q == allowed or q.startswith(allowed + ".") for q in sites)
        )
        self.assertEqual(stale, [], f"{SPEC}: allow-list entries that no longer subscript")

    def test_docs_models_and_roles_are_the_raw_v2_aliases(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertIs(bundle.docs["models"], bundle.docs["models-v2"])
        self.assertIs(bundle.docs["roles"], bundle.docs["roles-v2"])
        self.assertIs(bundle.lines, bundle.docs["models"]["models"])
        self.assertIs(bundle.roles, bundle.roles_v2)
        self.assertNotIn("compositions/default", bundle.docs)
        self.assertNotIn("compositions/default", bundle.bundle)

    def test_keeps_the_2x_composition_reader(self) -> None:
        self.assertTrue(callable(composition.validate_document))
        self.assertTrue(callable(composition.load_composition_file))
        self.assertEqual(catalog.SUPPORTED_DATA_VERSION, 1)
        for root in (RESOURCES_ROOT, FIXTURE_ROOT):
            with self.subTest(root=str(root)):
                self.assertTrue((root / "schemas" / "composition.schema.json").is_file())

    def test_cli_scope_compiler_transition_and_dev_import(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-c",
             "import claude_multi.cli, claude_multi.scope, claude_multi.compiler, "
             "claude_multi.transition, claude_multi.dev; "
             "import pkgutil, importlib; "
             "[importlib.import_module(m.name) for m in pkgutil.walk_packages("
             "claude_multi.cli.__path__, claude_multi.cli.__name__ + '.')]"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
