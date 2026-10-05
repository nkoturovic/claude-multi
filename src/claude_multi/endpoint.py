"""Where the local gateway listens, and which backend runs it.

``gateway_endpoint(document)`` returns the catalog's ``base_url`` and
``health_path`` exactly as written. Given ``home`` it prefers the user's
``endpoint.json``: a closed document next to the gateway key and
``config.yaml`` (HOME-relative, like them, so the launcher and the gateway
always agree) naming the loopback port and, once the supervised service is
installed, its backend. The catalog value is the fallback, so an install
without the document keeps the port it has always used.

The document may also name the gateway's outbound proxy (``proxy_url``: an
unauthenticated http/https/socks5 URL), which the render writes as the
config's ``proxy-url``; the gateway process never inherits proxy variables
from the environment.

A new install chooses the first free port of :data:`NEW_INSTALL_PORTS`
(:func:`ensure_config`: from a start, the setup step, a service install or
the preparation of its first launch, before a scope is compiled for it;
never from a render). The backend is on-demand on every
release channel and platform until the endpoint document records
``systemd``; the channel (``CLAUDE_MULTI_CHANNEL``, set by every wrapper)
is reported, never used to choose the backend.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import re
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claude_multi import errors, paths, state, strict_json

ENDPOINT_FILE = "endpoint.json"
ENDPOINT_VERSION = 1
HOST = "127.0.0.1"
# New installs never default to the long-lived development port; twenty
# candidates leave room for other local services.
NEW_INSTALL_PORTS = tuple(range(18317, 18337))
PORT_MIN, PORT_MAX = 1024, 65535

ON_DEMAND, SYSTEMD = "on-demand", "systemd"
BACKENDS = (ON_DEMAND, SYSTEMD)
DEFAULT_UNIT = "claude-multi-gateway"
_UNIT = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_KEYS = frozenset({"version", "host", "port", "backend", "unit", "proxy_url"})
OUTBOUND_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")

CHANNEL_ENV = "CLAUDE_MULTI_CHANNEL"
CHANNELS = ("nix", "bundle", "source")

# Files whose presence marks an install that already rendered a gateway:
# it keeps its port (the catalog value) unless endpoint.json says otherwise.
_EXISTING_INSTALL_MARKERS = ("config.yaml", "api-key")
# What sets the gateway up in a home that has neither (it records the port).
SETUP_COMMAND = "claude-multi setup --step gateway"
NOT_SET_UP = f"the local gateway is not set up yet: run {SETUP_COMMAND} first"


class NotSetUpError(errors.ClaudeMultiError, RuntimeError):
    """A gateway change in a home whose gateway was never set up."""


class EndpointError(errors.ClaudeMultiError, ValueError):
    """The endpoint document is unreadable, invalid or names an unsupported backend."""


@dataclass(frozen=True)
class GatewayEndpoint:
    document: Mapping[str, Any]

    @property
    def base_url(self) -> str:
        return self.document["gateway"]["base_url"]

    @property
    def health_path(self) -> str:
        return self.document["gateway"]["health_path"]


@dataclass(frozen=True)
class EndpointConfig:
    """One validated ``endpoint.json``."""

    port: int
    backend: str = ON_DEMAND
    unit: str | None = None
    host: str = HOST
    proxy_url: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def service_unit(self) -> str | None:
        """The supervised unit's name (systemd backend only)."""

        if self.backend != SYSTEMD:
            return None
        return self.unit or DEFAULT_UNIT

    def document(self) -> dict[str, Any]:
        result: dict[str, Any] = {"version": ENDPOINT_VERSION, "host": self.host, "port": self.port}
        if self.backend != ON_DEMAND:
            result["backend"] = self.backend
        if self.unit is not None:
            result["unit"] = self.unit
        if self.proxy_url:
            result["proxy_url"] = self.proxy_url
        return result


