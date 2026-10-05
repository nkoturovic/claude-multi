"""Render the supervised gateway's systemd user unit from the service spec.

Pure text in, text out. The service spec (``gateway-unit.json`` in the
packaged resources) chooses the private directories, the bind sets, the home
protection level and which Tier B directives apply; this module holds the
directive values and the unit's lifecycle:

- ``ExecStartPre=+`` runs ``init --prepare-start`` outside the namespace
  (Python prepares; the gateway later sees its config read-only),
- ``ExecStart`` runs ``run --prepared`` inside it,
- ``ExecReload=+`` runs ``init --reload-check`` (the gateway hot-reloads a
  re-rendered config in place),
- ``Restart=on-failure`` under a start limit, ``UMask=0077``, no core dumps,
  and the environment variables that would move the gateway's config or
  secrets unset.

Every path is HOME-relative (``%h``) and the executable is reached through a
stable link to the selected installation, so the text never names a package
or store path; the exceptions are the optional failure notifier's absolute
path and a certificate location outside HOME (:class:`TrustPath`: the
``SSL_CERT_FILE``/``SSL_CERT_DIR`` the installing launcher trusts, set in the
unit's environment and bound read-only where the unit's view hides it).
:func:`parse` reads a unit back into directives for comparisons, and
:func:`trust_of` the certificate trust a unit carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

SPEC_VERSION = 1
SPEC_KEYS = frozenset({"version", "backend", "unit", "exec_links", "state_root", "working_directory",
                       "private_dirs", "bind_rw", "bind_ro", "hardening"})
EXEC_CHANNELS = ("bundle", "nix")
HOME_LEVELS = ("tmpfs", "read-only", "none")
UNIT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
PROXY_ENTRY = "bin/claude-multi-proxy"
DESCRIPTION = "claude-multi local model gateway"
FAILURE_DESCRIPTION = "claude-multi gateway failure notice"
# The first line of every unit this product writes: uninstall and refresh
# touch only files that carry it.
HEADER = "# Written by claude-multi gateway service install; run it again to refresh this file."
# Variables that would point the gateway at another config root or secret file.
UNSET_ENVIRONMENT = ("XDG_CONFIG_HOME", "CLAUDE_MULTI_SECRET_ENV")
START_LIMIT_INTERVAL_SEC = 300
START_LIMIT_BURST = 5
RESTART_SEC = "5s"

# Tier B: the hardening directives that hold for the gateway (the spec lists
# which apply). Lists render one directive line per value.
TIER_B: dict[str, str | tuple[str, ...]] = {
    "ProtectSystem": "strict",
    "PrivateTmp": "true",
    "PrivateDevices": "true",
    "NoNewPrivileges": "true",
    "RestrictSUIDSGID": "true",
    "LockPersonality": "true",
    "RestrictRealtime": "true",
    "RestrictNamespaces": "true",
    "SystemCallArchitectures": "native",
    "SystemCallFilter": ("@system-service",),
    "SystemCallErrorNumber": "EPERM",
    "RestrictAddressFamilies": ("AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"),
    "ProtectKernelTunables": "true",
    "ProtectKernelModules": "true",
    "ProtectKernelLogs": "true",
    "ProtectControlGroups": "true",
    "ProtectHostname": "true",
}


class UnitSpecError(ValueError):
    """The service spec or a unit plan breaks a rule (named in the message)."""


# The certificate variables a unit may carry (the gateway reads them like OpenSSL).
TRUST_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")
# Characters a certificate path in Environment= or BindReadOnlyPaths= cannot
# carry without quoting: whitespace, quotes, specifiers, variables, the bind
# separator and backslashes.
_UNSAFE_PATH = frozenset(" \t\n\r%$\\'\":")


@dataclass(frozen=True)
class TrustPath:
    """One certificate location the unit carries: its variable, the path as
    the unit writes it (``%h/<relative>`` under HOME, else absolute) and
    whether it is bound read-only into the unit's view."""

    variable: str
    path: str
    bind: bool = False


def check_trust_path(item: TrustPath) -> None:
    """Raise :class:`UnitSpecError` unless the unit can carry ``item`` as written."""

    if item.variable not in TRUST_VARIABLES:
        raise UnitSpecError(f"a unit carries only {', '.join(TRUST_VARIABLES)}, not {item.variable}")
    # The same characters are refused after ``%h/``: a colon there would
    # make the bind a source and a destination too.
    unsafe = any(ch in _UNSAFE_PATH or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item.path.removeprefix("%h/"))
    if item.path.startswith("%h/"):
        _relative(item.path[3:], f"the {item.variable} path")
        if unsafe:
            raise UnitSpecError(f"the {item.variable} path must be a plain HOME-relative path "
                                "(no spaces, quotes, %, $, : or backslashes)")
        return
    path = PurePosixPath(item.path)
    if not path.is_absolute() or ".." in path.parts or str(path) != item.path or unsafe:
        raise UnitSpecError(f"the {item.variable} path must be a plain absolute path "
                            "(no spaces, quotes, %, $, : or backslashes)")


