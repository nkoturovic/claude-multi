"""Product paths for operator messages (gateway paths stay HOME-relative)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import os


def home(environ: Mapping[str, str]) -> Path:
    return Path(environ["HOME"]) if environ.get("HOME") else Path.home()


def display(path: Path | str, environ: Mapping[str, str]) -> str:
    absolute = Path(path).absolute()
    try:
        relative = absolute.relative_to(home(environ).absolute())
    except ValueError:
        return str(absolute)
    return "~" if relative == Path(".") else f"~/{relative}"


def state_root(environ: dict[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/claude-multi``, or ``~/.local/state/claude-multi``.

    A relative ``XDG_STATE_HOME`` is invalid by the XDG rules and ignored, as
    the installer ignores it, so both name the same state root.
    """

    env = os.environ if environ is None else environ
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path(env.get("HOME", str(Path.home()))) / ".local" / "state"
    return base / "claude-multi"


def config_root(environ: dict[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/claude-multi``, or ``~/.config/claude-multi``.

    A relative ``XDG_CONFIG_HOME`` is invalid by the XDG rules and ignored,
    like a relative ``XDG_STATE_HOME`` (:func:`state_root`) and as the
    installer and the service unit directory do.
    """

    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path(env.get("HOME", str(Path.home()))) / ".config"
    return base / "claude-multi"



def gateway_config_dir(environ: Mapping[str, str]) -> Path:
    return home(environ) / ".config" / "claude-multi"


def secret_env_default(environ: Mapping[str, str]) -> Path:
    """The API-key file when nothing selects another: HOME-relative, so the
    launcher and the gateway (whose service unsets the override) agree."""

    return gateway_config_dir(environ) / "secrets" / "provider-keys.env"


def secret_pointer_path(environ: Mapping[str, str]) -> Path:
    """The optional pointer that selects an existing key file
    (``claude-multi setup --keys-file PATH``); HOME-relative like the default."""

    return gateway_config_dir(environ) / "secret-file.json"


def choices_path(environ: Mapping[str, str]) -> Path:
    return config_root(dict(environ)) / "choices.json"


def data_root(environ: Mapping[str, str]) -> Path:
    return home(environ) / ".local" / "share" / "claude-multi"


def installer_receipt(environ: Mapping[str, str]) -> Path:
    """The installer's receipt: the launcher files and PATH lines it wrote."""

    return data_root(environ) / "install" / "installer.json"


def retained_root(environ: Mapping[str, str]) -> Path:
    return data_root(environ) / "pinned-clients"


def upstream_versions_dir(environ: Mapping[str, str]) -> Path:
    return home(environ) / ".local" / "share" / "claude" / "versions"


def native_projects(home_dir: Path | str, environ: Mapping[str, str] | None = None) -> Path:
    """Current native metadata root only; historical roots are not recorded.

    Never read transcript contents. An explicit current CLAUDE_CONFIG_DIR
    selects the metadata directory, not evidence about an older launch.
    """

    explicit = (environ or {}).get("CLAUDE_CONFIG_DIR")
    return (Path(explicit) if explicit else Path(home_dir) / ".claude") / "projects"


def project_agents(project: Path | str) -> Path:
    return Path(project) / ".claude" / "agents"


def claude_settings_dir(environ: Mapping[str, str]) -> Path:
    """The user settings directory the managed client reads: ``$HOME/.claude``.

    Never ``$CLAUDE_CONFIG_DIR``: every managed launch unsets it
    (``compiler.V2_ENV_UNSET``), so a value in the launcher's environment
    names a directory the managed client never reads. Every launcher decision or report about the settings a managed
    session applies (NO_PROXY layers, the permission-mode layers, the
    settings radar, saved selectors, cleanup days) reads this directory.
    """

    return home(environ) / ".claude"