def outbound_proxy_problem(url: Any) -> str | None:
    """None when ``url`` is empty (no proxy) or a credential-free proxy URL
    (http/https/socks5/socks5h, a host, an optional port, no userinfo, path,
    query or fragment); else a value-free reason (a URL may carry a secret,
    so it is never echoed)."""

    if url == "":
        return None
    if not isinstance(url, str):
        return "proxy URL must be a string"
    if len(url) > 2048 or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        return "proxy URL is too long or contains whitespace or control characters (not shown)"
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return "proxy URL does not parse (not shown)"
    if parsed.scheme.lower() not in OUTBOUND_PROXY_SCHEMES:
        return f"proxy URL scheme must be one of {', '.join(OUTBOUND_PROXY_SCHEMES)} (not shown)"
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        return "proxy URL carries userinfo; credentials are never allowed in it (not shown)"
    if not parsed.hostname:
        return "proxy URL has no host (not shown)"
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or "?" in url or "#" in url:
        return "proxy URL carries a path, query or fragment (not shown)"
    if port is not None and not 0 < port < 65536:
        return "proxy URL port is out of range (not shown)"
    return None


def endpoint_path(home: Path | str) -> Path:
    return paths.gateway_config_dir({"HOME": str(home)}) / ENDPOINT_FILE


def _remedy(path: Path) -> str:
    shown = paths.display(path, {"HOME": str(path.parents[2])})
    return (f"fix or remove {shown} (without it the gateway uses the packaged port); "
            "then retry")


def parse_config(document: Any) -> EndpointConfig:
    """Validate the closed document; raises :class:`EndpointError` naming the rule."""

    if not isinstance(document, dict):
        raise EndpointError("endpoint document must be a JSON object")
    unknown = sorted(set(document) - _KEYS)
    if unknown:
        raise EndpointError(f"endpoint document has unknown keys: {', '.join(unknown)}")
    missing = sorted({"version", "host", "port"} - set(document))
    if missing:
        raise EndpointError(f"endpoint document lacks: {', '.join(missing)}")
    if document["version"] != ENDPOINT_VERSION or isinstance(document["version"], bool):
        raise EndpointError(f"endpoint document version must be {ENDPOINT_VERSION}")
    if document["host"] != HOST:
        raise EndpointError(f"endpoint host must be {HOST} (the gateway is loopback only)")
    port = document["port"]
    if type(port) is not int or not PORT_MIN <= port <= PORT_MAX:
        raise EndpointError(f"endpoint port must be an integer in {PORT_MIN}..{PORT_MAX}")
    backend = document.get("backend", ON_DEMAND)
    if backend not in BACKENDS:
        raise EndpointError(f"endpoint backend must be one of: {', '.join(BACKENDS)}")
    unit = document.get("unit")
    if unit is not None:
        if backend != SYSTEMD:
            raise EndpointError("endpoint unit is recorded only with the systemd backend")
        if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
            raise EndpointError("endpoint unit must be a plain service name")
    proxy_url = document.get("proxy_url")
    if proxy_url is not None:
        problem = outbound_proxy_problem(proxy_url) if proxy_url != "" else "proxy URL is empty"
        if problem is not None:
            raise EndpointError(f"endpoint proxy_url refused: {problem}")
    return EndpointConfig(port=port, backend=backend, unit=unit, proxy_url=proxy_url)


def read_config(home: Path | str) -> EndpointConfig | None:
    """The validated document, or None when absent; unreadable/invalid raises."""

    path = endpoint_path(home)
    if not os.path.lexists(path):
        return None
    try:
        document = strict_json.loads(state.read_private(path))
    except state.StateError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise EndpointError(f"endpoint document unreadable: {exc}", remedy=_remedy(path)) from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise EndpointError(f"endpoint document is not valid JSON: {exc}", remedy=_remedy(path)) from exc
    try:
        return parse_config(document)
    except EndpointError as exc:
        raise EndpointError(str(exc), remedy=_remedy(path)) from exc


def write_config(home: Path | str, config: EndpointConfig) -> Path:
    """Atomic 0600 write in the private gateway config directory."""

    parse_config(config.document())  # never write what a reader would refuse
    path = endpoint_path(home)
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, strict_json.pretty_file_bytes(config.document()))
    return path


