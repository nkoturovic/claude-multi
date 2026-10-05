"""Single source of the asset roots the suite reads.

- ``SHIPPED_ROOT``: the shipped trusted catalog, i.e. the packaged resources
  (``_layout.RESOURCES_ROOT``). Only deliberate catalog-release tests read
  it: shape/evidence tests, the upgrade-synced version literals, the
  real-binary probes, packaging.
- ``FIXTURE_ROOT``: the frozen asset root under ``tests/fixtures/assets``
  (``tests/fixtures/build_fixture_assets.py`` documents how it was cut). It
  is never regenerated at test time, so a shipped catalog change moves no
  behavioural test.
- ``GOLDENS_ROOT``: checked-in golden bytes (generated from the fixture).

Every other repository path (the root, ``src/``, patches, ``nix/``,
``docs/``) comes from ``tests/_layout.py``.

``uses_shipped_catalog`` marks a test class/function that must read the
shipped catalog. Used bare it is a marker; with a module-global name it
also rebinds that global to ``SHIPPED_ROOT`` for the duration of each test.
"""

from __future__ import annotations

import functools
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable

from _layout import FIXTURES_ROOT, REPO_ROOT, RESOURCES_ROOT
SHIPPED_ROOT = RESOURCES_ROOT
FIXTURE_ROOT = FIXTURES_ROOT / "assets"
GOLDENS_ROOT = REPO_ROOT / "tests" / "goldens"

_MARK = "__uses_shipped_catalog__"


def _rebinding(function: Callable[..., Any], module: str, name: str) -> Callable[..., Any]:
    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        namespace = vars(sys.modules[module])
        saved = namespace[name]
        namespace[name] = SHIPPED_ROOT
        try:
            return function(*args, **kwargs)
        finally:
            namespace[name] = saved

    return wrapper


def uses_shipped_catalog(target: Any = None, *, rebind: str | None = None) -> Any:
    """Mark (and optionally rebind a root global for) a shipped-catalog test.

    ``@uses_shipped_catalog`` only marks. ``@uses_shipped_catalog(rebind=
    "CATALOG_ROOT")`` additionally points the decorated module's global at
    ``SHIPPED_ROOT`` while each test method (and ``setUp``) runs.
    """

    def apply(obj: Any) -> Any:
        setattr(obj, _MARK, True)
        if rebind is None:
            return obj
        module = obj.__module__
        if inspect.isclass(obj):
            # dir() includes inherited setUp/test methods (e.g. a shared
            # base-class setUp that builds a Runtime from the root global).
            names = [
                attr
                for attr in dir(obj)
                if (attr.startswith("test") or attr in ("setUp", "tearDown"))
                and callable(getattr(obj, attr))
            ]
            for attr in names:
                setattr(obj, attr, _rebinding(getattr(obj, attr), module, rebind))
            return obj
        return _rebinding(obj, module, rebind)

    if target is None:
        return apply
    return apply(target)


def is_shipped_catalog_test(obj: Any) -> bool:
    return bool(getattr(obj, _MARK, False))


# A shape-valid loopback gateway token for runtime HOMEs (never a real one).
FIXTURE_GATEWAY_TOKEN = "0123456789abcdef" * 4

# The launcher version the frozen fixture records. A Runtime names the
# running release (the packaged resources) in what it writes; a golden that
# must not move with that release passes this to ``Runtime(release_version=)``.
FIXTURE_LAUNCHER_VERSION = json.loads((FIXTURE_ROOT / "version.json").read_text())["launcher_version"]


def served_selectors(
    root: Path = FIXTURE_ROOT, *, include_continuity: bool = False
) -> frozenset[str]:
    """Every public selector the catalog at ``root`` can serve.

    Passthrough route names plus every line selector base (``[1m]``
    stripped) over the v2 lines — ``status: new`` lines included, since the
    gateway serves New lines — the fixture stand-in for the running
    gateway's ``/v1/models`` so no test reaches 127.0.0.1:8317.
    ``include_continuity`` adds the continuity seed aliases (every retired
    selector base); ``CLITestCase`` keeps the default: the fixture's
    retired line rides ``meta``, which it does not credential.
    """

    from claude_multi import catalog

    bundle = catalog.load_catalog(root)
    selectors = {
        route["name"]
        for provider in bundle.providers.values()
        for route in provider.get("passthrough_routes", [])
    }
    selectors.update(
        selector.removesuffix("[1m]")
        for line in bundle.lines.values()
        for _level, selector, _contract in catalog.line_selectors(line)
    )
    if include_continuity:
        from claude_multi import continuity

        selectors.update(continuity.seed_only(bundle)["aliases"])
    return frozenset(selectors)


__all__ = [
    "FIXTURE_GATEWAY_TOKEN",
    "FIXTURE_LAUNCHER_VERSION",
    "FIXTURE_ROOT",
    "GOLDENS_ROOT",
    "SHIPPED_ROOT",
    "is_shipped_catalog_test",
    "served_selectors",
    "uses_shipped_catalog",
]