def _relative(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnitSpecError(f"{where} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or any(
            ch.isspace() or ch in "%$\\'\"" for ch in value):
        raise UnitSpecError(f"{where} must be a plain HOME-relative path")
    return value


def _path_list(document: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list) or not value:
        raise UnitSpecError(f"service spec {key} must be a non-empty list")
    items = tuple(_relative(item, f"service spec {key}[]") for item in value)
    if len(set(items)) != len(items):
        raise UnitSpecError(f"service spec {key} has duplicates")
    return items


def _under(path: str, parent: str) -> bool:
    return PurePosixPath(path) == PurePosixPath(parent) or PurePosixPath(path).is_relative_to(parent)


def validate_spec(document: Any) -> dict[str, Any]:
    """The closed service spec, checked; raises :class:`UnitSpecError`."""

    if not isinstance(document, dict):
        raise UnitSpecError("service spec must be a JSON object")
    if set(document) != SPEC_KEYS:
        unknown = sorted(set(document) - SPEC_KEYS)
        missing = sorted(SPEC_KEYS - set(document))
        raise UnitSpecError(f"service spec keys differ (unknown: {unknown}, missing: {missing})")
    if document["version"] != SPEC_VERSION or isinstance(document["version"], bool):
        raise UnitSpecError(f"service spec version must be {SPEC_VERSION}")
    if document["backend"] != "systemd":
        raise UnitSpecError("service spec backend must be systemd")
    if not isinstance(document["unit"], str) or not UNIT_NAME.fullmatch(document["unit"]):
        raise UnitSpecError("service spec unit must be a plain service name")
    links = document["exec_links"]
    if not isinstance(links, dict) or set(links) != set(EXEC_CHANNELS):
        raise UnitSpecError(f"service spec exec_links must name exactly {', '.join(EXEC_CHANNELS)}")
    for channel, link in links.items():
        _relative(link, f"service spec exec_links.{channel}")
    state_root = _relative(document["state_root"], "service spec state_root")
    workdir = _relative(document["working_directory"], "service spec working_directory")
    if not _under(workdir, state_root) or workdir == state_root:
        raise UnitSpecError("service spec working_directory must lie under state_root")
    private = _path_list(document, "private_dirs")
    for index, path in enumerate(private):
        # Parents first, so creating them in order hardens every level.
        if any(_under(path, later) for later in private[index + 1:]):
            raise UnitSpecError("service spec private_dirs must list parents before children")
    for key in ("bind_rw", "bind_ro"):
        for path in _path_list(document, key):
            if path not in private:
                raise UnitSpecError(f"service spec {key} entry {path} is not a private directory")
    hardening = document["hardening"]
    if not isinstance(hardening, dict) or set(hardening) != {"home", "tier_b"}:
        raise UnitSpecError("service spec hardening must hold exactly home and tier_b")
    if hardening["home"] not in HOME_LEVELS:
        raise UnitSpecError(f"service spec hardening.home must be one of {', '.join(HOME_LEVELS)}")
    tier = hardening["tier_b"]
    if not isinstance(tier, list) or len(set(tier)) != len(tier) or not set(tier) <= set(TIER_B):
        raise UnitSpecError("service spec hardening.tier_b must list known directives once each")
    return document


@dataclass(frozen=True)
class UnitPlan:
    """One unit to render: its name, the stable link it executes through and
    the optional failure notifier (an absolute ``notify-send`` and the
    notice's title and body)."""

    name: str
    exec_root: str
    notify_send: str | None = None
    notice: tuple[str, str] | None = None
    trust: tuple[TrustPath, ...] = ()

    @property
    def unit_file(self) -> str:
        return f"{self.name}.service"

    @property
    def failure_unit_file(self) -> str | None:
        return f"{self.name}-failure.service" if self.notify_send else None


def bind_sets(spec: Mapping[str, Any], exec_root: str) -> tuple[list[str], list[str]]:
    """``(read-write, read-only)`` HOME-relative binds, sorted.

    The stable exec link's directory joins the read-only set when no bind
    already covers it (the executable must be reachable inside the view).
    """

    rw = sorted(set(spec["bind_rw"]))
    ro = set(spec["bind_ro"])
    parent = str(PurePosixPath(exec_root).parent)
    if not any(_under(parent, path) for path in (*rw, *ro)):
        ro.add(parent)
    return rw, sorted(ro)


def _exec_word(value: str) -> str:
    """One quoted argument of an Exec line (specifiers and variables escaped)."""

    if any(ch in value for ch in "'\\\n\r\t") or any(ord(ch) < 0x20 for ch in value):
        raise UnitSpecError("notice text must not contain quotes, backslashes or control characters")
    return "'" + value.replace("%", "%%").replace("$", "$$") + "'"


def _check_plan(spec: Mapping[str, Any], plan: UnitPlan) -> None:
    if not UNIT_NAME.fullmatch(plan.name):
        raise UnitSpecError("the unit name must be a plain service name (a-z, 0-9, -)")
    _relative(plan.exec_root, "the exec link")
    if plan.notify_send is not None:
        path = PurePosixPath(plan.notify_send)
        if not path.is_absolute() or any(ch.isspace() or ch in "%$\\'\"" for ch in plan.notify_send):
            raise UnitSpecError("the notifier must be a plain absolute path")
        if plan.notice is None:
            raise UnitSpecError("a failure notifier needs the notice text")
    variables = [item.variable for item in plan.trust]
    if len(set(variables)) != len(variables):
        raise UnitSpecError("a certificate variable is carried once")
    for item in plan.trust:
        check_trust_path(item)


def render(spec: Mapping[str, Any], plan: UnitPlan) -> dict[str, str]:
    """``{file name: text}``: the gateway unit and, with a notifier, its
    failure-notice unit."""

    validate_spec(dict(spec))
    _check_plan(spec, plan)
    home = lambda rel: f"%h/{rel}"  # noqa: E731
    proxy = home(f"{plan.exec_root}/{PROXY_ENTRY}")
    state_root = home(spec["state_root"])
    unit: list[tuple[str, str]] = [
        ("Description", DESCRIPTION),
        ("After", "network-online.target"),
        ("Wants", "network-online.target"),
        ("StartLimitIntervalSec", str(START_LIMIT_INTERVAL_SEC)),
        ("StartLimitBurst", str(START_LIMIT_BURST)),
    ]
    if plan.failure_unit_file:
        unit.append(("OnFailure", plan.failure_unit_file))
    service: list[tuple[str, str]] = [
        ("Type", "simple"),
        ("ExecStartPre", f"+{proxy} init --prepare-start --state-root {state_root}"),
        ("ExecStart", f"{proxy} run --prepared --state-root {state_root}"),
        ("ExecReload", f"+{proxy} init --reload-check --state-root {state_root}"),
        ("WorkingDirectory", "-" + home(spec["working_directory"])),
        ("Restart", "on-failure"),
        ("RestartSec", RESTART_SEC),
        ("UMask", "0077"),
        ("LimitCORE", "0"),
        *(("UnsetEnvironment", name) for name in UNSET_ENVIRONMENT),
        *(("Environment", f"{item.variable}={item.path}") for item in plan.trust),
    ]
    level = spec["hardening"]["home"]
    rw, ro = bind_sets(spec, plan.exec_root)
    if level == "tmpfs":
        service.append(("ProtectHome", "tmpfs"))
        service.extend(("BindPaths", home(path)) for path in rw)
        service.extend(("BindReadOnlyPaths", home(path)) for path in ro)
    elif level == "read-only":
        service.append(("ProtectHome", "read-only"))
        service.extend(("ReadWritePaths", home(path)) for path in rw)
    service.extend(("BindReadOnlyPaths", item.path) for item in plan.trust if item.bind)
    for name in spec["hardening"]["tier_b"]:
        value = TIER_B[name]
        values = value if isinstance(value, tuple) else (value,)
        service.extend((name, item) for item in values)
    files = {plan.unit_file: _text([("Unit", unit), ("Service", service),
                                    ("Install", [("WantedBy", "default.target")])])}
    if plan.failure_unit_file:
        title, body = plan.notice  # type: ignore[misc]
        files[plan.failure_unit_file] = _text([
            ("Unit", [("Description", FAILURE_DESCRIPTION)]),
            ("Service", [("Type", "oneshot"), (
                "ExecStart",
                f"-{plan.notify_send} --urgency=critical --app-name=claude-multi "
                f"{_exec_word(title)} {_exec_word(body)}")]),
        ])
    return files


def _text(sections: list[tuple[str, list[tuple[str, str]]]]) -> str:
    lines = [HEADER]
    for name, directives in sections:
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(f"{key}={value}" for key, value in directives)
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, list[tuple[str, str]]]:
    """``{section: [(key, value), ...]}`` in file order (comments dropped)."""

    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        if current is None or "=" not in line:
            raise UnitSpecError("not a unit file: a line outside any section or without '='")
        key, value = line.split("=", 1)
        current.append((key.strip(), value.strip()))
    return sections


def trust_of(text: str) -> tuple[TrustPath, ...]:
    """The certificate trust a unit's text carries (its ``Environment=``
    certificate variables, each bound when a ``BindReadOnlyPaths=`` names
    it); empty for a text that is not a unit."""

    try:
        section = parse(text).get("Service", [])
    except UnitSpecError:
        return ()
    binds = {value for key, value in section if key == "BindReadOnlyPaths"}
    found = []
    for key, value in section:
        variable, _, path = value.partition("=")
        if key == "Environment" and variable in TRUST_VARIABLES and path:
            found.append(TrustPath(variable, path, path in binds))
    return tuple(found)


def written_here(text: str) -> bool:
    """Whether a unit file's text carries this product's header line."""

    return text.split("\n", 1)[0] == HEADER