def effective_document(gateway_document: Mapping[str, Any],
                       config: EndpointConfig | None) -> Mapping[str, Any]:
    """The gateway document with the configured base URL and outbound proxy
    (unchanged without a document)."""

    if config is None:
        return gateway_document
    gateway = {**gateway_document["gateway"], "base_url": config.base_url}
    if config.proxy_url and isinstance(gateway.get("cliproxy_static"), Mapping):
        gateway["cliproxy_static"] = {**gateway["cliproxy_static"], "proxy-url": config.proxy_url}
    return {**gateway_document, "gateway": gateway}


def gateway_endpoint(gateway_document: Mapping[str, Any], *,
                     home: Path | str | None = None) -> GatewayEndpoint:
    """The catalog endpoint, or with ``home`` the user's ``endpoint.json`` first."""

    if home is None:
        return GatewayEndpoint(gateway_document)
    return GatewayEndpoint(effective_document(gateway_document, read_config(home)))


def apply_to_catalog(catalog: Any, home: Path | str) -> Any:
    """A loaded catalog whose ``docs["gateway"]`` carries the configured endpoint.

    The bundle hash and every other document are untouched: the endpoint is
    user configuration, not trusted catalog content.
    """

    config = read_config(home)
    if config is None:
        return catalog
    docs = {**catalog.docs, "gateway": effective_document(catalog.docs["gateway"], config)}
    return dataclasses.replace(catalog, docs=docs)


def port_of(base_url: str) -> int:
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", base_url)
    if match is None:
        raise EndpointError(f"gateway base URL {base_url!r} is not loopback http with a port")
    return int(match.group(1))


def port_free(port: int, *, host: str = HOST) -> bool:
    """Whether ``host:port`` can be bound right now (a bind, never a connect)."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def select_port(probe: Callable[[int], bool] = port_free,
                ports: Iterable[int] = NEW_INSTALL_PORTS) -> int:
    candidates = tuple(ports)
    for port in candidates:
        if probe(port):
            return port
    raise EndpointError(
        f"no free gateway port in {candidates[0]}-{candidates[-1]}",
        remedy=f"free one of those ports, or write {ENDPOINT_FILE} with a free port",
    )


def existing_install(home: Path | str) -> bool:
    """A gateway config was rendered here before (it keeps its port)."""

    directory = paths.gateway_config_dir({"HOME": str(home)})
    return any(os.path.lexists(directory / name) for name in _EXISTING_INSTALL_MARKERS)


def set_up(home: Path | str) -> bool:
    """The gateway has a home here: ``endpoint.json`` records its port, or an
    earlier gateway was prepared here (it keeps the packaged port).

    A home with neither is a new install that has not chosen its port: no
    command may reload, apply or ensure a gateway there (another program may
    own the packaged port); they refuse with :data:`NOT_SET_UP`.
    """

    return os.path.lexists(endpoint_path(home)) or existing_install(home)


def require_set_up(home: Path | str, what: str = "nothing was changed") -> None:
    """Raise :class:`NotSetUpError` in a home whose gateway is not set up."""

    if not set_up(home):
        raise NotSetUpError(f"{NOT_SET_UP}; {what}", remedy=SETUP_COMMAND)


def ensure_config(home: Path | str, *,
                  probe: Callable[[int], bool] = port_free) -> EndpointConfig | None:
    """The endpoint to start on; a new install records its port first.

    An existing document wins. An install that already rendered a gateway
    config keeps the packaged port (returns None). A new install takes the
    first free port of :data:`NEW_INSTALL_PORTS` and records it, under a lock
    so two concurrent first starts agree.
    """

    current = read_config(home)
    if current is not None or existing_install(home):
        return current
    path = endpoint_path(home)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        current = read_config(home)
        if current is not None:
            return current
        config = EndpointConfig(port=select_port(probe))
        write_config(home, config)
        return config


def resolve_backend(config: EndpointConfig | None, *, platform: str | None = None) -> str:
    """``on-demand`` unless the endpoint records an installed, supported service.

    The release channel never enters this decision. A recorded backend the
    platform cannot run is refused rather than silently replaced.
    """

    if config is None or config.backend == ON_DEMAND:
        return ON_DEMAND
    current = sys.platform if platform is None else platform
    if config.backend == SYSTEMD and current.startswith("linux"):
        return SYSTEMD
    raise EndpointError(
        f"the endpoint records the {config.backend} service backend, which this platform "
        f"({current}) cannot run",
        remedy=f"remove the backend and unit keys from {ENDPOINT_FILE} to use the on-demand gateway",
    )


def channel(environ: Mapping[str, str]) -> str | None:
    """The wrapper's release channel, or None when unset or unrecognised."""

    value = environ.get(CHANNEL_ENV)
    return value if value in CHANNELS else None


