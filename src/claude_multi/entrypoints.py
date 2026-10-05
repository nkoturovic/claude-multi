"""The three claude-multi commands and how each launch states its environment.

Every way of starting a command ends here: the console scripts an installed
package provides (``[project.scripts]`` in ``pyproject.toml``), the
source-checkout launchers in ``bin/`` and the wrappers of the Nix package.
The programs are the same; only who states the launch environment differs:

- **An installation wrapper** (the Nix package's) sets the channel, the
  resources, the hook command and the gateway attestation itself, then runs
  ``python3 -P -m claude_multi.entrypoints NAME``. Nothing is changed.
- **A source launcher** (``bin/NAME`` inside a source tree: ``nix/package.nix``
  and ``src/claude_multi/__init__.py`` beside it) drops an inherited
  resource override, so the checkout's own resources are read, and names the
  ``source`` channel. A copy of a launcher outside a source tree changes
  nothing: the installation that placed it there states its environment.
- **A console script** of an installed package (a wheel in a virtual
  environment, for example) has no wrapper: an inherited channel, resource
  override or hook command belongs to another installation and is dropped.
  Its channel stays unknown (an unknown channel neither claims nor refuses
  the state root) and its hook command is this installation's own
  ``claude-multi`` (:func:`claude_multi.layout.entry_point`).

A channel is therefore never inherited and never made up: it is ``source``
only for a verified source tree, and otherwise whatever the installation's
own wrapper set.

Standard library only, and cheap to import: every lifecycle hook passes
through here before the command module loads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, MutableMapping, Sequence

CHANNEL_ENV = "CLAUDE_MULTI_CHANNEL"
ASSETS_ENV = "CLAUDE_MULTI_ASSETS"
HOOK_COMMAND_ENV = "CLAUDE_MULTI_HOOK_COMMAND"
SOURCE_CHANNEL = "source"
# A source tree: the Nix package recipe and this package's source side by
# side. A checkout and the tree the Nix sandbox checks stage both carry them;
# an installed package never does.
SOURCE_MARKERS = ("nix/package.nix", "src/claude_multi/__init__.py")
# What a wrapper states for its installation; a console script without one
# never takes these from its caller.
WRAPPER_VARIABLES = (CHANNEL_ENV, ASSETS_ENV, HOOK_COMMAND_ENV)
NAMES = ("claude-multi", "claude-multi-proxy", "claude-multi-dev")


def is_source_tree(root: Path | str) -> bool:
    """True when ``root`` holds :data:`SOURCE_MARKERS`."""

    base = Path(root)
    return all((base / marker).is_file() for marker in SOURCE_MARKERS)


def prepare_source(root: Path | str, environ: MutableMapping[str, str] | None = None) -> bool:
    """The source launchers' rule: inside a source tree, drop an inherited
    resource override and name the ``source`` channel; elsewhere change
    nothing. Returns whether ``root`` is a source tree."""

    env = os.environ if environ is None else environ
    if not is_source_tree(root):
        return False
    env.pop(ASSETS_ENV, None)
    env[CHANNEL_ENV] = SOURCE_CHANNEL
    return True


def prepare_console(environ: MutableMapping[str, str] | None = None) -> None:
    """The console scripts' rule (no wrapper states anything): drop every
    inherited :data:`WRAPPER_VARIABLES` value. A package running from a
    source tree (an editable install) is a source launch."""

    env = os.environ if environ is None else environ
    from claude_multi import layout

    tree = layout.installation()
    if tree is not None and prepare_source(tree, env):
        return
    for name in WRAPPER_VARIABLES:
        env.pop(name, None)


# ------------------------------------------------------------ the programs


def _identity() -> int:
    # The release identity line: version, catalog, gateway and Claude Code pins.
    from claude_multi import identity

    print(identity.version_line())
    return 0


def _claude_multi(argv: list[str]) -> int:
    if argv == ["--version"]:
        return _identity()
    from claude_multi.cli.entry import main

    return main(argv)


def _claude_multi_proxy(argv: list[str]) -> int:
    # ``run`` and the sign-ins change into the gateway's working directory
    # before exec, so the gateway never loads a .env from the caller's
    # directory; this is the only caller that passes a real chdir.
    from claude_multi.proxy import main

    return main(argv, chdir=os.chdir)


def _claude_multi_dev(argv: list[str]) -> int:
    from claude_multi.dev import main

    return main(argv)


PROGRAMS: dict[str, Callable[[list[str]], int]] = {
    "claude-multi": _claude_multi,
    "claude-multi-proxy": _claude_multi_proxy,
    "claude-multi-dev": _claude_multi_dev,
}


def run(name: str, argv: Sequence[str] | None = None) -> int:
    """Run the command ``name`` with ``argv`` (default: this process's
    arguments) in the environment as it stands."""

    program = PROGRAMS[name]
    return program(list(sys.argv[1:] if argv is None else argv))


def run_source(name: str, root: Path | str, argv: Sequence[str] | None = None) -> int:
    """A source launcher at ``root/bin/name`` (:func:`prepare_source`)."""

    prepare_source(root)
    return run(name, argv)


def run_console(name: str, argv: Sequence[str] | None = None) -> int:
    """A console script of an installed package (:func:`prepare_console`)."""

    prepare_console()
    return run(name, argv)


# ------------------------------------------------------------ console scripts


def claude_multi() -> int:
    return run_console("claude-multi")


def claude_multi_proxy() -> int:
    return run_console("claude-multi-proxy")


def claude_multi_dev() -> int:
    return run_console("claude-multi-dev")


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -P -m claude_multi.entrypoints NAME [ARGS ...]``: the
    wrappers' form; the environment is the wrapper's."""

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in PROGRAMS:
        print(f"usage: python3 -P -m claude_multi.entrypoints {{{','.join(NAMES)}}} [ARGS ...]",
              file=sys.stderr)
        return 2
    return run(args[0], args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
