"""The first-run checks: nine ordered, local checks with one fix each
(``claude-multi doctor --first-run``, Get started's review step).

Every check runs, in order, on local facts only; a check whose prerequisite
failed is ``waiting``. Nothing is written and no provider is called.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from claude_multi import errors, paths, secret_store
from claude_multi.platform import posix_fs
from claude_multi.setup import model, texts

MIN_FREE_BYTES = 600 * 1024 * 1024
SUPPORTED_PLATFORMS = ("linux", "darwin")
MARKS = {"ok": "✓", "fail": "✗", "attention": "!", "waiting": "·"}
TITLES = {"computer": "This computer", "files": "Your files", "policy": "Policy", "claude": "Claude Code {v}",
          "gateway": "Local gateway", "providers": "Providers", "models": "Models", "profile": "Profile",
          "hooks": "Session hooks"}
ORDER = tuple(TITLES)
_LINE = re.compile(r":(\d+): ")


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    state: str  # ok | fail | attention | waiting
    detail: str
    fix: model.Fix | None
    required: bool = True


def _fix(cli: str, tui: str | None = None) -> model.Fix:
    return model.Fix(tui, cli, tuple(cli.split()) if cli.startswith("claude-multi ") else ())


def _gb(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GB"


def _computer(runtime: Any) -> Check:
    from claude_multi.platform import mounts
    from claude_multi.setup import external

    facts = external.platform_facts(runtime)
    system = {"linux": "Linux", "darwin": "macOS"}.get(sys.platform, sys.platform)
    parts = [f"{system} {facts['arch']}"]
    problems: list[tuple[str, model.Fix | None]] = []
    if not sys.platform.startswith(SUPPORTED_PLATFORMS):
        problems.append((f"{system} is not supported", _fix("see Supported systems in the claude-multi guide")))
    try:
        release = (Path(runtime.proc_root) / "sys/kernel/osrelease").read_text(errors="replace")
    except (OSError, TypeError):
        release = ""
    if facts.get("wsl") and "Microsoft" in release and "WSL2" not in release and "microsoft-standard" not in release:
        problems.append(("WSL 1 is not supported — use WSL 2", _fix("wsl --set-version <distro> 2")))
    for label, root in (("state", runtime.session_store.root), ("config", paths.config_root(dict(runtime.environ)))):
        try:
            fstype = mounts.filesystem_type(root, platform=sys.platform)
        except OSError:
            fstype = None
        if fstype in mounts.NETWORK_TYPES:
            problems.append((f"the {label} folder {paths.display(root, runtime.environ)} is on a network "
                             f"filesystem ({fstype})", _fix("keep claude-multi's folders on a local disk")))
    data = paths.data_root(runtime.environ)
    free = external.free_bytes(data)
    if free is not None:
        parts.append(f"{_gb(free)} free")
        if free < MIN_FREE_BYTES:
            shown = paths.display(data, runtime.environ)
            problems.append((f"less than 600 MB free under {shown}",
                             _fix(f"free at least 600 MB under {shown}")))
    parts.append("local disk" if not any("network" in text for text, _fix_ in problems) else "network disk")
    if problems:
        return Check("computer", TITLES["computer"], "fail", problems[0][0], problems[0][1])
    return Check("computer", TITLES["computer"], "ok", " · ".join(parts), None)


def _entry_problem(path: Path, *, directory: bool, environ: Any) -> tuple[str, model.Fix] | None:
    shown = paths.display(path, environ)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{shown} cannot be read ({exc.strerror})", _fix(f"check {shown}")
    if stat.S_ISLNK(info.st_mode):
        return f"{shown} is a symlink", _fix(f"replace the link {shown} with a real {'folder' if directory else 'file'}")
    if directory and not stat.S_ISDIR(info.st_mode):
        return f"{shown} is not a folder", _fix(f"move {shown} aside")
    if not directory and not stat.S_ISREG(info.st_mode):
        return f"{shown} is not a regular file", _fix(f"move {shown} aside")
    if not posix_fs.owned_by_caller(info):
        return f"{shown} belongs to another user", _fix(f"chown $USER {shown}")
    if posix_fs.shared_mode_bits(info):
        mode = "700" if directory else "600"
        return f"{shown} is readable by others", _fix(f"chmod {mode} {shown}")
    return None


def _files(runtime: Any) -> Check:
    environ = {**runtime.environ, "HOME": str(runtime.home)}
    roots = dict.fromkeys([paths.gateway_config_dir(environ), paths.config_root(dict(environ)),
                           runtime.session_store.root])
    for root in roots:
        problem = _entry_problem(Path(root), directory=True, environ=environ)
        if problem is not None:
            return Check("files", TITLES["files"], "fail", problem[0], problem[1])
    try:
        location = secret_store.key_file_location(environ)
    except secret_store.SecretStoreError as exc:
        return Check("files", TITLES["files"], "fail", str(exc), _fix(exc.remedy or "fix the key file pointer"))
    key_file = location.path
    problem = _entry_problem(key_file, directory=False, environ=environ)
    if problem is not None:
        return Check("files", TITLES["files"], "fail", problem[0], problem[1])
    if os.path.lexists(key_file):
        try:
            secret_store.read_env_file(key_file)
        except secret_store.SecretStoreError as exc:
            shown = paths.display(key_file, environ)
            found = _LINE.search(str(exc))
            if found is not None:
                text = f"{shown} line {found.group(1)} is not NAME=value — fix or remove that line"
                return Check("files", TITLES["files"], "fail", text, _fix(text))
            return Check("files", TITLES["files"], "fail", f"{shown} cannot be used", _fix(f"chmod 600 {shown}"))
    return Check("files", TITLES["files"], "ok", "private folders for your settings", None)


def _policy(runtime: Any) -> Check:
    from claude_multi.setup import external

    findings = external.managed_policy_findings(runtime)
    blocking = [item for item in findings if item[0] == "block"]
    if blocking:
        return Check("policy", TITLES["policy"], "fail", blocking[0][1], blocking[0][2])
    if findings:
        return Check("policy", TITLES["policy"], "attention", findings[0][1], findings[0][2])
    return Check("policy", TITLES["policy"], "ok", "no administrator setting blocks claude-multi", None)


def _claude(runtime: Any, verified: Any = None) -> Check:
    """The owned copy by its pinned size and sha256 (``verified``: that
    check's result, when the caller already has it)."""

    from claude_multi.setup import external

    status = verified if verified is not None else external.claude_verified(runtime)
    title = TITLES["claude"].format(v=status.version)
    if status.state == "verified":
        return Check("claude", title, "ok", "verified copy", None)
    return Check("claude", title, "fail", status.detail, status.fix or external.CLAUDE_FIX)


def _gateway(runtime: Any) -> Check:
    from claude_multi import launch
    from claude_multi.setup import external

    status = external.gateway_status(runtime)
    if status.state in ("blocked", "setup"):
        # A home with no port recorded yet: no provider change can be
        # served there until the gateway step records it.
        return Check("gateway", TITLES["gateway"], "fail", status.detail, status.fix or external.GATEWAY_FIX)
    key = paths.gateway_config_dir({"HOME": str(runtime.home)}) / "api-key"
    if os.path.lexists(key):
        try:
            launch.read_gateway_token(runtime.catalog.docs["gateway"], home=runtime.home)
        except (launch.GatewayKeyError, errors.ClaudeMultiError, OSError) as exc:
            return Check("gateway", TITLES["gateway"], "fail", f"the local gateway key is unreadable: {exc}",
                         external.GATEWAY_FIX)
    try:
        runtime.operator_render_plan()
    except (errors.ClaudeMultiError, OSError, ValueError) as exc:
        return Check("gateway", TITLES["gateway"], "fail", f"the gateway configuration does not render: {exc}",
                     _fix("claude-multi providers validate"))
    detail = "running" if status.state == "running" else "starts when needed"
    return Check("gateway", TITLES["gateway"], "ok", detail, None)


def _providers(runtime: Any, conns: Sequence[Any]) -> Check:
    from claude_multi.setup import signin

    linked = [item for item in conns if item.state == "connected"]
    if not linked:
        return Check("providers", TITLES["providers"], "fail", "nothing connected yet",
                     _fix("claude-multi setup --step providers", "A on Get started"))
    for item in linked:
        if item.kind == "account" and item.credential in signin.LOGIN_COMMANDS \
                and signin.current_ack(runtime.environ, str(item.credential)) is None:
            return Check("providers", TITLES["providers"], "attention",
                         f"{item.label} is signed in without a current personal-use confirmation",
                         _fix(f"claude-multi providers sign-in {item.provider_id}", "L on the provider"))
    return Check("providers", TITLES["providers"], "ok", " · ".join(item.label for item in linked), None)


def _models(runtime: Any, conns: Sequence[Any]) -> Check:
    linked = {item.provider_id: item for item in conns if item.state == "connected"}
    try:
        lcat = runtime.lineup_catalog()
        eff = runtime.current_effective()
    except (errors.ClaudeMultiError, OSError, ValueError) as exc:
        return Check("models", TITLES["models"], "fail", str(exc), _fix("claude-multi doctor"))
    from claude_multi import settings as settings_mod

    leads: dict[str, int] = {}
    for key, entry in lcat.lines.items():
        provider = entry.get("provider")
        if provider in linked and "lead" in (entry.get("capabilities") or ()) \
                and settings_mod.line_offered(key, entry, eff):
            leads[provider] = leads.get(provider, 0) + 1
    if not leads:
        first = sorted(linked)[0]
        return Check("models", TITLES["models"], "fail", "no connected provider has a model that can lead",
                     _fix(f"claude-multi discover {first}", "G → A adds models"))
    served = [linked[pid].served for pid in leads]
    if all(text.startswith("0/") for text in served):
        return Check("models", TITLES["models"], "fail", "the gateway serves none of them yet",
                     _fix("claude-multi providers apply", "G → P applies"))
    return Check("models", TITLES["models"], "ok", f"{sum(leads.values())} model(s) can lead", None)


def _profile(runtime: Any) -> Check:
    from claude_multi.setup import defaults

    try:
        choice = defaults.resolve_default(runtime)
    except (errors.ClaudeMultiError, OSError, ValueError) as exc:
        return Check("profile", TITLES["profile"], "fail", str(exc), _fix("claude-multi setup --step profile"))
    if choice.ready:
        return Check("profile", TITLES["profile"], "ok", f"{choice.name} ({choice.reason_text})", None)
    return Check("profile", TITLES["profile"], "fail", f"{choice.name} is not connected here",
                 _fix("claude-multi setup --step profile", "Enter on step 6"))


def _hooks(runtime: Any) -> Check:
    from claude_multi import scope

    root = runtime.session_store.root
    shims = [scope.hook_shim_path(root), Path(root) / scope.HOOK_SHIM_V3_RELATIVE,
             Path(root) / scope.GATEWAY_TOKEN_SHIM_RELATIVE]
    for shim in shims:
        shown = paths.display(shim, runtime.environ)
        try:
            info = os.lstat(shim)
        except OSError:
            return Check("hooks", TITLES["hooks"], "fail", f"{shown} is missing", _fix("claude-multi doctor --repair-all"))
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not posix_fs.owned_by_caller(info)
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            return Check("hooks", TITLES["hooks"], "fail", f"{shown} is not a private file you own",
                         _fix("claude-multi doctor --repair-all"))
    return Check("hooks", TITLES["hooks"], "ok", "the hook and gateway-key helpers are in place", None)


def _waiting(check_id: str, what: str) -> Check:
    return Check(check_id, TITLES[check_id], "waiting", texts.FIRSTRUN_WAITS.format(what=what), None)


def checks(runtime: Any, *, connections: Sequence[Any] | None = None, claude: Any = None) -> tuple[Check, ...]:
    """The nine checks, in order (all of them run). ``claude`` is the
    verified Claude Code status when the caller already checked it."""

    from claude_multi.setup import status

    conns = tuple(connections) if connections is not None else status.connections(runtime)
    result = [_computer(runtime), _files(runtime), _policy(runtime), _claude(runtime, claude), _gateway(runtime)]
    providers = _providers(runtime, conns)
    result.append(providers)
    if providers.state == "fail":
        result += [_waiting("models", "a provider"), _waiting("profile", "a provider")]
    else:
        models = _models(runtime, conns)
        result.append(models)
        result.append(_waiting("profile", "a model that can lead") if models.state == "fail" else _profile(runtime))
    result.append(_hooks(runtime))
    return tuple(result)


def info_lines(runtime: Any) -> tuple[str, ...]:
    """The fixed info line plus the expected-by-design facts (gateway
    management in this build, where the gateway's logs live)."""

    from claude_multi import endpoint, management

    lines = [texts.FIRSTRUN_INFO]
    try:
        managed = management.allowlist_build(runtime.environ)
    except (errors.ClaudeMultiError, OSError):
        managed = False
    lines.append("Gateway quota reads: " + ("available" if managed else "not part of this build (expected)"))
    try:
        backend = endpoint.read_config(runtime.home)
        systemd = backend is not None and backend.backend == endpoint.SYSTEMD
    except (errors.ClaudeMultiError, OSError, AttributeError):
        systemd = False
    lines.append("Gateway logs: " + ("the service journal" if systemd
                                     else "files under the state folder (claude-multi gateway logs)"))
    return tuple(lines)


def failing(items: Sequence[Check]) -> list[Check]:
    return [item for item in items if item.required and item.state in ("fail", "waiting")]


def render_lines(items: Sequence[Check], *, surface: str) -> list[str]:
    """``cli`` numbers the checks and names commands; ``tui`` names keys."""

    lines: list[str] = []
    for number, item in enumerate(items, start=1):
        mark = MARKS[item.state]
        head = f"{mark} {number} {item.title:<17}" if surface == "cli" else f"{mark} {item.title:<17}"
        lines.append(f"  {head}  {item.detail}".rstrip() if surface == "cli" else f"{head}  {item.detail}".rstrip())
        if item.fix is not None and item.state in ("fail", "attention"):
            text = (item.fix.tui or item.fix.cli) if surface == "tui" else (item.fix.cli or item.fix.tui)
            if text:
                lines.append(("      fix: " if surface == "cli" else "                     fix: ") + text)
    return lines


def summary(items: Sequence[Check]) -> str:
    failed = failing(items)
    if not failed:
        return texts.FIRSTRUN_READY
    return texts.FIRSTRUN_NOT_READY.format(n=len(failed), title=failed[0].title)


def error_json(message: str) -> dict[str, Any]:
    """The JSON form when the checks could not run at all: the same shape,
    not ready, no checks, and the error that stopped them."""

    return {"version": 1, "ready": False, "next": None, "items": [], "info": [texts.FIRSTRUN_INFO],
            "error": message}


def to_json(items: Sequence[Check], info: Sequence[str] = (texts.FIRSTRUN_INFO,)) -> dict[str, Any]:
    failed = failing(items)
    return {
        "version": 1,
        "ready": not failed,
        "next": failed[0].id if failed else None,
        "items": [{
            "id": item.id, "title": item.title, "state": item.state, "detail": item.detail,
            "fix": None if item.fix is None or item.state not in ("fail", "attention") else {
                "text": item.fix.cli or item.fix.tui, "argv": list(item.fix.argv)},
            "required": item.required,
        } for item in items],
        "info": list(info),
    }