def set_backend(home: Path | str, backend: str, unit: str | None = None, *,
                packaged_port: int, probe: Callable[[int], bool] = port_free,
                ) -> tuple[EndpointConfig | None, EndpointConfig]:
    """Record the backend (and, for systemd, the unit); returns
    ``(previous, recorded)`` so a failed hand-off can restore ``previous``.

    Without a document a new install takes its first free port and an
    existing install records the packaged port it already uses.
    """

    if backend not in BACKENDS:
        raise EndpointError(f"unknown backend {backend!r}")
    path = endpoint_path(home)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        previous = read_config(home)
        base = previous
        if base is None:
            base = EndpointConfig(port=packaged_port if existing_install(home) else select_port(probe))
        updated = dataclasses.replace(base, backend=backend,
                                      unit=unit if backend == SYSTEMD else None)
        write_config(home, updated)
        return previous, updated


def restore_config(home: Path | str, previous: EndpointConfig | None) -> None:
    """Put ``previous`` back (None: remove the document a hand-off created)."""

    path = endpoint_path(home)
    with state.FileLock(path):
        if previous is None:
            state.remove_private(path)
        else:
            write_config(home, previous)


def check_proxy(proxy_url: str | None) -> None:
    """Refuse an outbound proxy URL the gateway must not use (None: no proxy)."""

    if proxy_url is not None:
        problem = outbound_proxy_problem(proxy_url) if proxy_url != "" else "proxy URL is empty"
        if problem is not None:
            raise EndpointError(f"proxy refused: {problem}",
                                remedy="give an unauthenticated http://, https:// or socks5:// URL "
                                "with a host and an optional port")


def set_proxy(home: Path | str, proxy_url: str | None, *,
              probe: Callable[[int], bool] = port_free, packaged_port: int | None = None) -> EndpointConfig:
    """Record (or with None clear) the gateway's outbound proxy.

    The port and backend stay as recorded. Without a document, a new install
    takes its first free port and an existing install records the packaged
    port it already uses (``packaged_port``), so the proxy has a home.
    """

    return swap_proxy(home, proxy_url, probe=probe, packaged_port=packaged_port)[1]


def swap_proxy(home: Path | str, proxy_url: str | None, *,
               probe: Callable[[int], bool] = port_free,
               packaged_port: int | None = None,
               before_write: Callable[[bytes | None, EndpointConfig], None] | None = None,
               ) -> tuple[bytes | None, EndpointConfig]:
    """:func:`set_proxy`, also returning the bytes of the document it
    replaced, read under the same lock (None: there was none), so a failed
    change can put back exactly what was there (:func:`restore_bytes`).
    ``before_write(previous, updated)`` runs under that lock right before
    the new document is written: a caller registers its undo there, so an
    interrupt or a failure after the bytes were replaced is never left
    without one."""

    check_proxy(proxy_url)
    path = endpoint_path(home)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        current = read_config(home)
        previous = state.read_private(path) if current is not None else None
        if current is None:
            if existing_install(home):
                if packaged_port is None:
                    raise EndpointError("the packaged gateway port is unknown")
                current = EndpointConfig(port=packaged_port)
            else:
                current = EndpointConfig(port=select_port(probe))
        updated = dataclasses.replace(current, proxy_url=proxy_url or None)
        if before_write is not None:
            before_write(previous, updated)
        write_config(home, updated)
        return previous, updated


def restore_bytes(home: Path | str, previous: bytes | None) -> None:
    """Put back the document :func:`swap_proxy` replaced (None: remove it)."""

    path = endpoint_path(home)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        if previous is None:
            state.remove_private(path)
        else:
            state.atomic_write(path, previous)
