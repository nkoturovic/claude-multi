"""Runtime proxy control for claude-multi.

Commands: ``init``, ``status``, ``run``, ``rotate-management-key``,
``disable-management-key``, ``claude-login``,
``codex-device-login`` and ``snapshot-auth [--restore NAME]`` (copy the
gateway auth dir to ``<auth_dir>.pre-7.3.<YYYYMMDD>`` before a gateway
upgrade; restore it on rollback while the gateway is stopped; names only,
never contents).
The existing auth root (``~/.local/share/claude-multi``) and OAuth records
are preserved untouched. Provider ``env:NAME`` references resolve from the
existing mode-restricted secret env file via strict assignment parsing — no
shell sourcing, no eval, no value ever printed. The complete gateway config
renders atomically to a mode-0600 artifact outside the repository; ``run``
and the login helpers initialize safely, then ``execve`` the pinned
CLIProxyAPI with no resident wrapper.

``init``, ``run`` and the login helpers accept one flag, ``--state-root
/abs/path`` (default: ``$XDG_STATE_HOME/claude-multi`` or
``~/.local/state/claude-multi``): the session state root whose records
extend the gateway continuity set (``~/.config/claude-multi/
continuity.json``) — retired selectors live sessions still
name stay served until ``claude-multi doctor --prune-aliases``.

``run`` and the login helpers use ``<state root>/gateway`` (0700, with
``logs/``) as the gateway's working directory: the pinned
CLIProxyAPI loads ``<cwd>/.env`` after the env scrub, so they refuse to
exec while a ``.env`` exists there (never opened), and the entrypoint
``chdir``s into the directory right before ``execve``.
"""

from __future__ import annotations

from claude_multi import endpoint

from . import assets

import base64
import contextlib
import datetime
import json
import math
import os
import stat
import subprocess
import sys
import re
import secrets
import shutil
import tempfile
import time
import types
from dataclasses import dataclass
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import catalog as catalog_mod
from . import continuity as continuity_mod
from . import gateway_inhibition
from . import custom as custom_mod
from . import __version__, errors, management, paths, service
from .service import (
    GATEWAY_WORKDIR, GATEWAY_LOGS, GATEWAY_DOTENV,
    gateway_workdir, ensure_gateway_workdir, gateway_dotenv_present,
)
from . import launch as launch_mod
from . import operator as operator_mod
from . import render as render_mod
from . import served_plan as served_plan_mod
from . import sessions as sessions_mod
from . import secret_store, state, strict_json
from .platform import linux_process, posix_fs


class ProxyError(errors.ClaudeMultiError, RuntimeError):
    """Raised on any proxy-control failure (fail closed, secrets redacted)."""


class ConfigPublishedError(state.CommittedStateError):
    """``config.yaml`` was replaced (the new render is visible to the
    gateway's watcher) but its directory durability is unconfirmed.

    Still a :class:`state.CommittedStateError`; ``rendered`` carries the
    render's ``(target, result, report)`` so a caller that owns the inputs
    keeps them (they match the published config) instead of rolling back."""

    def __init__(self, cause: state.CommittedStateError,
                 rendered: tuple[Path, render_mod.RenderResult, "ContinuityReport"]) -> None:
        super().__init__(cause.errno, cause.strerror)
        self.rendered = rendered


@dataclass(frozen=True)
class ProxyCommand:
    """One ``claude-multi-proxy`` command: the single definition its
    dispatch, its own help, the overall usage and the surface census read.

    ``listing`` is the command's block of the overall usage, ``synopsis``
    its arguments, ``description`` its own help text; ``audience`` is
    ``user`` (a person runs it), ``service`` (the supervised unit runs it)
    or ``maintenance``; ``operations`` names the semantic operations it
    serves. ``check`` validates the arguments without any effect (no
    initialization, write or network observation): it raises
    :class:`ProxyError` with the usage."""

    name: str
    synopsis: str
    listing: str
    description: str
    audience: str
    operations: tuple[str, ...]
    check: Callable[[list[str], "dict[str, str] | None"], Any]

    def usage(self) -> str:
        return f"usage: claude-multi-proxy {self.name}" + (f" {self.synopsis}" if self.synopsis else "")

    def help(self) -> str:
        return f"{self.usage()}\n\n{self.description}"


def _no_arguments(name: str) -> Callable[[list[str], "dict[str, str] | None"], None]:
    def check(args: list[str], _environ: "dict[str, str] | None") -> None:
        if args:
            raise ProxyError(f"usage: claude-multi-proxy {name}")
    return check


def _check_snapshot_args(args: list[str], _environ: "dict[str, str] | None") -> None:
    if args and not (len(args) == 2 and args[0] == "--restore"):
        raise ProxyError("usage: claude-multi-proxy snapshot-auth [--restore SNAPSHOT]")


def _check_login_args(name: str) -> Callable[[list[str], "dict[str, str] | None"], None]:
    def check(args: list[str], environ: "dict[str, str] | None") -> None:
        _parse_state_root(name, args, environ)
    return check


PROXY_COMMANDS: tuple[ProxyCommand, ...] = (
    ProxyCommand(
        "init", "[--reload-check | --start-check | --prepare-start] [--state-root /abs]",
        "  init [--reload-check | --start-check | --prepare-start] [--state-root /abs]\n"
        "                                  prepare config/key/directories; verify hot reload (up to 2 s);\n"
        "                                  --start-check waits up to 45 s after a (re)start;\n"
        "                                  --prepare-start selects management keys only while stopped\n",
        "Prepare the gateway's configuration, key and directories and verify the hot reload (up to 2 s).\n"
        "--reload-check exits 0 reloaded, 3 restart required, 4 token mismatch, 5 down, 1 on errors.\n"
        "--start-check waits up to 45 s after a (re)start: 0 ready, 6 not ready, 1 on errors.\n"
        "--prepare-start (the supervised unit) selects management keys only while stopped.\n"
        "--state-root /abs --adopt-root adopts another state root after its inventory.",
        "service", ("gateway.prepare", "gateway.reload-check", "gateway.start-check", "gateway.adopt-root"),
        lambda args, environ: _parse_init_args(args, environ),
    ),
    ProxyCommand(
        "status", "",
        "  status                          loopback health and management key state; never initializes\n",
        "Show loopback health and the management key state; never initializes anything.",
        "maintenance", ("gateway.health",), _no_arguments("status"),
    ),
    ProxyCommand(
        "rotate-management-key", "",
        "  rotate-management-key           stage a key; takes effect at the next prepared start\n",
        "Stage a new management key; it takes effect at the next prepared gateway start.",
        "maintenance", ("management-key.rotate",), _no_arguments("rotate-management-key"),
    ),
    ProxyCommand(
        "disable-management-key", "",
        "  disable-management-key          disable quota reads until explicitly re-enabled\n",
        "Disable the management key (quota reads stay off until rotate-management-key re-enables them).",
        "maintenance", ("management-key.disable",), _no_arguments("disable-management-key"),
    ),
    ProxyCommand(
        "run", "[--prepared | --prepare-and-exec [--detach]] [--instance NONCE] [--state-root /abs]",
        "  run [--prepared | --prepare-and-exec [--detach]] [--instance NONCE] [--state-root /abs]\n"
        "                                  exec the gateway, holding the single-instance lock across exec;\n"
        "                                  --prepared verifies init output read-only; --prepare-and-exec\n"
        "                                  prepares like init --prepare-start, then execs in one process\n",
        "Exec the gateway, holding the single-instance lock across exec.\n"
        "--prepared (the supervised unit) verifies init output read-only and exits 1 if it is absent or stale;\n"
        "--prepare-and-exec prepares like init --prepare-start, then execs in one process.\n"
        "Exits 75 while another gateway instance holds the lock.",
        "service", ("gateway.run",), lambda args, environ: _parse_run_args(args, environ),
    ),
    ProxyCommand(
        "claude-login", "[--state-root /abs]",
        "  claude-login [--state-root /abs]        sign in to a Claude account "
        "(use claude-multi providers sign-in anthropic)\n",
        "Sign the gateway in to a Claude account. Use claude-multi providers sign-in anthropic, which\n"
        "shows the personal-use acknowledgement this command requires first.",
        "maintenance", ("account.sign-in",), _check_login_args("claude-login"),
    ),
    ProxyCommand(
        "codex-device-login", "[--state-root /abs]",
        "  codex-device-login [--state-root /abs]  sign in to a ChatGPT account "
        "(use claude-multi providers sign-in openai)\n",
        "Sign the gateway in to a ChatGPT account. Use claude-multi providers sign-in openai, which\n"
        "shows the personal-use acknowledgement this command requires first.",
        "maintenance", ("account.sign-in",), _check_login_args("codex-device-login"),
    ),
    ProxyCommand(
        "snapshot-auth", "[--restore NAME]",
        "  snapshot-auth [--restore NAME]   copy the auth dir aside / restore it (gateway stopped)\n",
        "Copy the gateway's auth directory aside, or restore a copy (with the gateway stopped).\n"
        "Prints paths, the file count and file names only.",
        "maintenance", ("auth.snapshot", "auth.restore"), _check_snapshot_args,
    ),
)
PROXY_COMMAND_NAMES = tuple(command.name for command in PROXY_COMMANDS)
_PROXY_COMMANDS = {command.name: command for command in PROXY_COMMANDS}
HELP_ARGUMENTS = ("-h", "--help")

PROXY_USAGE = (
    "usage: claude-multi-proxy COMMAND [ARGS]\n"
    "\n"
    "The claude-multi local gateway (CLIProxyAPI, loopback only).\n"
    "\n"
    "commands:\n"
    + "".join(command.listing for command in PROXY_COMMANDS)
    + "  -v, --version                   print the version\n"
    "\n"
    "init normally exits 0 after rendering; --reload-check exits 0 reloaded, 3 restart\n"
    "required, 4 token mismatch, 5 down, 1 on errors (including rendering failures).\n"
    "--start-check retries a refused connection and needs the render sentinel plus one\n"
    "rendered OAuth alias served (when the render has any): 0 ready, 6 not ready (the\n"
    "message names what is missing), 1 on errors.\n"
    "run --prepared exits 1 if preparation is absent/stale; it never writes config.\n"
    "run exits 75 while another gateway instance holds the lock.\n"
    "Every command exits 130 when it is interrupted (Ctrl-C).\n"
    "\n"
    "The local gateway: claude-multi gateway status (also start, stop, restart, logs)."
)


LOGIN_FLAGS = {
    "claude-login": "--claude-login",
    "codex-device-login": "--codex-device-login",
}

# The env-file grammar lives in secret_store (the same objects).
_ASSIGNMENT = secret_store.ASSIGNMENT
_VALUE_SHAPE = secret_store.VALUE_SHAPE
_TOKEN_SHAPE = re.compile(r"^[0-9a-f]{64}$")


def _home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return paths.home(env)


def state_dir(home: Path) -> Path:
    return home / ".local" / "share" / "claude-multi"


def config_dir(home: Path) -> Path:
    return paths.gateway_config_dir({"HOME": str(home)})


def secret_env_path(environ: dict[str, str] | None = None) -> Path:
    """Compatibility wrapper: :func:`secret_store.secret_env_path`."""

    return secret_store.secret_env_path(environ)


def assets_root(environ: dict[str, str] | None = None) -> Path:
    return assets.root(environ=environ)


def load_bundle(environ: Mapping[str, str] | None = None, *, home: Path | None = None,
                root: Path | str | None = None) -> catalog_mod.Catalog:
    """The trusted catalog with the user's gateway endpoint applied.

    Every gateway-facing path (render, readiness, rotation, status) loads
    through here, so the rendered listener port, the health and models checks
    and the sessions' base URL all name the same port. An unreadable
    ``endpoint.json`` refuses (``endpoint.EndpointError``).
    """

    env = dict(os.environ if environ is None else environ)
    bundle = catalog_mod.load_catalog(assets_root(env) if root is None else root)
    return endpoint.apply_to_catalog(bundle, _home(env) if home is None else home)


def parse_secret_env_bytes(raw: bytes, path: Path) -> dict[str, str]:
    """Strict assignment parsing of env-file BYTES (shared read/write rule).

    A compatibility wrapper over :func:`secret_store.parse_env_bytes`
    (same messages, raised as ``ProxyError``). Never shell-sourced; error
    messages carry line numbers, never secret bytes.
    """

    try:
        return secret_store.parse_env_bytes(raw, path)
    except secret_store.SecretStoreError as exc:
        raise ProxyError(str(exc)) from exc


def parse_secret_env(path: Path) -> dict[str, str]:
    """Strict assignment parsing of the secret env file. Never shell-sourced."""

    try:
        return secret_store.read_env_file(path)
    except secret_store.SecretStoreError as exc:
        raise ProxyError(str(exc)) from exc


def resolve_secret(
    name: str, *, environ: dict[str, str] | None = None
) -> str | None:
    """Resolve an env:NAME reference from the secret store; None if absent.

    The file backend at :func:`secret_env_path`; never the process
    environment.
    """

    try:
        return secret_store.default_store(environ).get(name)
    except secret_store.SecretStoreError as exc:
        raise ProxyError(str(exc), remedy=exc.remedy) from exc


# Verified provider model-listing support (2026-08-10, approval-gated probes):
# kimi answers the Anthropic-shape GET {base}/v1/models; the Qwen Token Plan
# apps/anthropic path returns 404 "Not support"; OAuth pools have no direct
# API credential to list with. Any other DIRECT provider (incl. customs) is
# attempted with the same Anthropic shape and falls back to manual entry.
# Per-provider listing descriptors: how `discover` lists each
# provider's models. Keys:
#   status: "verified" (shape confirmed by a probe) | "attempt" (generic
#           Anthropic-shape attempt on the configured base) | "unsupported"
#   note:   operator-facing detail for the unsupported refusal
#   url:    override the default <base_url>/v1/models target; "{base}"
#           stands for the configured base_url without a trailing slash
#           (a base that already ends in /v1 lists {base}/models)
#   auth:   "provider" (the configured secret/header; default) | "bearer"
#           (the configured secret as Authorization: Bearer — for providers
#           whose listing lives on a different surface than the Anthropic
#           route) | "none" (keyless endpoint, public or LAN — no secret
#           is resolved) |
#           "pool-credential" (an OAuth pool's own credential record from
#           the gateway auth dir — `list_codex_plan_models`, never the
#           generic lister; requires an explicit per-call confirmation)
#   shape:  "anthropic" (default) | "openai" ({data:[{id, name,
#           context_length, ...}]}, e.g. OpenRouter's public listing) |
#           "codex" ({models:[{slug, ...}]}, ids only)
# Descriptors route credentials — the table is immutable trusted config
# (review: a mutable module global could be mutated to redirect a
# credential-bearing probe).
def _freeze_descriptors(
    table: dict[str, dict[str, str]],
) -> Mapping[str, Mapping[str, str]]:
    return types.MappingProxyType(
        {key: types.MappingProxyType(dict(value)) for key, value in table.items()}
    )


CODEX_CLIENT_VERSION = "0.159.1"


_LISTING_SUPPORT = _freeze_descriptors({
    "kimi": {"status": "verified"},
    "qwen": {
        "status": "unsupported",
        "note": "verified 2026-08-10: its Anthropic path answers 404 'Not support'",
    },
    "deepseek": {
        "status": "verified",
        # Verified 2026-08-12 (approval-gated probes): the Anthropic path
        # answers 404 for /v1/models; the documented listing is the
        # OpenAI-shape GET https://api.deepseek.com/models (Bearer). Its
        # entries carry ids only (no context_length) — the add flow asks.
        "url": "https://api.deepseek.com/models",
        "auth": "bearer",
        "shape": "openai",
    },
    "openrouter": {
        "status": "verified",
        "url": "https://openrouter.ai/api/v1/models",
        "auth": "none",
        "shape": "openrouter",
    },
    "meta": {
        # Documented GET /v1/models with Bearer; unverified 2026-09 — the
        # descriptor pins the attempt shape now and flips to "verified" only
        # after a separately approved probe.
        "status": "attempt",
        "url": "https://api.meta.ai/v1/models",
        "auth": "bearer",
        "shape": "openai",
    },
    "openai": {
        # The codex plan's model list, read through ONE
        # codex pool credential from the gateway auth dir — the request the
        # pinned v7.3.15 `cmd/fetch_codex_models` makes, without its token
        # refresh/save. Never run by tests. Verified 2026-09-25 by the
        # operator's approved read on 7.3.15 (pro plan): HTTP 200, the
        # codex `{models:[{slug}]}` shape, nine ids.
        "status": "verified",
        "url": f"https://chatgpt.com/backend-api/codex/models?client_version={CODEX_CLIENT_VERSION}",
        "auth": "pool-credential",
        "shape": "codex",
        "note": "an OAuth pool: list it with `claude-multi discover openai` "
        "(one codex credential, explicit per-call confirmation)",
    },
})


# This table covers catalog providers only. llm-local left the catalog;
# its sample declaration carries its own ``listing`` block ({base}/models,
# openai shape, keyless), which ``claude-multi discover`` plans for operator
# providers (this catalog-only listing refuses them, below).
# Providers synthesized from a providers.d provider block carry
# ``origin: "operator"``; lines-only files on catalog providers keep the
# catalog (T1) provider and its descriptor.
OPERATOR_PROVIDER_ORIGINS = frozenset({"operator", "operator-migrated"})
OPERATOR_LISTING_REFUSAL = (
    "discover {provider_id}: an operator provider is listed through its approved listing block "
    "(claude-multi discover)"
)


def listing_uses_pool_credential(provider_id: str) -> bool:
    """Whether the listing reads an OAuth pool credential (discover openai)."""

    return _LISTING_SUPPORT.get(provider_id, {}).get("auth") == "pool-credential"


def listing_supported(provider_id: str) -> bool:
    """Whether a listing attempt is meaningful for this provider."""

    return _LISTING_SUPPORT.get(provider_id, {}).get("status") != "unsupported"


def listing_is_public(provider_id: str) -> bool:
    """Whether the listing endpoint is keyless (no key is sent).

    Keyless is not public: a keyless listing may be a LAN host.
    """

    return _LISTING_SUPPORT.get(provider_id, {}).get("auth") == "none"


def _listing_url(descriptor: Mapping[str, str], transport: Mapping[str, Any]) -> str:
    base = str(transport.get("base_url", "")).rstrip("/")
    url = descriptor.get("url")
    if url:
        return url.replace("{base}", base)
    return base + "/v1/models"


def listing_endpoint(provider_id: str, providers: dict[str, Any]) -> str:
    """The exact URL a listing would fetch (for the pane's consent text)."""

    descriptor = _LISTING_SUPPORT.get(provider_id, {})
    return _listing_url(descriptor, providers[provider_id]["transport"])


_CREATED_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})?)?$")


def list_provider_models(
    provider_id: str,
    providers: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    fetch: Callable[[str, dict[str, str]], bytes] | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """List the models a provider advertises — an EXPLICIT provider call.

    Runs only on explicit operator invocation (the `discover` command, or a
    confirmed fetch in the providers pane) — the invocation is the per-call
    approval; never from doctor or any automatic path. Secrets are read for
    the request and never logged, returned, or embedded in errors.
    """

    provider = providers[provider_id]
    if provider.get("origin") in OPERATOR_PROVIDER_ORIGINS:
        # A T2 provider block is never listed here (its route may be
        # unapproved); refuse before any secret read or call.
        raise ProxyError(OPERATOR_LISTING_REFUSAL.format(provider_id=provider_id))
    transport = provider["transport"]
    descriptor = _LISTING_SUPPORT.get(provider_id, {})
    if descriptor.get("status") == "unsupported":
        note = descriptor.get("note") or "no listing endpoint is known"
        raise ProxyError(
            f"provider {provider_id!r} does not support model listing ({note})"
        )
    if transport["kind"] == "oauth-pool":
        hint = f" ({descriptor['note']})" if descriptor.get("note") else ""
        raise ProxyError(
            f"provider {provider_id!r} is an OAuth pool — there is no "
            f"direct API credential to list models with{hint}"
        )
    # status "verified" (shape probed) or "attempt" (generic try on the
    # configured base); url/auth/shape may be overridden per provider.
    public = descriptor.get("auth") == "none"
    secret: str | None = None
    if not public:
        auth_block = transport.get("auth")
        secret_ref = auth_block.get("secret_ref") if isinstance(auth_block, dict) else None
        if not isinstance(secret_ref, str) or not secret_ref.startswith("env:"):
            # A keyless transport (auth kind none) without a public listing
            # descriptor: there is no credential to list with (research #8 i
            # — this used to be a KeyError traceback).
            kind = auth_block.get("kind") if isinstance(auth_block, dict) else None
            raise ProxyError(
                f"provider {provider_id!r} has no secret_ref to list models with "
                f"(transport auth {kind or 'missing'}); no listing endpoint is known"
            )
        env_name = secret_ref.removeprefix("env:")
        secret = resolve_secret(env_name, environ=environ)
        if secret is None:
            raise ProxyError(
                f"provider {provider_id!r} listing needs {secret_ref} in the "
                "secret env file first"
            )
    url = _listing_url(descriptor, transport)
    if public:
        headers = {}
    elif descriptor.get("auth") == "bearer":
        # Listing lives on a different surface than the Anthropic route
        # (deepseek: OpenAI-shape /models takes Bearer; the provider's
        # configured header auth stays for the route itself).
        headers = {"Authorization": f"Bearer {secret}"}
    else:
        auth = transport["auth"]
        if auth["kind"] == "bearer":
            headers = {"Authorization": f"Bearer {secret}"}
        else:
            headers = {auth["header"]: secret}
    # The Anthropic protocol header belongs on Anthropic-shape endpoints only
    # (OpenAI-shape listings — e.g. OpenRouter's public one — ignore it).
    if descriptor.get("shape", "anthropic") == "anthropic":
        headers["anthropic-version"] = "2023-06-01"

    try:
        raw = (fetch or _bounded_fetch(timeout))(url, headers)
    except Exception as exc:
        # Never interpolate the exception message: a crafted error could
        # carry request details. The HTTP status / connection reason are
        # safe and actionable (they never contain request data).
        raise ProxyError(
            f"provider {provider_id!r} model listing failed ({_failure_detail(exc)})"
        ) from exc
    try:
        return list(parse_listing(raw, descriptor.get("shape", "anthropic")).entries)
    except ListingShapeError as exc:
        raise ProxyError(
            f"provider {provider_id!r} model listing returned an unexpected "
            f"shape ({exc.detail})"
        ) from exc


# --- bounded listing transport and parser -------------------------------------
# Every listing/feed request: one GET, 20 s total wall-clock (a socket read
# timeout alone is not enough — a slow drip could outlive it), 4 MiB
# cumulative response bytes, no redirects (urllib would forward the
# credential header), no cookies, no retries, no fallback endpoint. Remote
# HTTPS keeps the urllib transport (environment proxies included);
# loopback is never a listing target.
LISTING_DEADLINE_SECONDS = 20.0
LISTING_MAX_BYTES = 4 * 1024 * 1024


class ListingFailure(ProxyError):
    """A bounded listing request failed; ``kind`` is a fixed enum and the
    text never carries request or response data."""

    def __init__(self, kind: str, status: int | None = None, detail: str | None = None):
        self.kind = kind
        self.status = status
        text = {
            "http": f"HTTP {status}",
            "redirect": f"redirect refused (HTTP {status})",
            "timeout": "20 s wall-clock cap reached",
            "oversize": "response exceeded the 4 MiB cap",
            "connection": f"connection error{': ' + detail if detail else ''}",
            "scheme": "only http and https URLs are listed",
            "protocol": "malformed HTTP response",
        }.get(kind, kind)
        super().__init__(text)


class ListingShapeError(ProxyError):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"unexpected listing shape ({detail})")


@dataclass(frozen=True)
class ListingResult:
    """Normalized entries; ``complete`` is False when the provider says more
    pages exist (absence can then never be concluded)."""

    entries: tuple[dict[str, Any], ...]
    complete: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the Authorization /
    x-api-key header to the redirect target — including an HTTPS→HTTP
    downgrade. A 3xx is a failed listing."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _failure_detail(exc: BaseException) -> str:
    if isinstance(exc, ListingFailure):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        detail = f"HTTP {exc.code}"
        exc.close()
        return detail
    if isinstance(exc, urllib.error.URLError):
        return f"connection error: {exc.reason}"
    return type(exc).__name__


def _response_socket(response: Any) -> Any:
    """Best effort: the socket under a urllib response (an injected opener)."""

    raw = getattr(getattr(response, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


def _cut_socket(sock: Any) -> None:
    """Shut a socket down from another thread: a blocked read or handshake
    returns at once. The base-class call works for TLS sockets too (an
    ``SSLSocket`` refuses ``dup()`` and its own ``shutdown`` unwraps)."""

    import socket

    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError, ValueError):
        pass


class _ListingDeadline:
    """One absolute deadline over resolution, connection, TLS, the request,
    the response headers and the body.

    A timer shuts down every socket the request opened when the deadline
    passes; resolution runs in a helper thread the request stops waiting
    for, so a stalled resolver can never lead to a later connect or send.
    """

    def __init__(self, seconds: float, clock: Callable[[], float]):
        import threading

        self._seconds = seconds
        self._clock = clock
        self._start = clock()
        self._lock = threading.Lock()
        self._sockets: list[Any] = []
        self._expired = False
        self._done = False
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def left(self) -> float:
        return self._seconds - (self._clock() - self._start)

    def over(self) -> bool:
        return self._expired or self.left() <= 0

    def timeout(self) -> float:
        return max(0.001, self.left())

    def check(self) -> None:
        if self.over():
            raise TimeoutError("listing deadline")

    def register(self, sock: Any) -> None:
        with self._lock:
            if self._done:
                return
            self._sockets.append(sock)
            if self._expired:
                _cut_socket(sock)

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self._expired = True
            for sock in self._sockets:
                _cut_socket(sock)

    def finish(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._done = True
            self._sockets.clear()

    def resolve(self, host: str, port: int) -> list[Any]:
        import socket
        import threading

        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["infos"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            except OSError as exc:
                outcome["error"] = type(exc)

        worker = threading.Thread(target=work, name="claude-multi-listing-resolve", daemon=True)
        worker.start()
        worker.join(max(0.0, self.left()))
        self.check()
        if "infos" in outcome:
            return outcome["infos"]
        if "error" in outcome:
            raise outcome["error"]("name resolution failed")
        raise TimeoutError("listing deadline")

    def connect(self, address: tuple[str, int]) -> Any:
        import socket

        host, port = address
        failure: type[OSError] | None = None
        for family, kind, proto, _canonical, target in self.resolve(host, port):
            self.check()
            sock = socket.socket(family, kind, proto)
            self.register(sock)
            try:
                sock.settimeout(self.timeout())
                sock.connect(target)
            except OSError as exc:
                failure = type(exc)
                sock.close()
                continue
            self.check()
            return sock
        self.check()
        raise (failure or OSError)("connection failed")


def _bounded_http_classes(deadline: _ListingDeadline) -> tuple[Any, Any]:
    import http.client

    class Plain(http.client.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = lambda address, *_rest, **_kw: deadline.connect(address)

    class Secure(http.client.HTTPSConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = lambda address, *_rest, **_kw: deadline.connect(address)

        def connect(self) -> None:
            http.client.HTTPConnection.connect(self)
            server_hostname = self._tunnel_host or self.host
            # The handshake runs on a socket the deadline already holds.
            sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname,
                                             do_handshake_on_connect=False)
            self.sock = sock
            deadline.register(sock)
            deadline.check()
            sock.settimeout(deadline.timeout())
            sock.do_handshake()
            deadline.check()

    return Plain, Secure


def _bounded_opener(deadline: _ListingDeadline) -> Any:
    plain, secure = _bounded_http_classes(deadline)

    class PlainHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(plain, req)

    class SecureHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(secure, req, context=self._context)

    from claude_multi import tls

    return urllib.request.build_opener(_NoRedirect(), PlainHandler(), SecureHandler(context=tls.context()))


def bounded_get(
    url: str, headers: Mapping[str, str], *,
    deadline: float = LISTING_DEADLINE_SECONDS, max_bytes: int = LISTING_MAX_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """One bounded GET; raises :class:`ListingFailure` on any failure.

    Every failure is a fixed, value-free :class:`ListingFailure` raised
    without exception context: a malformed status line or chunk header can
    echo request data, so neither its text nor its traceback may surface.
    """

    import http.client
    import socket
    import urllib.error
    import urllib.parse

    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ListingFailure("scheme")
    bound = _ListingDeadline(deadline, clock)
    response = None
    try:
        failure: ListingFailure | None = None
        try:
            request = urllib.request.Request(url, headers=dict(headers), method="GET")
            response = _bounded_opener(bound).open(request, timeout=bound.timeout())
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            failure = ListingFailure("redirect" if 300 <= code < 400 else "http", code)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)) or bound.over():
                failure = ListingFailure("timeout")
            else:
                failure = ListingFailure("connection", detail=type(exc.reason).__name__)
        except (socket.timeout, TimeoutError):
            failure = ListingFailure("timeout")
        except OSError as exc:
            failure = (ListingFailure("timeout") if bound.over()
                       else ListingFailure("connection", detail=type(exc).__name__))
        except (http.client.HTTPException, ValueError):
            failure = ListingFailure("timeout") if bound.over() else ListingFailure("protocol")
        if failure is not None:
            raise failure from None
        # A deadline cut during the headers can still parse as a response.
        if bound.over():
            raise ListingFailure("timeout")
        sock = _response_socket(response)
        if sock is not None:
            bound.register(sock)
        chunks: list[bytes] = []
        total = 0
        while True:
            if bound.over():
                raise ListingFailure("timeout")
            if sock is not None:
                try:
                    sock.settimeout(bound.timeout())
                except OSError:
                    pass
            try:
                chunk = response.read1(min(65536, max_bytes + 1 - total))
            except (socket.timeout, TimeoutError):
                failure = ListingFailure("timeout")
            except OSError as exc:
                failure = (ListingFailure("timeout") if bound.over()
                           else ListingFailure("connection", detail=type(exc).__name__))
            except (http.client.HTTPException, ValueError):
                failure = ListingFailure("timeout") if bound.over() else ListingFailure("protocol")
            if failure is not None:
                raise failure from None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ListingFailure("oversize")
        if bound.over():
            raise ListingFailure("timeout")
        return b"".join(chunks)
    finally:
        bound.finish()
        if response is not None:
            response.close()


def _bounded_fetch(deadline: float = LISTING_DEADLINE_SECONDS, max_bytes: int = LISTING_MAX_BYTES):
    def fetch(target: str, request_headers: dict[str, str]) -> bytes:
        return bounded_get(target, request_headers, deadline=deadline, max_bytes=max_bytes)

    return fetch


def _openrouter_facts(item: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded advertised facts, never arbitrary provider metadata."""

    from decimal import Decimal, InvalidOperation

    prices = {}
    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        for name in ("prompt", "completion"):
            value = pricing.get(name)
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            text = str(value)
            if len(text) > 64:
                continue
            try:
                number = Decimal(text)
                if number.is_finite() and number >= 0:
                    prices[name] = "0" if number == 0 else str(number)
            except InvalidOperation:
                pass
    parameters = item.get("supported_parameters")
    tools = ("tools" in parameters if isinstance(parameters, list)
             and all(isinstance(value, str) for value in parameters) else None)
    architecture = item.get("architecture")
    modality = architecture.get("modality") if isinstance(architecture, dict) else None
    if not isinstance(modality, str) or not re.fullmatch(r"[a-z+]+->[a-z+]+", modality) or len(modality) > 96:
        modality = None
    return {"openrouter": True, "pricing": prices, "tools": tools, "modality": modality}


def _openrouter_model_item(item: dict[str, Any]) -> dict[str, Any]:
    """Endpoint-specific facts are model-wide only when every endpoint agrees."""

    endpoints = item.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints or not all(isinstance(row, dict) for row in endpoints):
        return item
    result = dict(item)
    contexts = [row.get("context_length") for row in endpoints]
    if ("context_length" not in result
            and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in contexts)
            and all(value == contexts[0] for value in contexts)):
        result["context_length"] = contexts[0]
    facts = [_openrouter_facts(row) for row in endpoints]
    tools = [row["tools"] for row in facts]
    if "supported_parameters" not in result and tools[0] is not None and all(value is tools[0] for value in tools):
        result["supported_parameters"] = ["tools"] if tools[0] else []
    if "pricing" not in result:
        pricing = {}
        for name in ("prompt", "completion"):
            values = [row["pricing"].get(name) for row in facts]
            if values[0] is not None and all(value == values[0] for value in values):
                pricing[name] = values[0]
        result["pricing"] = pricing
    return result


def parse_listing(raw: bytes, shape: str) -> ListingResult:
    """Normalize an anthropic- or openai-shaped listing body.

    A 200 envelope without a ``data`` list is an unexpected shape, never an
    empty listing. Anthropic ``has_more: true`` makes the result incomplete.
    """

    try:
        payload = strict_json.loads(raw)
        items = payload.get("data") if isinstance(payload, dict) else None
        if shape == "openrouter-model":
            if not isinstance(items, dict) or not isinstance(items.get("id"), str):
                raise TypeError("the model envelope is not {data: {id: ...}}")
            items = [_openrouter_model_item(items)]
        if items is None or not isinstance(items, list):
            raise TypeError("the listing envelope is not {data: [...]}")
        entries = []
        openrouter_shape = shape in {"openrouter", "openrouter-model"}
        openai_shape = shape == "openai" or openrouter_shape
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            display = item.get("display_name") or item.get("name")
            context_length = item.get("context_length")
            # JSON booleans are ints in Python — exclude them explicitly.
            context_ok = isinstance(context_length, int) and not isinstance(context_length, bool)
            entry = {
                "id": item["id"],
                "display_name": display if isinstance(display, str) else "",
                "context_length": context_length if context_ok else None,
            }
            # research #8 iv: OpenAI shape `created` (unix seconds),
            # Anthropic shape `created_at` (RFC 3339). Anything else dropped.
            created = item.get("created")
            if isinstance(created, int) and not isinstance(created, bool) and created > 0:
                entry["created"] = created
            else:
                created_at = item.get("created_at")
                if isinstance(created_at, str) and _CREATED_AT.fullmatch(created_at):
                    entry["created"] = created_at
            efforts = item.get("think_efforts")
            if isinstance(efforts, dict) and efforts.get("valid_efforts"):
                entry["think_efforts"] = [str(e) for e in efforts["valid_efforts"] if isinstance(e, str)]
            if openai_shape:
                top = item.get("top_provider")
                max_completion = top.get("max_completion_tokens") if isinstance(top, dict) else None
                if isinstance(max_completion, int) and not isinstance(max_completion, bool):
                    entry["max_completion_tokens"] = max_completion
                reasoning = item.get("reasoning")
                if isinstance(reasoning, dict) and isinstance(reasoning.get("supported_efforts"), list):
                    entry["think_efforts"] = [
                        str(e) for e in reasoning["supported_efforts"] if isinstance(e, str)
                    ]
            if openrouter_shape:
                entry.update(_openrouter_facts(item))
                if not context_ok or context_length <= 0:
                    entry["context_length"] = None
            entries.append(entry)
        complete = shape != "openrouter-model" and not (isinstance(payload, dict) and payload.get("has_more") is True)
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        # RecursionError: deeply nested bodies far below the byte cap.
        raise ListingShapeError(type(exc).__name__) from exc
    return ListingResult(tuple(entries), complete)


def listing_headers(auth: str, shape: str, *, secret: str | None, header: str | None) -> dict[str, str]:
    """The request headers of one planned listing call (the secret only in
    the auth header; the Anthropic protocol header only on that shape)."""

    if auth == "none":
        headers: dict[str, str] = {}
    elif secret is None:
        raise ProxyError("a keyed listing needs its credential")
    elif auth == "bearer":
        headers = {"Authorization": f"Bearer {secret}"}
    elif auth == "header" and header:
        headers = {header: secret}
    else:
        raise ProxyError(f"listing auth {auth!r} is not a direct credential")
    if shape == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    return headers


# --- discover openai ----------------------------------------------------------
# The codex plan's model list, through ONE codex pool credential. The request
# mirrors the pinned v7.3.15 `cmd/fetch_codex_models` (GET
# <chatgpt>/backend-api/codex/models?client_version=…, Bearer access token,
# Chatgpt-Account-Id, codex originator + User-Agent) but never refreshes or
# saves a token: the gateway owns the credential records, and a refresh from
# here would rotate the refresh token behind its back. An expired token is a
# refusal, never a write.

CODEX_MODELS_URL = _LISTING_SUPPORT["openai"]["url"]
CODEX_USER_AGENT = f"codex_cli_rs/{CODEX_CLIENT_VERSION} (Mac OS 26.3.1; arm64) iTerm.app/3.6.9"
CODEX_ORIGINATOR = "codex_cli_rs"
# Upstream refreshes a token that expires within 30 s; this refuses instead.
CODEX_TOKEN_LEEWAY_SECONDS = 30
# The same expiry keys upstream reads (sdk/cliproxy/auth/types.go expireKeys).
_EXPIRY_KEYS = ("expired", "expire", "expires_at", "expiresAt", "expiry", "expires")
# The codex listing carries long per-model instruction strings (21 KiB in the
# pinned embedded copy); the byte cap stays strict_json's 4 MiB.
_CODEX_LISTING_LIMITS = strict_json.JSONLimits(max_string=1024 * 1024)
_CODEX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def gateway_auth_dir(
    gateway_info: Mapping[str, Any], environ: dict[str, str] | None = None
) -> Path:
    """The gateway auth dir: catalog `auth_dir`, strictly HOME-relative."""

    relative = Path(str(gateway_info["auth_dir"]))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ProxyError("catalog gateway auth_dir must be a HOME-relative path")
    return _home(environ) / relative


def _codex_expiry(document: Mapping[str, Any], token: str) -> datetime.datetime | None:
    """Access-token expiry: the JWT `exp` claim first (upstream precedence),
    else the credential file's expiry keys. None when neither is readable."""

    parts = token.split(".")
    if len(parts) == 3:
        try:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            exp = claims.get("exp") if isinstance(claims, dict) else None
            if isinstance(exp, (int, float)) and not isinstance(exp, bool):
                return datetime.datetime.fromtimestamp(exp, tz=datetime.timezone.utc)
        except (ValueError, UnicodeError, OverflowError, OSError):
            pass
    for key in _EXPIRY_KEYS:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            try:
                parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if value > 10**12 else value
            try:
                return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
    return None


def _codex_credential(
    auth_dir: Path, *, now: datetime.datetime
) -> tuple[str, str, str | None]:
    """(file name, access token, account id) of ONE usable codex credential.

    Reads only `codex-*.json` regular files (symlinks refused, group/other
    access refused — the gateway writes them 0600), skips disabled records,
    picks the first by name. Nothing is written. Errors name files, never
    values.
    """

    _require_private_dir(auth_dir, "gateway auth dir")
    names = sorted(
        entry.name
        for entry in os.scandir(auth_dir)
        if entry.name.startswith("codex-") and entry.name.endswith(".json")
    )
    if not names:
        raise ProxyError(
            f"no codex-*.json credential in {auth_dir} — sign in first "
            "(`claude-multi providers sign-in openai`)"
        )
    expired: list[str] = []
    for name in names:
        path = auth_dir / name
        try:
            raw = state.read_private(path)
        except state.StateError as exc:
            # A symlinked/foreign/group-readable credential is never read.
            raise ProxyError(f"codex credential {name} refused: {exc.strerror or exc}") from exc
        try:
            document = strict_json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ProxyError(f"codex credential {name} is not valid JSON") from exc
        if not isinstance(document, dict) or document.get("disabled") is True:
            continue
        token = document.get("access_token")
        if not isinstance(token, str) or not token.strip():
            continue
        token = token.strip()
        expiry = _codex_expiry(document, token)
        if expiry is None or expiry <= now + datetime.timedelta(
            seconds=CODEX_TOKEN_LEEWAY_SECONDS
        ):
            expired.append(name)
            continue
        account = document.get("account_id")
        account_id = account.strip() if isinstance(account, str) and account.strip() else None
        return name, token, account_id
    if expired:
        raise ProxyError(
            f"codex access token expired ({', '.join(expired)}) — let the gateway "
            "refresh it (any codex request) and retry; discover never refreshes "
            "or writes credentials"
        )
    raise ProxyError(f"no enabled codex credential with an access token in {auth_dir}")


def list_codex_plan_models(
    gateway_info: Mapping[str, Any],
    *,
    environ: dict[str, str] | None = None,
    fetch: Callable[[str, dict[str, str]], bytes] | None = None,
    now: datetime.datetime | None = None,
    timeout: float = 20.0,
) -> list[dict[str, str | None]]:
    """The codex plan's models — ONE real provider request.

    Each entry is ``{"id", "visibility", "upgrade", "retirement_at"}``
    (research #8 iii: the upstream successor and retirement date stay in
    the output; malformed values read as None). Callers MUST obtain an
    explicit per-call confirmation first; this function
    performs the call unconditionally. The access token travels only in the
    Authorization header; it is never returned, printed, or interpolated
    into an error.
    """

    auth_dir = gateway_auth_dir(gateway_info, environ)
    current = now or datetime.datetime.now(tz=datetime.timezone.utc)
    _name, token, account_id = _codex_credential(auth_dir, now=current)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Originator": CODEX_ORIGINATOR,
        "User-Agent": CODEX_USER_AGENT,
    }
    if account_id is not None:
        headers["Chatgpt-Account-Id"] = account_id

    # The bounded transport (20 s wall-clock, 4 MiB, no redirects —
    # the bearer token would be forwarded — no cookies, no retries).
    try:
        raw = (fetch or _bounded_fetch(timeout, _CODEX_LISTING_LIMITS.max_bytes))(CODEX_MODELS_URL, headers)
    except Exception as exc:
        raise ProxyError(f"codex plan model listing failed ({_failure_detail(exc)})") from exc
    try:
        payload = strict_json.loads(raw, _CODEX_LISTING_LIMITS)
        items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise TypeError("the listing envelope is not {models: [...]}")
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ProxyError(
            f"codex plan model listing returned an unexpected shape ({type(exc).__name__})"
        ) from exc
    models: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for item in items:
        slug = item.get("slug") if isinstance(item, dict) else None
        if isinstance(slug, str) and _CODEX_ID.fullmatch(slug) and slug not in seen:
            seen.add(slug)
            models.append({"id": slug, **catalog_mod.codex_upgrade_fields(item)})
    return models


# --- auth-dir snapshot --------------------------------------------------------
# 7.3 names new codex credential files differently at login and rewrites
# metadata on save (existing files are saved in place), so the
# rollback to a 7.2.80 generation needs the pre-activation auth dir back.

SNAPSHOT_TAG = "pre-7.3"
_SNAPSHOT_SUFFIX = re.compile(r"^pre-7\.3\.[0-9]{8}(?:\.[0-9]{1,2})?$")


def _require_private_dir(path: Path, what: str) -> None:
    """A real, owner-controlled, group/other-inaccessible directory."""

    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ProxyError(f"{what} {path} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProxyError(f"{what} {path} is not a real directory (symlinks refused)")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ProxyError(f"{what} {path} is not owner-private (0700)")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot_members(source: Path) -> list[Path]:
    """Relative member paths (dirs first, sorted); refuse anything unsafe."""

    members: list[Path] = []
    for root, dirs, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        for name in sorted(dirs) + sorted(files):
            # Patch 8's atomic-save staging file is not a credential. It may
            # disappear at any moment; skip before lstat/open, never copy it.
            # Real *.json records, and similarly named directories, still get
            # the ordinary safety and JSON checks.
            if name in files and re.fullmatch(r"\..+\.cm-save-[0-9a-f]{16}", name):
                continue
            path = root_path / name
            info = os.lstat(path)
            relative = path.relative_to(source)
            if stat.S_ISLNK(info.st_mode):
                raise ProxyError(f"refusing to snapshot: {relative} is a symlink")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ProxyError(f"refusing to snapshot: {relative} is not a regular file")
            if info.st_uid != os.geteuid():
                raise ProxyError(f"refusing to snapshot: {relative} is not owner-controlled")
            members.append(relative)
        dirs.sort()
    return members


def _copy_private_tree(source: Path, target: Path) -> list[str]:
    """Copy `source` into the NEW dir `target` (0700 dirs, 0600 files).

    Files are opened O_NOFOLLOW and copied by descriptor; contents are never
    decoded or logged. Returns the relative file names copied.
    """

    members = _snapshot_members(source)
    copied: list[str] = []
    for relative in members:
        src = source / relative
        dst = target / relative
        if stat.S_ISDIR(os.lstat(src).st_mode):
            os.mkdir(dst, 0o700)
            os.chmod(dst, 0o700)
            continue
        in_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            out_fd = os.open(
                dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            try:
                os.fchmod(out_fd, 0o600)
                while True:
                    chunk = os.read(in_fd, 1 << 16)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(out_fd, view)
                        view = view[written:]
                os.fsync(out_fd)
            finally:
                os.close(out_fd)
        finally:
            os.close(in_fd)
        copied.append(str(relative))
    return copied


def _stage_copy(source: Path, parent: Path, final_name: str) -> tuple[Path, list[str]]:
    """Copy `source` to a same-parent staging dir; the caller renames it."""

    staging = Path(tempfile.mkdtemp(prefix=f".{final_name}.", suffix=".tmp", dir=parent))
    os.chmod(staging, 0o700)
    try:
        names = _copy_private_tree(source, staging)
        # A credential caught mid-write by a running gateway would be torn:
        # every copied JSON record must still parse (contents never shown).
        for name in names:
            if name.endswith(".json"):
                try:
                    json.loads((staging / name).read_bytes())
                except (ValueError, RecursionError):
                    raise ProxyError(
                        f"copied {name} is not valid JSON (the gateway may have "
                        "been writing it) — nothing was kept; retry"
                    ) from None
        _fsync_dir(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, names


def snapshot_auth_dir(
    gateway_info: Mapping[str, Any],
    *,
    environ: dict[str, str] | None = None,
    today: datetime.date | None = None,
) -> tuple[Path, list[str]]:
    """Copy the auth dir to `<auth_dir>.pre-7.3.<YYYYMMDD>` (never overwrites).

    An existing snapshot for the same date gets a `.2`, `.3`… suffix; the
    copy lands via a staging dir + rename, so a crash leaves no partial
    snapshot under the final name. Returns (snapshot path, file names).
    """

    source = gateway_auth_dir(gateway_info, environ)
    _require_private_dir(source, "gateway auth dir")
    parent = source.parent
    stamp = (today or datetime.date.today()).strftime("%Y%m%d")
    base = f"{source.name}.{SNAPSHOT_TAG}.{stamp}"
    target = None
    for index in range(1, 100):
        candidate = parent / (base if index == 1 else f"{base}.{index}")
        if not os.path.lexists(candidate):
            target = candidate
            break
    if target is None:
        raise ProxyError(f"refusing to snapshot: 99 snapshots named {base}* already exist")
    staging, names = _stage_copy(source, parent, target.name)
    try:
        if os.path.lexists(target):
            raise ProxyError(f"refusing to overwrite existing snapshot {target}")
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _fsync_dir(parent)
    return target, names


_gateway_service_active = service.service_active


def restore_auth_dir(
    gateway_info: Mapping[str, Any],
    snapshot: str,
    *,
    environ: dict[str, str] | None = None,
    service_active: Callable[[str], bool] | None = None,
    gateway_status: Callable[..., service.GatewayStatus] | None = None,
    today: datetime.date | None = None,
) -> tuple[Path, Path | None, list[str]]:
    """Put a snapshot back as the auth dir (rollback; gateway must be stopped).

    The current auth dir is never deleted: it is moved aside to
    `<auth_dir>.replaced.<YYYYMMDD>[.N]`. The snapshot itself stays intact —
    the live dir is a fresh 0700/0600 copy of it. Returns (auth dir,
    moved-aside path or None, file names restored).
    """

    unit = str(gateway_info["service"])
    # A legacy manager injection alone is incomplete. Never fall through to
    # real health/PID reads from an old hermetic caller: refuse, without I/O.
    if service_active is not None and gateway_status is None:
        raise ProxyError(
            f"cannot tell whether {unit} is stopped without health and PID results; "
            f"{service.hint_code('stop')} first, then retry with a gateway-status check"
        )
    manager = None
    if service_active is not None:
        manager = lambda: "active" if service_active(unit) else "inactive"
    env = dict(os.environ if environ is None else environ)
    configured = endpoint.gateway_endpoint({"gateway": gateway_info})
    observed = (gateway_status or service.gateway_status)(
        base_url=configured.base_url, health_path=configured.health_path,
        state_root=sessions_mod.state_root(env), manager=manager,
    )
    if observed.state != "stopped":
        raise ProxyError(
            f"refusing to restore while {unit} is {observed.state} — "
            f"{service.hint_code('stop')} first, restore, then start it"
        )
    live = gateway_auth_dir(gateway_info, environ)
    parent = live.parent
    # The snapshot is named by its bare name or by the exact path
    # `snapshot-auth` printed (a sibling of the auth dir); nothing else.
    given = Path(snapshot)
    name = given.name
    if given.is_absolute():
        if Path(os.path.normpath(given)) != parent / name:
            raise ProxyError(f"snapshot must live next to the auth dir ({parent})")
    elif str(given) != name:
        raise ProxyError(
            f"snapshot must be a bare name like {live.name}.{SNAPSHOT_TAG}.YYYYMMDD "
            f"or its full path next to {live}"
        )
    if not name.startswith(f"{live.name}.") or not _SNAPSHOT_SUFFIX.fullmatch(
        name[len(live.name) + 1:]
    ):
        raise ProxyError(f"{name!r} is not an auth-dir snapshot name")
    source = parent / name
    _require_private_dir(source, "snapshot")
    staging, names = _stage_copy(source, parent, live.name)
    moved: Path | None = None
    try:
        if os.path.lexists(live):
            _require_private_dir(live, "current auth dir")
            stamp = (today or datetime.date.today()).strftime("%Y%m%d")
            base = f"{live.name}.replaced.{stamp}"
            for index in range(1, 100):
                candidate = parent / (base if index == 1 else f"{base}.{index}")
                if not os.path.lexists(candidate):
                    moved = candidate
                    break
            if moved is None:
                raise ProxyError(f"refusing to restore: 99 {base}* dirs already exist")
            os.rename(live, moved)
        os.rename(staging, live)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if moved is not None and not os.path.lexists(live) and os.path.lexists(moved):
            os.rename(moved, live)
        raise
    _fsync_dir(parent)
    return live, moved, names


def set_secret_value(path: Path, name: str, value: str) -> int:
    """Insert or replace ``NAME=value`` in the secret env file.

    A compatibility wrapper over :func:`secret_store.write_env_value`
    (the parse-preserving, locked, atomic 0600 writer, unchanged): every
    existing assignment of the name collapses into one, each line's
    ``export`` prefix and spacing is kept exactly, unrelated lines are
    untouched, and the candidate passes the strict parser before the write.
    The value is never logged or returned — only its length.
    """

    try:
        return secret_store.write_env_value(path, name, value)
    except secret_store.SecretStoreError as exc:
        raise ProxyError(str(exc)) from exc


def selected_secret_problems(
    resolved: Any,
    models: dict[str, Any],
    providers: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    store: secret_store.SecretStore | None = None,
) -> list[str]:
    """Offline secret readiness for providers selected by lead/variants.

    Only providers actually selected are checked (the lead and every bound
    agent); direct transports need a stored ``env:NAME`` value and keyed
    compat routes a USABLE one (missing, blank, non-string or
    invalid refuses, the render's own rule), while OAuth pools and keyless
    LAN providers stay usable. Reads go through the SecretStore interface
    (``store``, default :func:`secret_store.default_store`; a non-file
    backend works), never the process environment. Errors are redacted:
    display/reference names only, never values. No provider call, config
    write, token read/create, or readiness probe.
    """

    selected: list[str] = []
    seen: set[str] = set()
    for model_id in [resolved.lead.model, *(variant.model for variant in resolved.variants)]:
        provider_id = models[model_id]["provider"]
        if provider_id not in seen:
            seen.add(provider_id)
            selected.append(provider_id)

    backend = store if store is not None else secret_store.default_store(environ)
    path = backend.path
    where = f"the secret env file {path}" if path is not None else f"the secret store ({backend.description()})"
    read_error: str | None = None
    problems: list[str] = []
    for provider_id in selected:
        provider = providers[provider_id]
        transport = provider["transport"]
        keyed = catalog_mod.is_keyed_compat(provider)
        if transport["kind"] != "direct" and not keyed:
            continue
        display = provider["display"]
        secret_ref = transport["auth"]["secret_ref"]
        name = secret_ref.removeprefix("env:")
        if path is not None and not path.exists():
            problems.append(
                f"provider {display} ({provider_id}): required secret {secret_ref} "
                f"unavailable (secret env file {path} missing)"
            )
            continue
        if read_error is None:
            try:
                value: Any = backend.get(name) if keyed else None
                present = render_mod.usable_secret(value) if keyed else backend.is_set(name)
            except (secret_store.SecretStoreError, OSError) as exc:
                read_error = str(exc)
        if read_error is not None:
            source = f"secret env file {path}" if path is not None else f"secret store ({backend.description()})"
            problems.append(
                f"provider {display} ({provider_id}): {source} "
                f"unsafe or malformed: {read_error}"
            )
            continue
        if not present:
            if keyed and value is not None:
                problems.append(
                    f"provider {display} ({provider_id}): required secret {secret_ref} "
                    f"is blank or invalid in {where} (value not shown)"
                )
            else:
                problems.append(
                    f"provider {display} ({provider_id}): required variable {name} "
                    f"missing from {where}"
                )
    return problems


def ensure_directories(home: Path) -> None:
    """Create/validate owner-private dirs; existing auth records stay intact."""

    state.ensure_private_dir(state_dir(home))
    state.ensure_private_dir(state_dir(home) / "auth")
    state.ensure_private_dir(state_dir(home) / "traces")
    state.ensure_private_dir(config_dir(home))


def _refuse_gateway_dotenv(workdir: Path) -> None:
    if gateway_dotenv_present(workdir):
        raise ProxyError(
            f"gateway working directory {workdir} holds a .env file: CLIProxyAPI "
            "would load it (HOME_JWT, DEPLOY, WRITABLE_PATH and the store variables "
            "override the gateway's config); move it aside unless you put it there; "
            "the gateway was not started"
        )


def _read_token(path: Path) -> str:
    """Read a private key slot, refusing malformed bytes without echoing them."""

    try:
        # read_private's rule (regular, owner-only; 0400 is fine) — the same
        # as launch.read_gateway_token, so both readers agree on a slot.
        token = state.read_private(path).decode("ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ProxyError(f"gateway key file {path.name} is unusable (unavailable or unsafe)") from exc
    if not _TOKEN_SHAPE.fullmatch(token):
        raise ProxyError(f"gateway key file {path.name} has an invalid token shape")
    return token


def gateway_api_keys(
    home: Path | None = None, *, environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Read-only authority for render/doctor: (previous, current), or (current,).

    Paths deliberately remain HOME-relative, not XDG_CONFIG_HOME-relative.
    Symlinks (including dangling links), wrong modes and malformed slots refuse.
    Callers that write hold the api-key FileLock around this snapshot.
    """

    directory = config_dir(home if home is not None else _home(environ))
    current = _read_token(directory / "api-key")
    previous_path = directory / "previous-key"
    if os.path.lexists(previous_path):
        previous = _read_token(previous_path)
        if previous != current:
            return previous, current
    return (current,)


def ensure_token(home: Path) -> str:
    """Read or create the mode-0600 local gateway token. Never printed."""

    token_path = config_dir(home) / "api-key"
    if os.path.lexists(token_path):
        return _read_token(token_path)
    token = secrets.token_hex(32)
    state.atomic_write(token_path, (token + "\n").encode("utf-8"))
    return token


@dataclass(frozen=True)
class ContinuityReport:
    """Non-secret continuity outcome of one render.

    ``size`` counts the aliases in the working set, ``rendered`` those the
    render emitted, ``unservable`` record selectors nothing can serve,
    ``skipped_records`` unreadable record ids, ``corrupt`` the reason when
    ``continuity.json`` could not be used (the render then serves exactly
    the seed set and writes nothing). ``operator`` carries the
    sanitized operator-layer notices of this render (names and codes only).
    """

    size: int
    rendered: tuple[str, ...]
    unservable: tuple[str, ...]
    skipped_records: tuple[str, ...]
    notices: tuple[str, ...]
    corrupt: str | None
    operator: tuple[str, ...] = ()
    # The persisted ``continuity.state_root``
    # (the authority) and whether this render came from another root. An
    # other-root render publishes the catalog and declarations as a
    # same-root render would; it never extends, prunes or rewrites the
    # persisted continuity set (and never moves the authority).
    managed_root: str | None = None
    other_root: bool = False


# Operator-layer failure policy:
#   explicit — init / providers apply: invalid operator input refuses the
#              candidate (old config kept); live-reference loss and an
#              uncapturable emitted alias refuse too.
#   reload   — init --reload-check / --start-check, token rotation, alias
#              pruning: invalid or unapproved declarations degrade unless
#              dropping them breaks a live reference (or references are
#              unknown) or an emitted alias cannot be captured.
#   start    — init --prepare-start, unprepared run, logins: degrade with a
#              sanitized notice; a corrupt ledger drops every T2 contribution.
RENDER_POLICIES = ("explicit", "reload", "start")


def operator_snapshot(
    environ: Mapping[str, str], docs: Mapping[str, Any], legacy: Mapping[str, Any], *,
    asset_root: Path | str | None = None,
) -> operator_mod.OperatorSnapshot:
    """The one operator read shared by the render and doctor (same inputs,
    same bytes): providers.d, the ledger, the legacy registry and the
    stored secret values for the in-memory literal scan."""

    return operator_mod.load_snapshot(
        environ, docs, asset_root=asset_root if asset_root is not None else assets_root(dict(environ)),
        legacy=legacy, secret_values=lambda: _stored_secret_values(environ),
    )


def _stored_secret_values(environ: Mapping[str, str]) -> frozenset[str]:
    """The stored secret values for the in-memory literal scan (never raises;
    an unreadable store contributes nothing)."""

    try:
        return secret_store.default_store(environ).scan_values()
    except (errors.ClaudeMultiError, OSError, ValueError):
        return frozenset()


def overlay_static_wires(environ: Mapping[str, str],
                         asset_root: Path | str | None = None) -> dict[str, frozenset[str]] | None:
    """The pinned static registry ids per overlay channel, or None (unknown);
    the registry of the explicit ``asset_root`` when one is given."""

    directory = catalog_mod.registry_dir(dict(environ), asset_root)
    if directory is None:
        return None
    try:
        return operator_mod.static_overlay_wires(catalog_mod.load_pinned_registry(directory))
    except (errors.ClaudeMultiError, OSError, ValueError):
        return None


def _problem_lines(problems: Sequence[operator_mod.OperatorProblem], limit: int = 5) -> list[str]:
    lines = [problem.text() for problem in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"… and {len(problems) - limit} more")
    return lines


def _ids8(ids: Iterable[str]) -> str:
    return ", ".join(sorted(identifier[:8] for identifier in ids))


def _render_locked(
    home: Path, tokens: Sequence[str], *,
    resolver: Callable[[str], str | None] | None,
    environ: dict[str, str] | None,
    state_root: Path | None = None,
    policy: str = "explicit",
    barrier: state.BarrierToken | None = None,
) -> tuple[Path, render_mod.RenderResult, ContinuityReport]:
    """Write a render while the caller holds the api-key lock.

    Never re-acquires the api-key lock (``rotate_token`` already holds it)
    and never takes a record, lifecycle or runtime-index lock: the record
    scan is a lock-free read. ``continuity.json`` is written before
    ``config.yaml`` and only when it changed (or was absent). A corrupt
    file is left untouched; the render then serves exactly the seed set
    (``continuity.seed_only``, no record extension) — the same set doctor
    expects, so the corruption is one BLOCK and never drift.

    The operator layer joins the transaction. Every refusal happens
    before any write; then continuity, the ledger captures of every emitted
    T2 alias and last the config. A failed capture write never
    publishes the candidate config. ``policy`` is one of RENDER_POLICIES.
    """

    if policy not in RENDER_POLICIES:
        raise ValueError(f"unknown render policy {policy!r}")
    if barrier is not None:
        # A served mutation's render asserts the held
        # barrier token; unit, init and rotation renders never take it.
        state.require_barrier(barrier)
    env = dict(os.environ if environ is None else environ)
    bundle = load_bundle(environ, home=home)
    resolve = resolver or (lambda name: resolve_secret(name, environ=environ))
    legacy = custom_mod.load_registry(env)
    snapshot = operator_snapshot(env, bundle.docs, legacy)
    layer = snapshot.layer
    operator_notes: list[str] = []
    blockers = operator_mod.blocking_problems(layer)
    if blockers and policy == "explicit":
        raise ProxyError(
            "operator layer invalid — config not written:\n  " + "\n  ".join(_problem_lines(blockers))
            + "\nfix the named file(s) (claude-multi providers validate), then rerun"
        )
    for file in sorted({problem.file for problem in blockers}):
        operator_notes.append(f"{file} not rendered (invalid; claude-multi providers validate)")
    plan = operator_mod.render_plan(bundle.docs, layer, snapshot.ledger, legacy=legacy)
    if snapshot.ledger_error is not None and plan.layer.lines and policy == "start":
        plan = operator_mod.render_plan(
            bundle.docs, operator_mod.without_contributions(layer), None, legacy=legacy,
        )
        operator_notes.append(f"{snapshot.ledger_error}: operator lines not rendered")
    elif snapshot.ledger_error is not None:
        # explicit/reload: refused after the candidate render below only when
        # it actually emits T2 aliases (they could not be captured).
        operator_notes.append(snapshot.ledger_error)
    for pid, status in sorted(plan.not_rendered.items()):
        operator_notes.append(f"provider {pid} not rendered (route {status}; claude-multi providers approve {pid})")
    providers = plan.docs["providers"]["providers"]
    lines = plan.docs["models-v2"]["models"]
    notices: list[str] = []
    unservable: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    corrupt: str | None = None
    write_continuity = False
    try:
        persisted = continuity_mod.read(home)
    except continuity_mod.ContinuityError as exc:
        corrupt, persisted = str(exc), None
    # The persisted ``state_root`` is the
    # authority. It is set when continuity.json is created (or still null)
    # and moved only by ``init --adopt-root``; a render from another root
    # never rewrites it and never extends from its own records. Its live
    # reference checks read the managed root (read-only, lock-free).
    managed_root = persisted.get("state_root") if persisted is not None else None
    other_root = state_root is not None and managed_root is not None and managed_root != str(state_root)
    scan_root = Path(managed_root) if other_root else state_root
    scan = continuity_mod.scan_records(scan_root) if scan_root is not None else None
    if corrupt is not None:
        working = continuity_mod.seed_only(bundle)
    else:
        working = persisted if persisted is not None else continuity_mod.empty()
        working, seeded = continuity_mod.merge_seed(working, bundle)
        extended = False
        if other_root:
            notices.append(
                f"render from state root {state_root}; the gateway is managed for {managed_root}: "
                "the persisted continuity set is kept unchanged (additive render)"
            )
        elif scan is not None:
            # The complete candidate alias union (catalog,
            # current operator lines and retained captures) is served.
            served_by_render = render_mod.catalog_alias_set(providers, lines) | frozenset(plan.captures)
            working, extended, unservable = continuity_mod.extend_from_records(
                working, bundle, scan, served_by_render,
            )
            skipped = scan.unreadable
            notices.extend(scan.notices)
        else:
            notices.append("record extension skipped: no state root")
        claimed = False
        if state_root is not None and working["state_root"] is None:
            working["state_root"] = str(state_root)
            claimed = True
        write_continuity = not other_root and (persisted is None or seeded or extended or claimed)
    result = render_mod.render_config(
        plan.docs["gateway"], providers, lines,
        home=home, gateway_tokens=tokens, resolve_secret=resolve,
        continuity=working["aliases"], captures=plan.captures,
        provider_headers=plan.headers, oauth_overlay=plan.overlay,
        overlay_static=overlay_static_wires(env), quiet_providers=plan.quiet_providers,
        retired=(plan.docs.get("retired") or {}).get("retired"),
    )
    notices.extend(result.notices)
    operator_notes.extend(conflict.text() for conflict in result.overlay_conflicts)
    if policy != "start" and snapshot.ledger_error is not None:
        # The corrupt ledger is never overwritten, so a candidate that
        # emits T2 aliases (after credential filtering) cannot capture them.
        if operator_mod.emitted_operator_aliases(plan, result.served):
            raise ProxyError(
                f"{snapshot.ledger_error}: operator aliases cannot be captured — config not written; "
                "move ~/.config/claude-multi/operator-ledger.json aside only after reviewing it"
            )
    declared = operator_mod.declared_aliases(layer, snapshot.read)
    # Capture history absent or corrupt while providers.d exists (or a
    # ledger path existed): a live reference to an unserved reserved-namespace
    # alias is unsafe whatever declared_aliases could extract (a malformed or
    # secret-bearing declaration yields no names). Pure legacy is unchanged.
    history_unknown = snapshot.ledger is None and (
        snapshot.ledger_present or (snapshot.read is not None and snapshot.read.exists)
    )

    def refuse_lost(parts: list[str]) -> None:
        if parts:
            raise ProxyError(
                "operator: this render would stop serving aliases live sessions use — config not written: "
                + "; ".join(parts[:5])
                + " — restore (or approve) the named providers.d file, or end the session(s); "
                + sessions_mod.mark_ended_remedy()
            )

    def unproven(skip: Iterable[str] = ()) -> list[str]:
        if not history_unknown or scan is None:
            return []
        skipped = set(skip)
        refs = {alias: ids for alias, ids in scan.refs.items() if alias not in skipped}
        return [f"alias {alias} ({label}) used by {_ids8(holders)}"
                for alias, label, holders in operator_mod.unproven_live_references(
                    refs, scan.live, result.served, declared, secret_values=lambda: _stored_secret_values(env),
                )]

    if policy != "start" and scan is not None:
        if snapshot.ledger_error is not None:
            if scan.unreadable or scan.directory_error:
                raise ProxyError(
                    f"{snapshot.ledger_error}: cannot prove liveness (unreadable session records) while "
                    "operator capture history is unknown — config not written"
                )
            refuse_lost(unproven())
            unknown = {base for base, ids in scan.refs.items() if ids & scan.live and base not in result.served
                       and base not in working["aliases"]}
            if unknown:
                raise ProxyError(
                    f"{snapshot.ledger_error}: live session references ({', '.join(sorted(unknown)[:5])}) "
                    "may be operator captures this render cannot prove — config not written"
                )
        else:
            lost = operator_mod.lost_live_references(
                snapshot.ledger, result.served, scan.refs, scan.live, declared=declared,
            )
            parts = [f"alias {alias} ({label}) used by {_ids8(holders)}"
                     for alias, (label, holders) in sorted(lost.items())]
            refuse_lost(parts + unproven(skip=lost))
            # Without a ledger there is no capture history: every declared
            # alias the candidate omits may be one a session uses.
            known = set(snapshot.ledger.aliases) if snapshot.ledger is not None else set(declared)
            dropped = [alias for alias in known if alias not in result.served]
            if dropped and (scan.unreadable or scan.directory_error):
                raise ProxyError(
                    "operator: cannot prove liveness (unreadable session records) while this render "
                    f"drops operator aliases ({', '.join(sorted(dropped)[:5])}) — config not written"
                )
    elif policy == "start" and scan is not None and snapshot.ledger_error is None:
        lost = operator_mod.lost_live_references(
            snapshot.ledger, result.served, scan.refs, scan.live, declared=declared,
        )
        for alias, (label, holders) in sorted(lost.items()):
            operator_notes.append(f"alias {alias} ({label}) is used by {_ids8(holders)} but not served")
    captured = operator_mod.plan_alias_capture(
        snapshot.ledger, plan, result.served, catalog_version=int(bundle.docs["version"]["catalog_version"]),
    )
    if write_continuity:
        continuity_mod.write(home, working)
    if captured is not None and snapshot.ledger_error is None:
        schemas = snapshot.schemas or operator_mod.load_schemas(assets_root(environ))
        try:
            operator_mod.write_ledger(env, captured, schemas)
        except (state.StateError, OSError, operator_mod.OperatorError) as exc:
            raise ProxyError(
                f"operator alias capture could not be committed ({type(exc).__name__}) — "
                "config not published; rerun claude-multi-proxy init"
            ) from exc
    target = config_dir(home) / "config.yaml"
    report = ContinuityReport(
        size=len(working["aliases"]),
        rendered=result.continuity_rendered,
        unservable=tuple(unservable),
        skipped_records=tuple(skipped),
        notices=tuple(notices),
        corrupt=corrupt,
        operator=tuple(operator_notes),
        managed_root=managed_root,
        other_root=other_root,
    )
    try:
        state.atomic_write(target, result.yaml.encode("utf-8"))
    except state.CommittedStateError as exc:
        # Published, durability unconfirmed: never report it as unpublished.
        raise ConfigPublishedError(exc, (target, result, report)) from exc
    return target, result, report


def render_runtime_config(
    home: Path,
    *,
    resolver: Callable[[str], str | None] | None = None,
    environ: dict[str, str] | None = None,
    state_root: Path | None = None,
    policy: str = "explicit",
    barrier: state.BarrierToken | None = None,
) -> tuple[Path, render_mod.RenderResult, ContinuityReport]:
    """Atomically render the key slots under the shared api-key FileLock.

    init/run/login preserve a published dual-key window across gateway restarts.
    An interrupted, unpublished rotation (previous == current) deduplicates.
    ``state_root`` enables the continuity record extension (None skips it).
    ``policy`` is the operator failure policy (RENDER_POLICIES).
    ``barrier``: a served mutation passes its held token, which is
    asserted, never acquired; ordinary, unit, init and rotation renders pass
    none. A fenced caller's managed root is re-read under the lock
    (``gateway_inhibition.revalidate``): an adoption since its fence refuses.
    """

    if barrier is not None:
        state.require_barrier(barrier)
    state.ensure_private_dir(config_dir(home))
    lock = state.FileLock(config_dir(home) / "api-key")
    lock.acquire(blocking=True)
    try:
        gateway_inhibition.revalidate(home, what="the gateway configuration was not written")
        ensure_token(home)
        return _render_locked(
            home, gateway_api_keys(home), resolver=resolver, environ=environ,
            state_root=state_root, policy=policy, barrier=barrier,
        )
    finally:
        lock.release()


# ------------------------------------------------------------ served-change planning
# The served-change planner's two sides.
# Both read only: no lock, no state write, no secret value (credential
# presence only), no network.
PLAN_PRESENT_VALUE = "present-value-not-read"


@dataclass(frozen=True)
class PublishedIdentity:
    """``config.yaml`` as published: identities only, or why it is unknown."""

    routes: dict[str, served_plan_mod.RouteIdentity] | None
    unknown: str | None
    gateway: str


def published_identity(home: Path) -> PublishedIdentity:
    """The published render (never a re-render); missing/unparseable is unknown."""

    target = config_dir(home) / "config.yaml"
    try:
        raw = state.read_private(target)
    except state.StateError as exc:
        reason = "no published gateway config" if exc.errno == 2 else "published gateway config unreadable"
        return PublishedIdentity(None, reason, "unknown")
    except OSError:
        return PublishedIdentity(None, "published gateway config unreadable", "unknown")
    try:
        document = served_plan_mod.parse_restricted_yaml(raw.decode("utf-8"))
        routes = served_plan_mod.routes_from_document(document)
    except (served_plan_mod.PlanParseError, UnicodeDecodeError, ValueError):
        return PublishedIdentity(None, "published gateway config is not in the rendered form", "unknown")
    return PublishedIdentity(routes, None, served_plan_mod.gateway_label(document))


def candidate_document(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    asset_root: Path | str | None = None,
    layer: operator_mod.OperatorLayer | None = None,
    ledger: operator_mod.OperatorLedger | None = None,
    replace_ledger: bool = False,
    state_root: Path | None = None,
    assume_present: Iterable[str] = (),
    assume_absent: Iterable[str] = (),
    continuity_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The document a render of the given inputs would publish (pure).

    ``asset_root`` selects candidate assets (``plan --assets``); ``layer``
    and ``ledger`` (with ``replace_ledger``) are a command's proposed
    operator state; ``continuity_override`` is a proposed continuity set
    (the alias prune's post-prune document). Secrets resolve to a fixed placeholder when present and
    to nothing when absent, so availability matches a real render while no
    value is read into the plan; ``assume_present`` / ``assume_absent`` name
    secrets a proposed key save or removal changes. Record extension follows
    the root authority exactly as :func:`_render_locked` does.
    """

    env = dict(os.environ if environ is None else environ)
    root = Path(asset_root) if asset_root is not None else assets_root(dict(env))
    bundle = load_bundle(env, home=home, root=root)
    legacy = custom_mod.load_registry(env)
    snapshot = operator_snapshot(env, bundle.docs, legacy, asset_root=root)
    use_layer = snapshot.layer if layer is None else layer
    use_ledger = ledger if replace_ledger else snapshot.ledger
    if layer is None and operator_mod.blocking_problems(use_layer):
        use_layer = operator_mod.renderable_layer(use_layer)
    if use_ledger is None and snapshot.ledger_error is not None:
        use_layer = operator_mod.without_contributions(use_layer)
    plan = operator_mod.render_plan(bundle.docs, use_layer, use_ledger, legacy=legacy)
    providers = plan.docs["providers"]["providers"]
    lines = plan.docs["models-v2"]["models"]
    try:
        persisted = (continuity_mod.read(home) if continuity_override is None
                     else dict(continuity_override))
    except continuity_mod.ContinuityError:
        persisted = None
        working = continuity_mod.seed_only(bundle)
    else:
        working, _seeded = continuity_mod.merge_seed(
            persisted if persisted is not None else continuity_mod.empty(), bundle)
        managed = persisted.get("state_root") if persisted is not None else None
        if state_root is not None and (managed is None or managed == str(state_root)):
            scan = continuity_mod.scan_records(state_root)
            served_by_render = render_mod.catalog_alias_set(providers, lines) | frozenset(plan.captures)
            working, _extended, _unservable = continuity_mod.extend_from_records(
                working, bundle, scan, served_by_render)
    store = secret_store.default_store(env)
    assumed = frozenset(assume_present)
    withdrawn = frozenset(assume_absent)
    keyed_names = {name for provider in providers.values()
                   if (name := catalog_mod.keyed_secret_name(provider)) is not None}

    def present(name: str) -> str | None:
        if name in withdrawn:
            return None
        if name in assumed:
            return PLAN_PRESENT_VALUE
        try:
            if name in keyed_names:
                # A keyed route counts only a USABLE value (the same
                # predicate as the render); the value is checked transiently
                # and never enters the plan.
                return PLAN_PRESENT_VALUE if render_mod.usable_secret(store.get(name)) else None
            return PLAN_PRESENT_VALUE if store.is_set(name) else None
        except (secret_store.SecretStoreError, OSError):
            return None

    document, _available, _unavailable, _info = render_mod.build_config_document(
        plan.docs["gateway"], providers, lines,
        home=home, gateway_tokens=("0" * 64,), resolve_secret=present,
        continuity=working["aliases"], captures=plan.captures,
        provider_headers=plan.headers, oauth_overlay=plan.overlay,
        overlay_static=overlay_static_wires(env, root), quiet_providers=plan.quiet_providers,
        retired=(plan.docs.get("retired") or {}).get("retired"),
    )
    return document


def managed_state_root(home: Path) -> str | None:
    """The persisted root authority (None: none yet or unreadable).

    Display and render fallbacks only: a mutation guard uses
    :func:`root_authority` / :func:`root_authority_refusal`, which keep an
    unreadable authority distinct from an absent one."""

    return root_authority(home)[0]


def root_authority(home: Path) -> tuple[str | None, str | None]:
    """``(managed root, unreadable reason)``: ``(None, None)`` when no
    authority is recorded yet; ``(None, reason)`` when continuity.json exists
    but is unreadable, unsafe or malformed."""

    try:
        persisted = continuity_mod.read(home)
    except continuity_mod.ContinuityError as exc:
        return None, str(exc)
    return (None if persisted is None else persisted.get("state_root")), None


def root_authority_refusal(home: Path, state_root: Path | str) -> str | None:
    """The state-root boundary for served mutations, plan application and new
    managed references: another root, or an unreadable authority, refuses."""

    managed, unreadable = root_authority(home)
    return served_plan_mod.root_refusal(managed, str(state_root), unreadable=unreadable)


ModelsGetter = Callable[[str, str], tuple[int, set[str]]]
def restart_required() -> str:
    """The reload failure's text (a call: the remedy seam picks the command)."""

    return "gateway did not reload — restart required: " + service.hint("restart")


HELPER_TTL_SECONDS = 300.0
# The unchanged TTL wait reports its remaining time
# at this bounded cadence on the monotonic clock; nothing is persisted.
ROTATION_PROGRESS_SECONDS = 60.0
ROTATION_RESUMED = (
    "Resuming a published token rotation.",
    "The previous wait is not persisted, so the full {total}s safety window starts again.",
    "No key has been discarded.",
)
ROTATION_WAIT = (
    "step 3/4: Waiting {total}s for the helper cache window: {ttl}s TTL + {margin}s safety margin.",
    "Both gateway keys remain accepted during this wait.",
)
ROTATION_REMAINING = "  helper cache window: {remaining}s remaining; both keys accepted"
# Sessions whose launch may have put the gateway credential in their environment.
ROTATION_ENV_CREDENTIAL_PREFIX = "sessions that may still hold an environment credential (the previous token)"
ROTATION_WAIT_DONE = "  helper cache window complete: {total}s elapsed on the monotonic clock"
ROTATION_WAIT_CANCELLED = (
    "Wait interrupted: both gateway keys remain accepted and no key was discarded; rerun "
    "claude-multi doctor --rotate-token to resume (the full {total}s window starts again)."
)
# Hot reload (and a config-only init): the listener never went down, so the
# render sentinel alone proves the reload, within this hard-clamped deadline.
RELOAD_TIMEOUT = 2.0
# Readiness after a (re)start. The patched gateway
# holds model routes for at most 30 s after start until its initial auth load
# is registered (the sixth patch); this budget adds a start margin.
START_READINESS_TIMEOUT = 45.0
# One start-mode request may wait on that gate; hot reloads keep 0.25 s.
START_REQUEST_TIMEOUT = 5.0
READINESS_MODES = ("reload", "start")
START_CHECK_FAILED = 6  # init --start-check: not ready within the budget


@dataclass(frozen=True)
class ReloadResult:
    """Non-secret readiness outcome.

    Reload mode: reloaded/restart_required/token_mismatch/down. Start mode:
    ready/not_ready (the message names what is missing).
    """

    status: str
    message: str


def _start_missing(last: str, status: int | None, sentinel: str,
                   required: frozenset[str]) -> str:
    """Name the piece a (re)started gateway still lacked at the deadline."""

    if last == "refused":
        return (
            "the gateway still refused connections (is it running? why it stopped: "
            + service.hint("why") + ")"
        )
    if last == "failed":
        return "the models check kept failing (timeouts, resets or malformed listings)"
    if status == 401:
        return "the gateway rejects the current key (token mismatch)"
    if status != 200:
        return f"the models listing answered HTTP {status}"
    if last == "no-sentinel":
        return f"the render sentinel {sentinel} is not served (the running gateway has not loaded this render)"
    return (
        f"none of the {len(required)} rendered OAuth aliases is served (their pools are not "
        "registered; a pool without a sign-in serves none: claude-multi providers sign-in anthropic "
        "or claude-multi providers sign-in openai)"
    )


def instance_listener(home: Path, state_root: Path, environ: Mapping[str, str] | None,
                      gateway: Mapping[str, Any]) -> Callable[[str], service.OwnerVerdict]:
    """The listener verdict of the gateway ``state_root`` runs: its own
    proof (the instance record, lock and PID on demand; the unit's process
    when supervised), so a gateway this root started and proves reloads
    without an ownership Attention. Another port, or an observation that
    fails, gets the plain verdict."""

    def check(base_url: str) -> service.OwnerVerdict:
        from claude_multi import gateway_lifecycle

        try:
            own = gateway_lifecycle.Gateway(home=home, state_root=state_root,
                                            environ=dict(os.environ if environ is None else environ),
                                            gateway_document=gateway)
            if own.base_url == base_url:
                return own.observe().listener
        except (errors.ClaudeMultiError, OSError):
            pass
        return service.listener_owner(base_url)

    return check


def await_sentinel(
    gateway: dict[str, Any], token: str, sentinel: str, *,
    models_get: ModelsGetter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float | None = None,
    mode: str = "reload",
    oauth_aliases: Sequence[str] | frozenset[str] = (),
    owner_check: Callable[[str], service.OwnerVerdict] | None = None,
) -> ReloadResult:
    """Poll only the loopback registry, with a bounded deadline and test seams.

    ``mode="reload"`` (hot reload, config-only init): at most 2 s whatever
    ``timeout`` says; the sentinel alone is proof. A 401 may be the short
    interval before the new render applies; retry it until the deadline. Only
    connection refusal is informational (``down``); timeouts, resets and
    malformed responses retry within the deadline, then require a restart.

    ``mode="start"`` (after a (re)start): the budget is
    ``timeout`` (default ``START_READINESS_TIMEOUT``) with no clamp, and
    connection refusal is retried within it (the brief pre-listen window).
    Ready needs the sentinel AND at least one of ``oauth_aliases`` (the
    render's OAuth aliases, ``RenderResult.oauth_aliases``) when there are
    any. Expiry returns ``not_ready`` naming the missing piece. All exception
    details are suppressed (getters handle a credential).

    ``owner_check`` is the listener verdict before each authenticated poll
    (:func:`instance_listener` for a state root's own gateway); None asks the
    service manager only.
    """

    if mode not in READINESS_MODES:
        raise ValueError(f"unknown readiness mode: {mode!r}")
    start = mode == "start"
    required = frozenset(oauth_aliases) - {sentinel}

    # Recheck before every authenticated poll: a listener can change during
    # reload. Report each unconfirmed-owner warning only once per wait.
    notices: set[str] = set()

    def attention(message: str) -> None:
        if message not in notices:
            notices.add(message)
            print(f"claude-multi: Attention: {message}", file=sys.stderr)

    if start:
        budget = max(0.0, START_READINESS_TIMEOUT if timeout is None else timeout)
    else:
        budget = min(RELOAD_TIMEOUT, max(0.0, RELOAD_TIMEOUT if timeout is None else timeout))
    per_request = START_REQUEST_TIMEOUT if start else 0.25
    deadline = clock() + budget
    status = None
    failed = False
    last = "none"
    first = True
    while first or clock() < deadline:
        first = False
        try:
            if models_get is None:
                status, ids = launch_mod._default_models_get(
                    endpoint.gateway_endpoint(gateway).base_url, token,
                    timeout=max(0.001, min(per_request, deadline - clock())),
                    owner_check=owner_check, attention=attention,
                )
            else:
                status, ids = models_get(endpoint.gateway_endpoint(gateway).base_url, token)
            failed = False
            last = "answered"
        except ConnectionRefusedError:
            if not start:
                return ReloadResult("down", "gateway: not running; config will apply on start")
            status, ids, failed, last = None, set(), False, "refused"
        except AssertionError:
            raise  # do not hide test isolation trips in the retry loop
        except Exception:
            status, ids, failed, last = None, set(), True, "failed"
        if status == 200 and sentinel in ids:
            if not start:
                return ReloadResult("reloaded", f"gateway: reloaded (sentinel {sentinel})")
            served = sorted(required & ids)
            if served:
                return ReloadResult(
                    "ready", f"gateway: ready (sentinel {sentinel}; OAuth alias {served[0]} served)")
            if not required:
                return ReloadResult(
                    "ready", f"gateway: ready (sentinel {sentinel}; the render has no OAuth alias)")
            last = "no-oauth"
        elif status == 200:
            last = "no-sentinel"
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(0.1, remaining))
    if start:
        return ReloadResult(
            "not_ready",
            f"gateway: not ready {budget:g} s after a (re)start: "
            + _start_missing(last, status, sentinel, required),
        )
    if failed:
        return ReloadResult("restart_required", restart_required() + " (models check failed)")
    if status == 401:
        return ReloadResult("token_mismatch", restart_required() + " (token mismatch)")
    return ReloadResult("restart_required", restart_required())


class _FencedLock:
    """The api-key lock of one write phase, taken inside the writer fence of
    the home's roots (``gateway_inhibition.fenced``): each acquisition
    resolves the roots afresh (a root adopted since the last phase is fenced
    too), re-checks the inhibition, and re-reads the managed root under the
    api-key lock, so no phase writes once a transaction owner began. Raises
    like ``fenced``; released together."""

    def __init__(self, target: Path, home: Path, state_root: Path, token: str | None, what: str) -> None:
        self._lock = state.FileLock(target)
        self._home, self._root, self._token, self._what = home, state_root, token, what
        self._fence: contextlib.ExitStack | None = None

    def acquire(self, blocking: bool = True) -> bool:
        fence = contextlib.ExitStack()
        try:
            roots = gateway_inhibition.home_roots(self._home, self._root, what=self._what)
            fence.enter_context(gateway_inhibition.fenced(roots, token=self._token, what=self._what,
                                                          home=self._home))
            self._lock.acquire(blocking=True)
            fence.callback(self._lock.release)
            gateway_inhibition.revalidate(self._home, what=self._what)
        except BaseException:
            fence.close()
            raise
        self._fence = fence
        return True

    def release(self) -> None:
        if self._fence is not None:
            fence, self._fence = self._fence, None
            fence.close()  # the api-key lock first, then the fences


def rotate_token(
    home: Path, *,
    live_sessions: Callable[[], Sequence[str]],
    environ: dict[str, str] | None = None,
    resolver: Callable[[str], str | None] | None = None,
    models_get: ModelsGetter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    confirm: Callable[[str], bool] | None = None,
    margin: float = 5.0,
    progress: Callable[[str], None] | None = None,
    state_root: Path | None = None,
) -> ReloadResult:
    """Crash-reentrant hitless rotation; never returns or reports a token.

    ``live_sessions`` is REQUIRED: the CLI supplies the labels of the
    records that may still hold an environment credential (live or unknown). ``confirm(message)`` must explicitly acknowledge restart fallback
    or that the listed old sessions are dead. Confirmation never substitutes
    for positive HTTP verification. TTL is fixed at 300s, plus ``margin``;
    clock/sleep and the (base_url, token)->(status, ids) getter are injectable.

    The only persistent phase state is previous-key: equal to api-key means
    an unpublished attempt (safe to retry with a fresh candidate), unequal
    means published (resume the grace period). A resumed grace period waits
    a full TTL again, conservatively avoiding timestamp/clock-skew hazards.
    previous-key is removed ONLY after old=401/new=200 and sentinel proof.
    A separate nonblocking rotation lock excludes concurrent rotators; the
    api-key lock is never held during network, TTL waits or confirmations.
    ``progress(line)`` receives one non-secret line per step (CLI output).
    ``state_root`` threads into every render (continuity record extension).

    The wait is unchanged — never shortened, never inferred
    from file mtimes or persisted deadlines — and is disclosed: a resumed
    rotation says the full window starts again, the wait reports the
    remaining time every ``ROTATION_PROGRESS_SECONDS`` on ``clock`` (the
    monotonic clock by default) when a ``progress`` reporter is given, and
    once more when it ends (before any confirmation); an interrupt keeps the
    dual-key state recoverable.
    """

    if not isinstance(margin, (int, float)) or not 0 <= margin < float("inf"):
        raise ProxyError("rotation margin must be a finite non-negative number")
    endpoint.require_set_up(home, "nothing was rotated")
    own_root = state_root or sessions_mod.state_root(environ)
    owner_token = (os.environ if environ is None else environ).get(gateway_inhibition.TOKEN_ENV) or None
    for root in gateway_inhibition.home_roots(home, own_root, what="nothing was rotated"):
        gateway_inhibition.guard(root, token=owner_token, what="nothing was rotated")
    say = progress or (lambda _line: None)
    directory = config_dir(home)
    state.ensure_private_dir(directory)
    rotation_lock = state.FileLock(directory / "token-rotation")
    if not rotation_lock.acquire(blocking=False):
        raise ProxyError(ROTATION_IN_PROGRESS)
    # Every write phase re-checks the inhibition inside the writer fence of
    # the roots that own the home then (the waits between phases are long).
    lock = _FencedLock(directory / "api-key", home, own_root, owner_token,
                       "rotation paused; rerun doctor --rotate-token")
    previous_path = directory / "previous-key"
    token_path = directory / "api-key"

    def verify(
        result: render_mod.RenderResult, token: str, tokens: Sequence[str], *, published: bool,
    ) -> tuple[ReloadResult, render_mod.RenderResult]:
        outcome = await_sentinel(
            gateway, token, result.sentinel, models_get=models_get, clock=clock, sleep=sleep,
            owner_check=instance_listener(home, own_root, environ, gateway),
        )
        if outcome.status != "reloaded":
            message = restart_required() if outcome.status == "down" else outcome.message
            if confirm is None or not confirm(message + "; confirm after restarting to retry"):
                raise ProxyError(message + "; rotation paused; rerun doctor --rotate-token")
            # The unit's cmd_run re-renders from the published slots. Before
            # publication that is old-only; during retirement it is dual.
            # Reapply this phase after the acknowledged restart, then prove
            # it afresh. An unpatched watcher still refuses safely.
            lock.acquire(blocking=True)
            try:
                check_slots(old, new, published=published)
                _, result, _report = _render_locked(
                    home, tokens, resolver=resolver, environ=environ, state_root=state_root, policy="reload",
                )
            finally:
                lock.release()
            outcome = await_sentinel(
                gateway, token, result.sentinel, models_get=models_get, clock=clock, sleep=sleep,
                owner_check=instance_listener(home, own_root, environ, gateway),
            )
            if outcome.status != "reloaded":
                raise ProxyError("gateway reload still unverified; rotation paused (patched watcher required)")
        return outcome, result

    def check_slots(old: str, new: str, *, published: bool) -> None:
        expected = new if published else old
        if _read_token(token_path) != expected or _read_token(previous_path) != old:
            raise ProxyError("gateway key slots changed during rotation; retry")

    try:
        gateway = load_bundle(environ, home=home).docs["gateway"]
        lock.acquire(blocking=True)
        try:
            current = ensure_token(home)
            keys = gateway_api_keys(home)
            published = len(keys) == 2
            if published:
                old, new = keys
            else:
                old, new = current, secrets.token_hex(32)
                state.atomic_write(previous_path, (old + "\n").encode("ascii"))
            target, dual, _report = _render_locked(
                home, (old, new), resolver=resolver, environ=environ, state_root=state_root, policy="reload",
            )
        finally:
            lock.release()
        total = f"{HELPER_TTL_SECONDS + margin:.0f}"
        if published:
            for line in ROTATION_RESUMED:
                say(line.format(total=total))
        say(
            "step 1/4: dual-key config rendered (resuming the published "
            "rotation); verifying the gateway reload"
            if published else
            "step 1/4: dual-key config rendered (current + new candidate); "
            "verifying the gateway reload"
        )
        _, dual = verify(dual, new, (old, new), published=published)
        # Publication requires the sentinel AND HTTP 200 with the candidate.
        lock.acquire(blocking=True)
        try:
            check_slots(old, new, published=published)
            if state.read_private(target) != dual.yaml.encode("utf-8"):
                raise ProxyError("gateway config changed during rotation; retry")
            if not published:
                state.atomic_write(token_path, (new + "\n").encode("ascii"))
        finally:
            lock.release()
        say("step 2/4: candidate verified (sentinel + HTTP 200); the token helper now serves it")

        for line in ROTATION_WAIT:
            say(line.format(total=total, ttl=f"{HELPER_TTL_SECONDS:.0f}", margin=f"{margin:g}"))
        deadline = clock() + HELPER_TTL_SECONDS + margin
        # Without a progress reporter (a library caller) nothing is reported,
        # so the wait is one sleep to the deadline, as before.
        step = ROTATION_PROGRESS_SECONDS if progress is not None else float("inf")
        try:
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                sleep(min(step, remaining))
                remaining = deadline - clock()
                if remaining > 0 and progress is not None:
                    say(ROTATION_REMAINING.format(remaining=math.ceil(remaining)))
        except KeyboardInterrupt:
            say(ROTATION_WAIT_CANCELLED.format(total=total))
            raise
        say(ROTATION_WAIT_DONE.format(total=total))
        live = tuple(live_sessions())
        if live:
            message = (
                ROTATION_ENV_CREDENTIAL_PREFIX + ": " + ", ".join(live)
                + "; relaunch them, or explicitly confirm they are dead"
            )
            if confirm is None or not confirm(message):
                raise ProxyError(message + "; rotation paused")
        say("step 4/4: retiring the previous key; verifying the reload, then old=401 / new=200")
        lock.acquire(blocking=True)
        try:
            check_slots(old, new, published=True)
            target, single, _report = _render_locked(
                home, (new,), resolver=resolver, environ=environ, state_root=state_root, policy="reload",
            )
        finally:
            lock.release()
        outcome, single = verify(single, new, (new,), published=True)
        try:
            old_status, _ = (models_get or launch_mod._default_models_get)(
                endpoint.gateway_endpoint(gateway).base_url, old,
            )
        except Exception:
            raise ProxyError("previous token rejection unverified; rotation paused") from None
        if old_status != 401:
            raise ProxyError("previous token still accepted or rejection unverified; rotation paused")
        lock.acquire(blocking=True)
        try:
            check_slots(old, new, published=True)
            if state.read_private(target) != single.yaml.encode("utf-8"):
                raise ProxyError("gateway config changed during rotation; retry")
            state.remove_private(previous_path)
        finally:
            lock.release()
        return outcome
    finally:
        rotation_lock.release()


@dataclass(frozen=True)
class PruneOutcome:
    """``doctor --prune-aliases`` result: exit code and non-secret lines."""

    code: int
    lines: tuple[str, ...]


# The token-rotation lock is held by a rotation, an alias
# prune or a served operator change; the refusal names none of them.
ROTATION_IN_PROGRESS = (
    "another gateway operation is in progress (a token rotation, an alias prune or a served "
    "change); rerun when it completes"
)


@dataclass(frozen=True)
class AliasPrunePlan:
    """What one alias prune removes and the inputs it was derived from."""

    document: dict[str, Any] | None
    snapshot: operator_mod.OperatorSnapshot
    scan: continuity_mod.RecordScan
    remove_continuity: tuple[str, ...]
    remove_captures: tuple[str, ...]
    lines: tuple[str, ...]
    catalog_version: int


def _plan_alias_prune(
    home: Path,
    state_root: Path,
    requested: Sequence[str] | None,
    *,
    environ: dict[str, str] | None,
    env: dict[str, str],
    is_process_live: Callable[[Mapping[str, Any]], bool] | None,
    pinned: frozenset[str] | None,
) -> "PruneOutcome | AliasPrunePlan":
    """Read-only: the prune of both stores (continuity.json and the ledger's
    captures) under one combined live-reference check. ``pinned`` is the
    set of ended holders whose lifecycle locks the caller holds (None: the
    lock-free preview, which never claims them pinned)."""

    lines: list[str] = []
    try:
        document = continuity_mod.read(home)
    except continuity_mod.ContinuityError as exc:
        return PruneOutcome(1, (
            f"gateway continuity set unreadable ({exc}); nothing pruned — move "
            "~/.config/claude-multi/continuity.json aside and run "
            "claude-multi-proxy init",
        ))
    bundle = load_bundle(environ, home=home)
    legacy = custom_mod.load_registry(env)
    snapshot = operator_snapshot(env, bundle.docs, legacy)
    if snapshot.ledger_error is not None:
        return PruneOutcome(1, (f"{snapshot.ledger_error}; nothing pruned",))
    captures = dict(snapshot.ledger.aliases) if snapshot.ledger is not None else {}
    if document is None and not captures:
        return PruneOutcome(0, ("nothing to prune",))
    scan = continuity_mod.scan_records(state_root)
    if scan.directory_error is not None:
        return PruneOutcome(1, (
            "cannot prove liveness: sessions directory unreadable "
            f"({scan.directory_error}); nothing pruned",
        ))
    if scan.unreadable:
        ids = ", ".join(stem[:8] for stem in scan.unreadable)
        return PruneOutcome(1, (
            f"cannot prove liveness: unreadable records {ids}; nothing pruned",
        ))
    if scan.fence_unreadable:
        ids = ", ".join(stem[:8] for stem in scan.fence_unreadable)
        return PruneOutcome(1, (
            f"cannot prove liveness: unreadable scope fences {ids} (unknown references); "
            "nothing pruned",
        ))
    ended_now = {stem for ids in scan.refs.values() for stem in ids} - set(scan.live)
    if pinned is not None and not ended_now <= set(pinned):
        return PruneOutcome(1, (
            "session records changed during the prune scan; nothing pruned — rerun",))
    if is_process_live is not None and ended_now:
        still_running = set()
        for stem in sorted(ended_now):
            # The same lenient, lock-free read as the scan (in the
            # commit phase its lifecycle lock is held): only the
            # identity fields the process check names are used.
            try:
                raw = strict_json.loads(state.read_private(
                    Path(state_root) / "sessions" / f"{stem}.json"))
            except Exception:
                raw = None
            if not isinstance(raw, dict):
                return PruneOutcome(1, (
                    f"cannot prove liveness: unreadable records {stem[:8]}; nothing pruned",))
            if is_process_live(raw):
                still_running.add(stem)
        if still_running:
            scan = continuity_mod.RecordScan(
                refs=scan.refs, live=frozenset(set(scan.live) | still_running),
                unreadable=scan.unreadable, notices=scan.notices,
                directory_error=scan.directory_error,
                fence_unreadable=scan.fence_unreadable, slots=scan.slots,
            )
    continuity_aliases = set(document["aliases"]) if document is not None else set()
    if requested:
        names = {name[:-4] if name.endswith("[1m]") else name for name in requested}
        unknown = sorted(names - continuity_aliases - set(captures))
        if unknown:
            store = "continuity or captured" if captures else "continuity"
            return PruneOutcome(1, (
                f"refused: unknown {store} alias(es): {', '.join(unknown)}; nothing pruned",))
        wanted_continuity = frozenset(names & continuity_aliases)
        wanted_captures = frozenset(names & set(captures))
    else:
        names = None
        wanted_continuity = wanted_captures = frozenset()
    # The render's own collision set (custom merge, the rendered
    # operator lines and retained captures included): an
    # alias in it is served ahead of continuity.
    plan = operator_mod.render_plan(bundle.docs, snapshot.layer, snapshot.ledger, legacy=legacy)
    merged = plan.docs
    served = render_mod.catalog_alias_set(
        merged["providers"]["providers"], merged["models-v2"]["models"],
    ) | frozenset(plan.captures)
    continuity_plan = None
    if document is not None and (names is None or wanted_continuity):
        continuity_plan = continuity_mod.plan_prune(
            document, bundle, scan, wanted_continuity or None, served_by_catalog=served,
        )
    current = {base for line in snapshot.layer.lines.values()
               for base in operator_mod.line_aliases(line.core_entry)}
    capture_plan = None
    if captures and (names is None or wanted_captures):
        capture_plan = operator_mod.plan_capture_prune(
            snapshot.ledger, current, scan.refs, scan.live, wanted_captures or None,
        )
    refusals = [p.refusal for p in (continuity_plan, capture_plan) if p is not None and p.refusal]
    if refusals:
        holders = {h for p in (continuity_plan, capture_plan) if p is not None for h in p.holders}
        remedy = f"; {sessions_mod.mark_ended_remedy(holders)}" if holders else ""
        return PruneOutcome(1, (f"refused: {'; '.join(refusals)}; nothing pruned{remedy}",))
    remove_continuity = continuity_plan.remove if continuity_plan is not None else ()
    remove_captures = capture_plan.remove if capture_plan is not None else ()
    keeps = [*(continuity_plan.keep if continuity_plan is not None else ()),
             *(capture_plan.keep if capture_plan is not None else ())]
    if not remove_continuity and not remove_captures:
        for alias, reason in keeps:
            lines.append(f"kept: {alias} — {reason}")
        return PruneOutcome(0, tuple(lines or ["nothing to prune"]))
    catalog_version = int(bundle.docs["version"]["catalog_version"])
    for alias in remove_continuity:
        entry = document["aliases"][alias]
        note = f" — {continuity_mod.CATALOG_SERVED}" if alias in served else ""
        lines.append(f"pruned: {alias} ({entry['provider']} {entry['wire']}){note}")
    for alias in remove_captures:
        entry = captures[alias]
        lines.append(f"pruned: {alias} ({entry['provider']} {entry['wire']}; captured for "
                     f"{entry['source'].removeprefix('operator:')})")
    for alias, reason in keeps:
        lines.append(f"kept: {alias} — {reason}")
    return AliasPrunePlan(
        document=document, snapshot=snapshot, scan=scan,
        remove_continuity=tuple(remove_continuity), remove_captures=tuple(remove_captures),
        lines=tuple(lines), catalog_version=catalog_version,
    )


@dataclass(frozen=True)
class PrunePreview:
    """The lock-free alias prune preview: an early outcome (nothing to prune
    or a refusal), or the prune plan and the served-change plan of the
    complete post-prune render."""

    outcome: PruneOutcome | None
    prune: AliasPrunePlan | None = None
    served: served_plan_mod.ServedChangePlan | None = None

    def unrelated(self) -> bool:
        """The post-prune render also publishes something that is not this
        prune's own removal (a pending declaration change, or an unknown
        published render)."""

        if self.served is None or self.prune is None:
            return False
        own = set(self.prune.remove_continuity) | set(self.prune.remove_captures)
        if self.served.before_unknown is not None or self.served.added or self.served.retargeted:
            return True
        return any(served_plan_mod._base(selector) not in own for selector, _route in self.served.removed)


def prune_preview(
    home: Path,
    state_root: Path,
    requested: Sequence[str] | None,
    *,
    environ: dict[str, str] | None = None,
    is_process_live: Callable[[Mapping[str, Any]], bool] | None = None,
    process_known: bool = True,
) -> PrunePreview:
    """``doctor --prune-aliases``' shared served-change preflight: no
    lock, no write. The plan compares the published
    render with the render after the prune — the proposed continuity and
    capture removals plus every pending declaration change."""

    if not process_known:
        return PrunePreview(PruneOutcome(1, ("cannot prove liveness: the process table is unreadable; "
                                             "nothing pruned",)))
    refusal = root_authority_refusal(home, state_root)
    if refusal is not None:
        return PrunePreview(PruneOutcome(1, tuple(refusal.splitlines())))
    env = dict(os.environ if environ is None else environ)
    planned = _plan_alias_prune(home, state_root, requested, environ=environ, env=env,
                                is_process_live=is_process_live, pinned=None)
    if isinstance(planned, PruneOutcome):
        return PrunePreview(planned)
    if not planned.remove_continuity and not planned.remove_captures:
        return PrunePreview(PruneOutcome(0, planned.lines or ("nothing to prune",)))
    continuity_after = planned.document
    if planned.remove_continuity and planned.document is not None:
        continuity_after = continuity_mod.apply_prune(
            planned.document, continuity_mod.PrunePlan(planned.remove_continuity, ()), planned.catalog_version)
    ledger_after = None
    if planned.remove_captures:
        schemas = planned.snapshot.schemas or operator_mod.load_schemas(assets_root(environ))
        ledger_after = operator_mod.parse_ledger(strict_json.canonical_file_bytes(operator_mod.apply_capture_prune(
            operator_mod.ledger_document(planned.snapshot.ledger), planned.remove_captures, planned.catalog_version,
        )), schemas.ledger)
    published = published_identity(home)
    candidate = candidate_document(
        home, environ=environ, ledger=ledger_after, replace_ledger=ledger_after is not None,
        state_root=state_root, continuity_override=continuity_after,
    )
    served = served_plan_mod.build_plan(
        published.routes, served_plan_mod.routes_from_document(candidate),
        references=served_plan_mod.references_from_scan(planned.scan), state_root=str(state_root),
        gateway=served_plan_mod.gateway_label(candidate), before_unknown=published.unknown,
    )
    return PrunePreview(None, planned, served)


def prune_aliases(
    home: Path,
    state_root: Path,
    requested: Sequence[str] | None,
    *,
    environ: dict[str, str] | None = None,
    resolver: Callable[[str], str | None] | None = None,
    models_get: ModelsGetter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    barrier: state.BarrierToken | None = None,
    is_process_live: Callable[[Mapping[str, Any]], bool] | None = None,
    process_known: bool = True,
    revalidate: Callable[["AliasPrunePlan"], str | None] | None = None,
) -> PruneOutcome:
    """Remove continuity aliases no live session references.

    The caller's outer phase holds the migration guard
    and the served-change barrier (``barrier`` is asserted, never taken).
    The managed root must be ``state_root``. An unreadable compiled fence
    or an unknown process table is unknown coverage and refuses. A record
    counts as not live only when it is ended, no process names its runtime,
    and the re-read under the runtime-index then lifecycle locks (held
    through the commit) still sees it ended.

    Lock order is ``rotate_token``'s: the token-rotation lock first
    (non-blocking — a rotation in progress refuses, since a render inside
    its window would abort the rotation or drop its candidate key), then
    the api-key lock (blocking) around read, plan, write and re-render.
    Unreadable records refuse: liveness cannot be proven. Records are never
    modified. The reload is verified after the api-key lock is released.

    ``revalidate(plan)`` runs under every lock with the
    re-derived plan; a non-None text refuses with nothing pruned — the
    caller's served-change preview (:func:`prune_preview`) is bound to it.
    """

    if barrier is not None:
        state.require_barrier(barrier, root=state_root)
    if not process_known:
        return PruneOutcome(1, ("cannot prove liveness: the process table is unreadable; nothing pruned",))
    if not endpoint.set_up(home):
        return PruneOutcome(1, (f"{endpoint.NOT_SET_UP}; nothing pruned",))
    inhibition = gateway_inhibition.blocking(
        state_root, token=(os.environ if environ is None else environ).get(gateway_inhibition.TOKEN_ENV) or None)
    if inhibition is not None:
        message, remedy = gateway_inhibition.refusal(inhibition, "nothing pruned")
        return PruneOutcome(1, (message, f"  fix: {remedy}"))
    refusal = root_authority_refusal(home, state_root)
    if refusal is not None:
        return PruneOutcome(1, tuple(refusal.splitlines()))
    directory = config_dir(home)
    state.ensure_private_dir(directory)
    rotation_lock = state.FileLock(directory / "token-rotation")
    if not rotation_lock.acquire(blocking=False):
        return PruneOutcome(1, (ROTATION_IN_PROGRESS,))
    pinned_locks: list[state.FileLock] = []
    try:
        env = dict(os.environ if environ is None else environ)
        lines: list[str] = []
        # Both stores (continuity.json and the operator ledger's
        # captures) under one combined live-reference check. Lock order:
        # token rotation -> operator store lock -> api-key leaf.
        with operator_mod.store_lock(env):
            # Pin every ended record that names a selector (runtime-index,
            # then each lifecycle lock in sorted order, held through the
            # commit), so no hook can turn it live between the final scan
            # and the write.
            pre = continuity_mod.scan_records(state_root)
            pinned = sorted({stem for ids in pre.refs.values() for stem in ids} - set(pre.live))
            if pinned:
                store = sessions_mod.SessionStore(state_root, sessions_mod.default_schema())
                index_lock = store.runtime_index_lock()
                index_lock.acquire(blocking=True)
                pinned_locks.append(index_lock)
                for stem in pinned:
                    lifecycle = store.lifecycle_lock(stem)
                    lifecycle.acquire(blocking=True)
                    pinned_locks.append(lifecycle)
            # The write phase re-checks the inhibition inside the writer fence.
            lock = _FencedLock(directory / "api-key", home, state_root,
                               (os.environ if environ is None else environ).get(gateway_inhibition.TOKEN_ENV) or None,
                               "nothing pruned")
            try:
                lock.acquire(blocking=True)
            except (gateway_inhibition.Inhibited, gateway_inhibition.InhibitionError) as exc:
                return PruneOutcome(1, (str(exc), f"  fix: {exc.remedy}"))
            try:
                planned = _plan_alias_prune(
                    home, state_root, requested, environ=environ, env=env,
                    is_process_live=is_process_live, pinned=frozenset(pinned),
                )
                if isinstance(planned, PruneOutcome):
                    return planned
                if revalidate is not None:
                    changed = revalidate(planned)
                    if changed is not None:
                        return PruneOutcome(1, (changed,))
                lines.extend(planned.lines)
                document, snapshot = planned.document, planned.snapshot
                remove_continuity, remove_captures = planned.remove_continuity, planned.remove_captures
                catalog_version = planned.catalog_version
                if remove_captures:
                    schemas = snapshot.schemas or operator_mod.load_schemas(assets_root(environ))
                    operator_mod.write_ledger(env, operator_mod.apply_capture_prune(
                        operator_mod.ledger_document(snapshot.ledger), remove_captures, catalog_version,
                    ), schemas)
                if remove_continuity:
                    updated = continuity_mod.apply_prune(
                        document, continuity_mod.PrunePlan(tuple(remove_continuity), ()), catalog_version,
                    )
                    continuity_mod.write(home, updated)
                ensure_token(home)
                _target, result, _report = _render_locked(
                    home, gateway_api_keys(home), resolver=resolver, environ=environ,
                    state_root=state_root, policy="reload", barrier=barrier,
                )
            finally:
                lock.release()
                for held in reversed(pinned_locks):
                    held.release()
                pinned_locks.clear()
        gateway = load_bundle(environ, home=home).docs["gateway"]
        outcome = await_sentinel(
            gateway, gateway_api_keys(home)[-1], result.sentinel,
            models_get=models_get, clock=clock, sleep=sleep,
            owner_check=instance_listener(home, state_root, environ, gateway),
        )
        lines.append(outcome.message)
        return PruneOutcome(0, tuple(lines))
    finally:
        for held in reversed(pinned_locks):
            held.release()
        rotation_lock.release()


# Environment the pinned gateway reads at
# startup that would silently change what it is. MANAGEMENT_PASSWORD alone
# enables every management route (internal/api/server.go); HOME_JWT and the
# PGSTORE_/GITSTORE_/OBJECTSTORE_ families move the config/credential store
# off the rendered file (cmd/server/main.go, which also reads the lowercase
# spellings); DEPLOY=cloud switches the startup mode; WRITABLE_PATH moves
# the writable base; MANAGEMENT_STATIC_PATH serves a control panel;
# GITHUB_TOKEN and META_MINT_URL are credential/endpoint overrides. None of
# them has a place in the loopback, file-store, served == rendered gateway,
# so run/login never pass them. Matched case-insensitively, so every
# lowercase (and mixed-case) spelling is covered.
# Owned by secret_store (the shared credential policy); these are
# the same objects, re-exported by reference.
GATEWAY_ENV_DENY_NAMES = secret_store.GATEWAY_ENV_DENY_NAMES
GATEWAY_ENV_DENY_PREFIXES = secret_store.GATEWAY_ENV_DENY_PREFIXES


EXEC_ARGV_FLAGS = ("--local-model",)
PREPARED_STAMP = "prepared.json"


def exec_signature() -> str:
    return strict_json.sha256_hex(strict_json.canonical_bytes({
        "argv_flags": list(EXEC_ARGV_FLAGS),
        "deny_names": sorted(GATEWAY_ENV_DENY_NAMES),
        "deny_prefixes": list(GATEWAY_ENV_DENY_PREFIXES),
        "workdir": service.GATEWAY_WORKDIR, "v": 1,
        "management_injection": management.EXEC_POLICY,
    }))[:16]


def _prepared_inputs(home: Path, state_root: Path, environ: dict[str, str] | None) -> dict:
    """Receipt inputs available inside the read-only Go view (no secret file).

    ExecStartPre freshly resolves secrets outside the sandbox. The receipt
    binds that exact render to this release, renderer, catalog, key slots and
    operator inputs. Run does not initialize, lock, render or scan records.
    """
    bundle = catalog_mod.load_catalog(assets_root(environ))
    env = dict(os.environ if environ is None else environ)
    files = {}
    for name in ("config.yaml", "api-key", "previous-key", "continuity.json", endpoint.ENDPOINT_FILE):
        path = config_dir(home) / name
        files[name] = strict_json.sha256_hex(state.read_private(path)) if os.path.lexists(path) else None
    return {
        "version": 1, "launcher_version": __version__, "state_root": str(state_root),
        "catalog": bundle.bundle_sha256,
        "renderer": strict_json.sha256_hex(Path(render_mod.__file__).read_bytes()),
        "custom": strict_json.bundle_digest(custom_mod.load_registry(env)), "files": files,
        # Raw, non-raising digests of providers.d and the ledger.
        "operator": operator_mod.prepared_fingerprint(env),
    }


def _prepared_config(home: Path, state_root: Path, environ: dict[str, str] | None) -> Path:
    try:
        receipt = strict_json.loads(state.read_private(config_dir(home) / PREPARED_STAMP))
        current = _prepared_inputs(home, state_root, environ)
        if receipt != current or current["files"]["config.yaml"] is None:
            raise ValueError("stale preparation")
        gateway_api_keys(home)  # validate key slots without ever creating them
    except (OSError, ValueError, errors.ClaudeMultiError) as exc:
        raise ProxyError(
            "gateway preparation is absent or stale; run claude-multi-proxy init before run --prepared"
        ) from exc
    return config_dir(home) / "config.yaml"


def gateway_env_denied(name: str) -> bool:
    """Whether an inherited variable is withheld from the gateway."""

    return secret_store.gateway_env_denied(name)


def gateway_environment(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The scrubbed environment the gateway is exec'd with, plus the NAMES
    removed (sorted; values are never returned or printed)."""

    source = os.environ if environ is None else environ
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in source.items():
        if gateway_env_denied(name):
            dropped.append(name)
        else:
            kept[name] = value
    return kept, sorted(dropped)


def _gateway_exec_env(environ: dict[str, str] | None) -> dict[str, str]:
    env, dropped = gateway_environment(environ)
    if dropped:
        print(
            f"gateway env: not passed to the gateway: {', '.join(dropped)}",
            file=sys.stderr,
        )
    # The management attestation describes this launcher's wrapper; the
    # gateway never reads it, so it never carries one. Nor does it carry the
    # inhibition token a starting owner handed to this process.
    env.pop(management.PATCHES_ENV, None)
    env.pop(gateway_inhibition.TOKEN_ENV, None)
    return env


def resolve_proxy_binary(environ: dict[str, str] | None = None) -> Path:
    """Pinned proxy binary: explicit env override, else PATH lookup."""

    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_MULTI_PROXY_BIN")
    candidate = Path(configured) if configured else None
    if candidate is None:
        found = shutil.which("cli-proxy-api")
        candidate = Path(found) if found else None
    if candidate is None:
        raise ProxyError("CLIProxyAPI binary not found (CLAUDE_MULTI_PROXY_BIN/PATH)")
    resolved = Path(os.path.realpath(candidate))
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProxyError(f"CLIProxyAPI binary {resolved} is not an executable file")
    return resolved


def _summarize(
    result: render_mod.RenderResult, target: Path, report: ContinuityReport | None = None,
) -> list[str]:
    digest = strict_json.sha256_hex(result.yaml.encode("utf-8"))[:16]
    lines = [
        f"config: {target} (sha256:{digest})",
        f"providers available: {', '.join(result.available_providers) or 'none'}",
    ]
    for item in result.unavailable:
        lines.append(
            f"provider unavailable: {item['provider']} ({item['reason']})"
        )
    if report is not None:
        lines.append(
            f"continuity: {report.size} aliases ({len(report.rendered)} rendered)"
        )
        if report.unservable:
            lines.append(f"unservable selectors: {', '.join(report.unservable)}")
        if report.skipped_records:
            lines.append(f"records skipped: {len(report.skipped_records)}")
    return lines


def _continuity_problem_lines(report: ContinuityReport) -> list[str]:
    """Non-fatal continuity lines for stderr (never fail run/login)."""

    lines = [f"continuity: {notice}" for notice in report.notices]
    lines.extend(f"operator: {note}" for note in report.operator)
    if report.corrupt is not None:
        lines.append(
            f"gateway continuity set unreadable ({report.corrupt}): serving the "
            f"seed set; move {config_dir(Path('~'))}/continuity.json aside and "
            "run claude-multi-proxy init"
        )
    return lines


STATE_ROOT_USAGE = "usage: claude-multi-proxy {command} [--state-root /abs/path]"


def _parse_state_root(
    command: str, args: list[str], environ: dict[str, str] | None,
) -> Path:
    """``--state-root PATH`` (absolute) is the only accepted flag.

    Absent, the session state root follows the PASSED environ
    (``XDG_STATE_HOME`` or ``$HOME/.local/state``), like the launcher's.
    """

    usage = STATE_ROOT_USAGE.format(command=command)
    if not args:
        derived = sessions_mod.state_root(environ)
        if not derived.is_absolute():
            # A relative XDG_STATE_HOME (or HOME) would only fail later on
            # the continuity schema's absolute `state_root`: the same usage
            # error as an explicit relative flag.
            raise ProxyError(
                f"{usage} (the default state root {derived} derived from "
                "XDG_STATE_HOME/HOME is not absolute)"
            )
        return derived
    if len(args) == 2 and args[0] == "--state-root":
        value = args[1]
    elif len(args) == 1 and args[0].startswith("--state-root="):
        value = args[0].split("=", 1)[1]
    else:
        raise ProxyError(usage)
    if not value or not os.path.isabs(value):
        raise ProxyError(usage)
    path = Path(value)
    if path.parent == path:
        # "/" (or "//") is never a claude-multi state root, and the
        # continuity schema would refuse to persist it anyway.
        raise ProxyError(usage)
    return path



RUN_MODES = ("--prepared", "--prepare-and-exec")
RUN_DETACH = "--detach"
RUN_INSTANCE = "--instance"
RUN_USAGE = ("usage: claude-multi-proxy run [--prepared | --prepare-and-exec [--detach]] "
             "[--instance NONCE] [--state-root /abs/path]")
# Exit status of run when another instance holds the single-instance lock.
INSTANCE_BUSY_EXIT = 75
# Exit status of any command interrupted by Ctrl-C (the launcher's too).
CANCELLED_EXIT = 130
INSTANCE_LOCK_WAIT = 1.0


class InstanceBusyError(ProxyError):
    """Another gateway instance holds the single-instance lock."""


def _parse_run_args(args: list[str], environ: dict[str, str] | None,
                    ) -> tuple[Path, str | None, str | None, bool]:
    """``(state root, mode, instance nonce, detach)`` for ``run``, flags in any order."""

    rest: list[str] = []
    instance: str | None = None
    instances = 0
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == RUN_INSTANCE:
            instances += 1
            instance = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if arg.startswith(RUN_INSTANCE + "="):
            instances += 1
            instance = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        index += 1
    modes = [arg for arg in rest if arg in RUN_MODES]
    detach = RUN_DETACH in rest
    if (len(modes) > 1 or rest.count(RUN_DETACH) > 1 or instances > 1
            or (detach and modes != ["--prepare-and-exec"])
            or (instance is not None and (not modes or not service.INSTANCE_NONCE.fullmatch(instance)))):
        raise ProxyError(RUN_USAGE)
    remaining = [arg for arg in rest if arg not in RUN_MODES and arg != RUN_DETACH]
    try:
        root = _parse_state_root("run", remaining, environ)
    except ProxyError as exc:
        if not modes:
            raise  # preserve established state-root diagnostics on the legacy path
        raise ProxyError(RUN_USAGE) from exc
    return root, (modes[0] if modes else None), instance, detach


INIT_MODE_FLAGS = ("--reload-check", "--start-check", "--prepare-start")


ADOPT_ROOT_FLAG = "--adopt-root"
INIT_USAGE = ("usage: claude-multi-proxy init [--reload-check | --start-check | --prepare-start] "
              "[--state-root /abs/path] | init --state-root /abs/path --adopt-root")


def _parse_init_args(args: list[str], environ: dict[str, str] | None) -> tuple[Path, str | None]:
    """``[--reload-check | --start-check | --prepare-start] [--state-root /abs/path]``, any order.

    ``--state-root R --adopt-root`` is the explicit root
    adoption (an explicit ``--state-root`` is required, no mode flag).
    """

    usage = INIT_USAGE
    if ADOPT_ROOT_FLAG in args:
        rest = [arg for arg in args if arg != ADOPT_ROOT_FLAG]
        if args.count(ADOPT_ROOT_FLAG) > 1 or any(arg in INIT_MODE_FLAGS for arg in rest) or not rest:
            raise ProxyError(usage)
        try:
            return _parse_state_root("init", rest, environ), ADOPT_ROOT_FLAG
        except ProxyError as exc:
            raise ProxyError(usage) from exc
    flags = [arg for arg in args if arg in INIT_MODE_FLAGS]
    if len(flags) > 1:
        raise ProxyError(usage)
    rest = [arg for arg in args if arg not in INIT_MODE_FLAGS]
    try:
        root = _parse_state_root("init", rest, environ)
    except ProxyError as exc:
        if not flags:
            raise  # preserve established state-root diagnostics on the legacy path
        raise ProxyError(usage) from exc
    return root, (flags[0] if flags else None)


RELOAD_CHECK_EXIT = {"reloaded": 0, "restart_required": 3, "token_mismatch": 4, "down": 5}


def _management_status(info: management.KeyState) -> str:
    if info.disabled:
        return "disabled"
    if info.active == "unusable":
        return f"key unusable ({info.unusable_reason})"
    if info.pending:
        return "key pending; quota reads off until a prepared gateway start"
    if info.active == "present":
        return "key active, rotation staged" if info.staged else "key active"
    return "off (no key)"


def _management_error(exc: management.ManagementKeyError) -> None:
    print(f"management key: {exc}; fix: {exc.remedy}", file=sys.stderr)


def _management_notes(notes: tuple[str, ...]) -> None:
    for note in notes:
        if note == "promoted":
            print("gateway management: staged management key promoted", file=sys.stderr)
        elif note == "staged-unusable":
            print("gateway management: management-key.next unusable; kept the current key", file=sys.stderr)
        elif note == "start-unconfirmed":
            print("gateway management: stopped listener and PID not confirmed; quota stays off; "
                  "retry init --prepare-start at the next stopped gateway start", file=sys.stderr)
        elif note.startswith("unusable:"):
            print(f"gateway management: management-key unusable ({note.split(':', 1)[1]}); "
                  "MANAGEMENT_PASSWORD not passed", file=sys.stderr)


def _init_management(home: Path, environ: dict[str, str] | None, *, prepare_start: bool,
                     gateway: dict, state_root: Path, listener_observer: Callable | None,
                     pid_get: Callable | None, instance_locked: bool = True) -> None:
    """Optional key trouble cannot change init's readiness result or stamps."""
    if not management.allowlist_build(os.environ if environ is None else environ):
        if management.key_state(home).active != "absent":
            print(f"management key: not used ({management.UNAVAILABLE})")
        return
    try:
        if prepare_start:
            # No connect, no credentials, and no service-manager mutation.
            # The stopped proof is the instance lock (this process holds it,
            # so no instance that honours it runs or can start) together with
            # an absent listener; the recorded process (and the manager's
            # MainPID hint) must be gone as well. MainPID=0 alone never
            # proves anything. Inaccessible /proc or a missing manager is not
            # stopped: the backend's observation is unknown, which is never a
            # stopped proof. The proof is revalidated under the key lock, at
            # the promotion boundary: a replacement since the first sample
            # withdraws it.
            def observe():
                return service.gateway_observation(
                    endpoint.gateway_endpoint(gateway).base_url, state_root=state_root,
                    listener=listener_observer, pid_get=pid_get)
            first = observe()
            _management_notes(management.prepare_start(
                home, stopped=lambda: instance_locked and service.confirm_stopped(first, observe)))
            return
        result = management.ensure(home)
        info = management.key_state(home)
        restart = service.hint('restart')
        if result == "disabled":
            print("management key: disabled (claude-multi-proxy rotate-management-key re-enables)")
        elif info.pending and info.staged:
            print("management key: new key staged in management-key.next; quota reads stay off "
                  f"until the gateway next starts: {restart}")
        elif info.pending:
            print("management key: present; quota reads stay off until a prepared "
                  f"gateway start: {restart}")
        elif info.staged:
            print("management key: present; a rotated key is staged and takes effect "
                  f"when the gateway next starts: {restart}")
        else:
            print("management key: present")
    except management.ManagementKeyError as exc:
        _management_error(exc)


def cmd_rotate_management_key(args: list[str], *, environ: dict[str, str] | None = None) -> int:
    if args:
        raise ProxyError("usage: claude-multi-proxy rotate-management-key")
    if not management.allowlist_build(os.environ if environ is None else environ):
        raise ProxyError(f"{management.UNAVAILABLE}; nothing written")
    result = management.stage_rotation(_home(environ))
    if result == "staged":
        print("management key: new key staged in management-key.next; the running gateway "
              "keeps the current key until it restarts")
    else:
        print("management key: new key staged in management-key.next; quota reads stay off "
              "until the gateway next starts")
    print(f"apply: {service.hint('restart')}")
    return 0


def cmd_disable_management_key(args: list[str], *, environ: dict[str, str] | None = None) -> int:
    if args:
        raise ProxyError("usage: claude-multi-proxy disable-management-key")
    management.disable(_home(environ))
    print("management key: removed and disabled; quota reads are off "
          "(doctor falls back to the gateway journal)")
    print(f"the running gateway keeps its key until it restarts: {service.hint('restart')}")
    print("re-enable: claude-multi-proxy rotate-management-key")
    return 0


def pending_served_summary(
    home: Path, *, environ: Mapping[str, str] | None = None, state_root: Path | None = None,
) -> list[str]:
    """The plan of the declared render against the published one, as lines
    (empty when nothing served changes). Never raises, never prompts."""

    try:
        published = published_identity(home)
        document = candidate_document(home, environ=environ, state_root=state_root)
        scan = continuity_mod.scan_records(state_root) if state_root is not None else None
        references = (served_plan_mod.references_from_scan(scan) if scan is not None
                      else served_plan_mod.References({}, frozenset(), unknown="no state root"))
        plan = served_plan_mod.build_plan(
            published.routes, served_plan_mod.routes_from_document(document),
            references=references, state_root=str(state_root), before_unknown=published.unknown,
            gateway=served_plan_mod.gateway_label(document),
        )
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError, TypeError):
        return []
    if published.routes is None or not plan.served_changed:
        # Nothing published yet (a first init) has nothing to compare with.
        return []
    return [line.replace(served_plan_mod.HEADER, "Pending served change (publishing now; nothing is asked)")
            for line in plan.lines()]


ADOPT_ROOT_TEXT = (
    "Root adoption preview — nothing written yet\n"
    "  managed state root now: {managed}\n"
    "  requested state root:   {requested}\n"
    "  session records: {requested_count} under the requested root; {managed_count} under the managed root "
    "(metadata only; other historical roots are not known)\n"
    "After adoption, served changes and new managed sessions must use {requested}; sessions recorded "
    "under {managed} are no longer scanned for live references.\n"
    "Adopt {requested} as this gateway's state root? [y/N] "
)
ADOPT_ROOT_CHANGED = "the managed state root changed while awaiting confirmation — nothing adopted; rerun"
ADOPT_ROOT_REFERENCES_CHANGED = (
    "session records under the managed or the requested state root changed while awaiting "
    "confirmation — nothing adopted; rerun to review a fresh preview"
)


def _root_inventory(root: str | Path | None) -> tuple[continuity_mod.RecordScan | None, str]:
    """A root's record scan (the references and liveness the adoption
    preview shows) and its displayed record count."""

    if root is None:
        return None, "0"
    scan = continuity_mod.scan_records(Path(root))
    if scan.directory_error is not None:
        return scan, "unknown"
    return scan, str(len(scan.slots))


def _inventory_key(scan: continuity_mod.RecordScan | None) -> Any:
    if scan is None:
        return None
    references = served_plan_mod.references_from_scan(scan)
    return (references.fingerprint(), sorted(scan.slots), scan.unreadable, scan.fence_unreadable,
            scan.directory_error)


def adopt_root(
    home: Path, state_root: Path, *, environ: Mapping[str, str] | None = None,
    input_stream: Any = None, guard: Callable[[str, Mapping[str, str]], None] | None = None,
    barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
) -> bool:
    """``init --state-root R --adopt-root``: its own guarded
    transaction. An offline preview and a TTY-guarded confirmation (no lock
    held), then the migration guard of ``R``, then the gateway barrier, then
    the api-key leaf around the write of the existing ``state_root`` field
    (no new continuity field). Returns False when declined (nothing written).
    The confirmation is bound to both roots' record inventory (references,
    liveness, readability): any change by the commit phase refuses.
    """

    env = dict(os.environ if environ is None else environ)
    if guard is None:
        from claude_multi.cli import consent  # the one human guard (entry-level only)

        guard = consent.require_human
    guard("claude-multi-proxy init --adopt-root", env)
    managed = managed_state_root(home)
    try:
        current = continuity_mod.read(home)
    except continuity_mod.ContinuityError as exc:
        raise ProxyError(f"gateway continuity set unreadable ({exc}) — nothing adopted") from exc
    if managed == str(state_root):
        print(f"state root {state_root} is already this gateway's managed root", file=sys.stderr)
        return True
    # The reviewed inventory: both roots' references and liveness, bound to
    # the confirmation and revalidated inside the commit phase.
    requested_scan, requested_count = _root_inventory(state_root)
    managed_scan, managed_count = _root_inventory(managed)
    reviewed = (_inventory_key(managed_scan), _inventory_key(requested_scan))
    text = ADOPT_ROOT_TEXT.format(
        managed=managed or "none recorded", requested=state_root,
        requested_count=requested_count, managed_count=managed_count,
    )
    sys.stderr.write(text)
    sys.stderr.flush()
    reader = input_stream if input_stream is not None else sys.stdin
    answer = (reader.readline() or "").strip().lower()
    if answer not in ("y", "yes"):
        print("not adopted — nothing written", file=sys.stderr)
        return False
    state.ensure_private_dir(Path(state_root))
    with sessions_mod.served_change_phase(state_root, home, timeout=barrier_timeout):
        state.ensure_private_dir(config_dir(home))
        lock = state.FileLock(config_dir(home) / "api-key")
        lock.acquire(blocking=True)
        try:
            try:
                fresh = continuity_mod.read(home)
            except continuity_mod.ContinuityError as exc:
                raise ProxyError(f"gateway continuity set unreadable ({exc}) — nothing adopted") from exc
            if (fresh.get("state_root") if fresh is not None else None) != (
                    current.get("state_root") if current is not None else None):
                raise ProxyError(ADOPT_ROOT_CHANGED)
            # A launch or a session event that committed under either root
            # while the operator answered is absent from the reviewed preview.
            if (_inventory_key(_root_inventory(managed)[0]),
                    _inventory_key(_root_inventory(state_root)[0])) != reviewed:
                raise ProxyError(ADOPT_ROOT_REFERENCES_CHANGED)
            document = dict(fresh) if fresh is not None else continuity_mod.empty()
            document["state_root"] = str(state_root)
            continuity_mod.write(home, document)
        finally:
            lock.release()
    print(f"adopted state root {state_root} for this gateway", file=sys.stderr)
    return True


def _publish_prepared_render(
    home: Path, environ: dict[str, str] | None, state_root: Path, policy: str,
) -> tuple[Path, render_mod.RenderResult, ContinuityReport]:
    """Render, record the preparation receipt ``run --prepared`` verifies, report."""

    target, result, report = render_runtime_config(
        home, environ=environ, state_root=state_root, policy=policy,
    )
    state.atomic_write(config_dir(home) / PREPARED_STAMP,
                       strict_json.canonical_bytes(_prepared_inputs(home, state_root, environ)))
    for line in _summarize(result, target, report):
        print(line)
    for line in _continuity_problem_lines(report):
        print(line, file=sys.stderr)
    return target, result, report


def cmd_init(
    args: list[str], *, environ: dict[str, str] | None = None,
    models_get: ModelsGetter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    listener_observer: Callable | None = None,
    pid_get: Callable | None = None,
) -> int:
    """Prepare config, key and directories, then check readiness.

    No flag (an operator re-render): a hot-reload
    check that is informational only, exit 0. ``--reload-check`` (ExecReload):
    exit 0/3/4/5. ``--start-check`` (the post-switch proof after a
    (re)start): wait up to ``START_READINESS_TIMEOUT`` for
    the sentinel and one rendered OAuth alias; exit 0 ready, 6 not ready.
    """

    state_root, flag = _parse_init_args(args, environ)
    if flag == ADOPT_ROOT_FLAG:
        if not adopt_root(_home(environ), state_root, environ=environ):
            return 1
        flag = None
    reload_check = flag == "--reload-check"
    start_check = flag == "--start-check"
    home = _home(environ)
    workdir = gateway_workdir(state_root)
    status = "error"
    instance_lock: int | None = None
    # Attempt first, including failures preparing directories or rendering. If
    # stamps cannot be written, systemd's ExecReload exit remains the fallback.
    try:
        if reload_check:
            try:
                ensure_gateway_workdir(state_root)
                service.write_reload_attempt(workdir, service.utc_now(), __version__)
            except (OSError, errors.ClaudeMultiError):
                service.invalidate_reload_stamps(workdir)
        ensure_directories(home)
        ensure_gateway_workdir(state_root)
        if flag == "--prepare-start":
            instance_lock = _try_instance_lock(workdir)
        policy = {None: "explicit", "--reload-check": "reload", "--start-check": "reload",
                  "--prepare-start": "start"}[flag]
        if flag is None:
            # A manual init shows what it is about to publish
            # (stderr, never a prompt); the unit's preparation stays silent.
            for line in pending_served_summary(home, environ=environ, state_root=state_root):
                print(line, file=sys.stderr)
        target, result, report = _publish_prepared_render(home, environ, state_root, policy)
        gateway = load_bundle(environ, home=home).docs["gateway"]
        # The management key is the next write phase: a root adopted since
        # the render refuses it too (a fenced command only).
        gateway_inhibition.revalidate(home, what="the management key was not changed")
        _init_management(home, environ, prepare_start=flag == "--prepare-start",
                         gateway=gateway, state_root=state_root,
                         listener_observer=listener_observer, pid_get=pid_get,
                         instance_locked=instance_lock is not None)
        if start_check:
            outcome = await_sentinel(
                gateway, gateway_api_keys(home)[-1], result.sentinel,
                models_get=models_get, clock=clock, sleep=sleep,
                mode="start", oauth_aliases=result.oauth_aliases,
                owner_check=instance_listener(home, state_root, environ, gateway),
            )
            # A start check is not a reload: it records only a proven render
            # (ready implies the sentinel was served), never a failure status.
            status = "reloaded" if outcome.status == "ready" else None
            print(outcome.message, file=sys.stdout if outcome.status == "ready" else sys.stderr)
            return 0 if outcome.status == "ready" else START_CHECK_FAILED
        outcome = await_sentinel(
            gateway, gateway_api_keys(home)[-1], result.sentinel,
            models_get=models_get, clock=clock, sleep=sleep,
            owner_check=instance_listener(home, state_root, environ, gateway),
        )
        status = outcome.status
        print(outcome.message, file=sys.stdout if status in ("reloaded", "down") else sys.stderr)
        return RELOAD_CHECK_EXIT[status] if reload_check else 0
    finally:
        if instance_lock is not None:
            os.close(instance_lock)
        try:
            if status is not None:
                service.write_reload_stamp(workdir, status, service.utc_now(), __version__)
        except (OSError, errors.ClaudeMultiError):
            service.invalidate_reload_stamps(workdir)


def _try_instance_lock(workdir: Path) -> int | None:
    """The unit's start preparation takes the single-instance lock (bounded):
    while it holds it, no instance that honours the lock runs or can start,
    which completes the stopped proof the management key promotion needs.
    None when another instance holds it (then nothing is promoted). The
    descriptor is never inherited by a child."""

    try:
        descriptor = _hold_instance_lock(workdir)
    except InstanceBusyError:
        return None
    os.set_inheritable(descriptor, False)
    return descriptor


def cmd_status(
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
    health_get: Callable[[str, str], int] | None = None,
) -> int:
    """Loopback-only status; never initializes and never prints secrets."""

    home = _home(environ)
    initialized = (
        state_dir(home).is_dir()
        and (state_dir(home) / "auth").is_dir()
        and (config_dir(home) / "api-key").is_file()
        and (config_dir(home) / "config.yaml").is_file()
    )
    print(f"runtime: {'initialized' if initialized else 'not initialized'}")
    if management.allowlist_build(os.environ if environ is None else environ):
        print("management: " + _management_status(management.key_state(home)))
    else:
        print("management: unavailable in this build")
    bundle = load_bundle(environ, home=home)
    base_url = endpoint.gateway_endpoint(bundle.docs["gateway"]).base_url
    health_path = endpoint.gateway_endpoint(bundle.docs["gateway"]).health_path
    getter = health_get or launch_mod._default_health_get
    try:
        status = getter(base_url, health_path)
        print(f"proxy: {'running' if status == 200 else f'unhealthy ({status})'}")
    except Exception:
        print("proxy: stopped")
    return 0


def _prepare_gateway_workdir(state_root: Path) -> Path:
    """run/login: the private working directory, refused while it holds a
    ``.env`` — before anything is rendered."""

    workdir = ensure_gateway_workdir(state_root)
    _refuse_gateway_dotenv(workdir)
    return workdir


def _hold_instance_lock(workdir: Path, *, wait: float | None = None,
                        clock: Callable[[], float] = time.monotonic,
                        sleep: Callable[[float], None] = time.sleep) -> int:
    """Take the single-instance lock for this process and its exec'd gateway.

    A status reader's momentary probe can collide with the attempt, so it is
    retried for at most ``wait`` seconds (default :data:`INSTANCE_LOCK_WAIT`)
    before another instance is assumed.
    """

    deadline = clock() + (INSTANCE_LOCK_WAIT if wait is None else wait)
    while True:
        descriptor = posix_fs.hold_exclusive(workdir / service.INSTANCE_LOCK)
        if descriptor is not None:
            return descriptor
        if clock() >= deadline:
            raise InstanceBusyError(
                "another gateway instance is running or starting (its lock is held)",
                remedy="check it with: claude-multi gateway status",
            )
        sleep(0.05)


def _detach() -> None:
    """Fork once more and let the caller's child exit, so the gateway is never
    a child of the process that started it (the instance lock and the log
    descriptors stay with the gateway)."""

    sys.stdout.flush()
    sys.stderr.flush()
    if os.fork() > 0:
        os._exit(0)
    os.umask(0o077)


def _prepare_start_here(home: Path, environ: dict[str, str] | None, state_root: Path) -> None:
    """``init --prepare-start`` inside the starting process, without its readiness wait.

    This process holds the instance lock, so every instance that honours it
    is gone; an absent listener on the port completes the stopped proof the
    management key preparation needs. The parent checks readiness itself.
    """

    ensure_directories(home)
    _publish_prepared_render(home, environ, state_root, "start")
    gateway = load_bundle(environ, home=home).docs["gateway"]
    _init_management(
        home, environ, prepare_start=True, gateway=gateway, state_root=state_root,
        listener_observer=lambda base: service.listener_owner(base, stamp={"pid": None}),
        pid_get=lambda **_kwargs: (None, False),
    )


def _start_marker(instance: str, port: int | None) -> str:
    where = f", port {port}" if port is not None else ""
    return (f"{service.start_marker_prefix(instance)} "
            f"(pid {os.getpid()}{where}, launcher {__version__}, {service.utc_now()})")


def cmd_run(
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
    execve: Callable[[str, list[str], dict[str, str]], Any] = posix_fs.exec_replace,
    chdir: Callable[[str], Any] | None = None,
    detach: Callable[[], None] = _detach,
) -> Any:
    """Initialize safely, then execve the pinned proxy. No resident wrapper.

    Every mode takes the single-instance lock (``<state root>/gateway/
    gateway.lock``) before anything else and keeps it across the exec, stamps
    the start with a random instance nonce (``--instance`` supplies the one
    the starter named the log file after) and writes one start-marker line to
    stdout right before exec. ``--prepare-and-exec`` runs the start
    preparation and the prepared exec in this one process, with one
    environment; ``--detach`` (only with it) first forks away from the caller.

    ``chdir`` None (the default) changes no directory; the entrypoint passes
    ``os.chdir`` so the gateway runs in ``<state root>/gateway``.
    """

    state_root, mode, instance, detach_requested = _parse_run_args(args, environ)
    home = _home(environ)
    if mode == "--prepared":
        workdir = gateway_workdir(state_root)
        _refuse_gateway_dotenv(workdir)
        target = _prepared_config(home, state_root, environ)
        if not workdir.is_dir() or not (workdir / GATEWAY_LOGS).is_dir():
            raise ProxyError("gateway working directory is absent; run claude-multi-proxy init")
        lock = _hold_instance_lock(workdir)
    elif mode == "--prepare-and-exec":
        workdir = _prepare_gateway_workdir(state_root)
        lock = _hold_instance_lock(workdir)
        try:
            if detach_requested:
                detach()
            _prepare_start_here(home, environ, state_root)
            target = _prepared_config(home, state_root, environ)
        except BaseException:
            os.close(lock)
            raise
    else:
        ensure_directories(home)
        workdir = _prepare_gateway_workdir(state_root)
        lock = _hold_instance_lock(workdir)
        try:
            target, result, report = render_runtime_config(
                home, environ=environ, state_root=state_root, policy="start",
            )
        except BaseException:
            os.close(lock)
            raise
        for item in result.unavailable:
            print(f"provider unavailable: {item['provider']} ({item['reason']})", file=sys.stderr)
        for line in _continuity_problem_lines(report):
            print(line, file=sys.stderr)
    try:
        return _exec_gateway(home, environ, workdir, target, prepared=mode is not None,
                             instance=instance or secrets.token_hex(8), execve=execve, chdir=chdir)
    finally:
        # Reached only when exec failed (or a test double returned): the lock
        # is released; after a real exec the gateway keeps it until it exits.
        os.close(lock)


def _exec_gateway(home: Path, environ: dict[str, str] | None, workdir: Path, target: Path, *,
                  prepared: bool, instance: str,
                  execve: Callable[[str, list[str], dict[str, str]], Any],
                  chdir: Callable[[str], Any] | None) -> Any:
    binary = resolve_proxy_binary(environ)
    env = _gateway_exec_env(environ)
    if management.allowlist_build(os.environ if environ is None else environ):
        if not prepared:
            # This path is not a verified stop/start boundary. It may stage an
            # initial key but cannot promote it or switch a known active key.
            try:
                management.ensure(home)
            except management.ManagementKeyError as exc:
                _management_error(exc)
        key, notes = management.prepare_for_exec(home)
        _management_notes(notes)
        if key is not None:
            env["MANAGEMENT_PASSWORD"] = key
            print("gateway management: MANAGEMENT_PASSWORD from management-key "
                  "(read-only allowlist build)", file=sys.stderr)
    elif management.key_state(home).active != "absent":
        print(f"gateway management: management-key present but {management.UNAVAILABLE}; "
              "MANAGEMENT_PASSWORD not passed", file=sys.stderr)
    try:
        pid_namespace = linux_process.pid_namespace()
    except OSError:
        pid_namespace = None  # unknown namespace cannot prove a PID is absent
    try:
        service.write_exec_stamp(workdir, service.ExecStamp(
            __version__, exec_signature(), os.getpid(), str(binary), service.utc_now(),
            pid_namespace=pid_namespace, instance=instance))
    except (OSError, errors.ClaudeMultiError) as exc:
        # An older PID must not survive as stopped evidence for this new start.
        try:
            state.remove_private(workdir / service.EXEC_STAMP)
        except (OSError, errors.ClaudeMultiError):
            pass
        print(
            f"claude-multi-proxy: could not record the gateway start ({type(exc).__name__}); "
            "doctor cannot tell whether a restart is pending", file=sys.stderr,
        )
    try:
        port = endpoint.port_of(endpoint.gateway_endpoint(load_bundle(environ, home=home).docs["gateway"]).base_url)
    except (errors.ClaudeMultiError, OSError):
        port = None
    print(_start_marker(instance, port))
    sys.stdout.flush()
    sys.stderr.flush()
    if chdir is not None:
        chdir(str(workdir))
    return execve(str(binary), [str(binary), "--config", str(target), *EXEC_ARGV_FLAGS], env)


@dataclass(frozen=True)
class LoginInvocation:
    """The gateway program's sign-in command, ready to run (its environment
    is the scrubbed gateway environment; it holds no credential)."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str

    def __repr__(self) -> str:
        return f"LoginInvocation(argv=[…, {', '.join(self.argv[1:])}], cwd={self.cwd})"


def login_invocation(command: str, *, environ: Mapping[str, str] | None, state_root: Path,
                     no_browser: bool = False) -> LoginInvocation:
    """Everything a sign-in does before it runs the gateway program: the
    private working directory, the start render, the pinned binary and the
    scrubbed environment. ``no_browser`` makes the program print the address
    instead of opening a browser."""

    env_in = None if environ is None else dict(environ)
    home = _home(env_in)
    ensure_directories(home)
    workdir = _prepare_gateway_workdir(state_root)
    target, result, report = render_runtime_config(
        home, environ=env_in, state_root=state_root, policy="start",
    )
    for item in result.unavailable:
        print(
            f"provider unavailable: {item['provider']} ({item['reason']})",
            file=sys.stderr,
        )
    for line in _continuity_problem_lines(report):
        print(line, file=sys.stderr)
    binary = resolve_proxy_binary(env_in)
    env = _gateway_exec_env(env_in)
    argv = [str(binary), "--config", str(target), "--local-model", LOGIN_FLAGS[command]]
    if no_browser:
        argv.append("--no-browser")
    return LoginInvocation(tuple(argv), env, str(workdir))


def cmd_login(
    command: str,
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
    execve: Callable[[str, list[str], dict[str, str]], Any] = posix_fs.exec_replace,
    chdir: Callable[[str], Any] | None = None,
) -> Any:
    """Exec the pinned proxy's supported OAuth/device flow via existing auth paths.

    Refused unless the account's personal-use acknowledgement is current
    (``claude-multi providers sign-in`` shows it first). ``chdir`` as for
    :func:`cmd_run` (None = no chdir).
    """

    from claude_multi.setup import signin

    state_root = _parse_state_root(command, args, environ)
    refusal = signin.login_refusal(command, os.environ if environ is None else environ)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    invocation = login_invocation(command, environ=environ, state_root=state_root)
    if chdir is not None:
        chdir(invocation.cwd)
    return execve(invocation.argv[0], list(invocation.argv), dict(invocation.env))


def cmd_snapshot_auth(
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
    service_active: Callable[[str], bool] | None = None,
    gateway_status: Callable[..., service.GatewayStatus] | None = None,
) -> int:
    """`snapshot-auth` / `snapshot-auth --restore NAME`.

    Prints paths, the file count and file NAMES only — the files hold OAuth
    tokens, so their contents are never read into output.
    """

    gateway_info = load_bundle(environ).docs["gateway"]["gateway"]
    if not args:
        target, names = snapshot_auth_dir(gateway_info, environ=environ)
        print(f"snapshot: {target}")
        print(f"files: {len(names)}")
        for name in names:
            print(f"  {name}")
        print(
            "restore on rollback (gateway stopped): "
            f"claude-multi-proxy snapshot-auth --restore {target.name}"
        )
        return 0
    if len(args) == 2 and args[0] == "--restore":
        live, moved, names = restore_auth_dir(
            gateway_info, args[1], environ=environ, service_active=service_active,
            gateway_status=gateway_status
        )
        print(f"restored: {live} <- {args[1]}")
        if moved is not None:
            print(f"previous auth dir kept at: {moved}")
        print(f"files: {len(names)}")
        for name in names:
            print(f"  {name}")
        return 0
    raise ProxyError("usage: claude-multi-proxy snapshot-auth [--restore SNAPSHOT]")


# The supervised unit's own directives (ExecStartPre, ExecStart, ExecReload):
# the service manager runs them, and every claude-multi path that asks it to
# start, restart or reload the unit is itself fenced (see _entry_refusal).
UNIT_DIRECTIVES = (("init", "--prepare-start"), ("init", "--reload-check"), ("run", "--prepared"))
# Commands that render the configuration or run the gateway program.
_RENDERING = frozenset({"init", "run", *LOGIN_FLAGS})
_STATE_WRITERS = frozenset({*_RENDERING, "rotate-management-key", "disable-management-key", "snapshot-auth"})


def _entry_roots(home: Path, root: Path, *, reading: bool) -> tuple[Path, ...]:
    """The roots an entry check covers (``gateway_inhibition.home_roots``).
    An unreadable managed-root authority refuses every change; a command
    that only reads (a plain ``snapshot-auth`` copy) then checks its own
    root alone."""

    try:
        return gateway_inhibition.home_roots(home, root)
    except gateway_inhibition.AuthorityUnreadable:
        if reading:
            return (root,)
        raise


def _entry_refusal(command: str, rest: list[str], environ: dict[str, str] | None,
                   fence: contextlib.ExitStack | None = None) -> ProxyError | None:
    """The checks every ``claude-multi-proxy`` command that changes state
    passes before it does anything (it runs outside the launcher's command
    dispatcher, so it applies them itself):

    - set up: a home with no recorded endpoint and no earlier gateway is not
      rendered for, run in or signed in from on the packaged port — the
      unit's directives included (a service install always records the
      endpoint first);
    - the channel: a state root another installation's channel owns is
      refused (read, never claimed: only the launcher claims it), for the
      requested root and for the managed root ``continuity.json`` records
      (the configuration and keys this command changes are the home's; a
      ``continuity.json`` that cannot be read refuses every change, since
      that root is unknown);
    - the inhibition: while a transaction owner holds the inhibition of
      either root, only that owner (its token in the environment) prepares,
      runs or signs in a gateway, renders its configuration, adopts a root
      or changes its keys (a plain ``snapshot-auth`` copy reads only). The
      check is made inside both roots' writer fences, which ``fence`` (the
      caller's exit stack) keeps held until the command finished, or until
      the gateway program replaces it (the fence is not inherited by exec).
      The managed root is read again under the fences and before each
      write: a root adopted meanwhile refuses (``revalidate``).

    Exceptions, reviewed: ``status``, help and the version only read; the
    unit's directives (:data:`UNIT_DIRECTIVES`) run what the service manager
    was asked to run (``init --prepare-start`` proves the stopped state with
    the instance lock, ``run --prepared`` holds it), so they skip the channel
    and the inhibition. A command line this cannot parse is left to the
    command's own usage error.
    """

    if command not in _STATE_WRITERS:
        return None
    from . import installs

    env = dict(os.environ if environ is None else environ)
    home = _home(environ)
    try:
        if command == "init":
            root = _parse_init_args(rest, environ)[0]
        elif command == "run":
            root = _parse_run_args(rest, environ)[0]
        elif command in LOGIN_FLAGS:
            root = _parse_state_root(command, rest, environ)
        else:
            root = sessions_mod.state_root(environ)
    except ProxyError:
        return None
    if command in _RENDERING and not endpoint.set_up(home):
        return ProxyError(f"{endpoint.NOT_SET_UP}; nothing was changed", remedy=endpoint.SETUP_COMMAND)
    if any((command, flag) in UNIT_DIRECTIVES for flag in rest):
        return None
    reading = command == "snapshot-auth" and "--restore" not in rest
    try:
        roots = _entry_roots(home, root, reading=reading)
    except gateway_inhibition.AuthorityUnreadable as exc:
        return ProxyError(str(exc), remedy=exc.remedy)
    for each in roots:
        check = installs.check(each, env, record=False)
        if not check.ok:
            return ProxyError(f"{check.problem}; nothing was changed", remedy=check.remedy)
    if reading:
        return None
    try:
        held = gateway_inhibition.fenced(roots, token=env.get(gateway_inhibition.TOKEN_ENV) or None, home=home)
        if fence is not None:
            fence.enter_context(held)
        else:
            with held:
                pass
    except gateway_inhibition.Inhibited as exc:
        return ProxyError(str(exc), remedy=exc.remedy)
    except gateway_inhibition.InhibitionError as exc:
        return ProxyError(f"{exc}", remedy=exc.remedy)
    return None


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    execve: Callable[[str, list[str], dict[str, str]], Any] = posix_fs.exec_replace,
    health_get: Callable[[str, str], int] | None = None,
    service_active: Callable[[str], bool] | None = None,
    gateway_status: Callable[..., service.GatewayStatus] | None = None,
    models_get: ModelsGetter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    chdir: Callable[[str], Any] | None = None,
    listener_observer: Callable | None = None,
    pid_get: Callable | None = None,
) -> Any:
    """Dispatch one command. ``chdir`` reaches only ``run`` and the login
    helpers (None = no chdir; ``bin/claude-multi-proxy`` passes ``os.chdir``).
    """

    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(PROXY_USAGE)
        return 0
    command, rest = args[0], args[1:]
    if command in ("-v", "--version"):
        from . import __version__

        print(f"claude-multi-proxy {__version__}")
        return 0
    descriptor = _PROXY_COMMANDS.get(command)
    if descriptor is None:
        print(f"claude-multi-proxy: unknown command: {command}", file=sys.stderr)
        print(PROXY_USAGE, file=sys.stderr)
        return 1
    if rest and rest[0] in HELP_ARGUMENTS:
        # Its own help, before any check, initialization or observation.
        print(descriptor.help())
        return 0
    try:
        # Options are validated before anything runs or is read.
        descriptor.check(rest, environ)
    except ProxyError as exc:
        print(f"claude-multi-proxy: {exc}", file=sys.stderr)
        return 1
    fence = contextlib.ExitStack()  # the entry check's writer fence, held through the command
    try:
        refusal = _entry_refusal(command, rest, environ, fence)
        if refusal is not None:
            raise refusal
        if command == "init":
            return cmd_init(rest, environ=environ, models_get=models_get, clock=clock, sleep=sleep,
                            listener_observer=listener_observer, pid_get=pid_get)
        if command == "rotate-management-key":
            return cmd_rotate_management_key(rest, environ=environ)
        if command == "disable-management-key":
            return cmd_disable_management_key(rest, environ=environ)
        if command == "status":
            return cmd_status(rest, environ=environ, health_get=health_get)
        if command == "run":
            try:
                return cmd_run(rest, environ=environ, execve=execve, chdir=chdir)
            except InstanceBusyError as exc:
                print(f"claude-multi-proxy: {exc}", file=sys.stderr)
                return INSTANCE_BUSY_EXIT
        if command in LOGIN_FLAGS:
            return cmd_login(command, rest, environ=environ, execve=execve, chdir=chdir)
        if command == "snapshot-auth":
            return cmd_snapshot_auth(rest, environ=environ, service_active=service_active,
                                     gateway_status=gateway_status)
        raise AssertionError(f"claude-multi-proxy {command} has no dispatch")  # PROXY_COMMANDS is closed
    except errors.ClaudeMultiError as exc:
        # Domain refusals are concise errors, never a traceback loop under
        # the systemd unit (including corrupt catalog/custom input).
        print(f"claude-multi-proxy: {exc}", file=sys.stderr)
        if exc.remedy:
            print(f"  fix: {exc.remedy}", file=sys.stderr)
        return 1
    except OSError as exc:
        # snapshot-auth copy/rename failures: the path and reason, one line.
        print(f"claude-multi-proxy: {exc.strerror or type(exc).__name__}"
              f"{f' ({exc.filename})' if exc.filename else ''}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Ctrl-C during a render, a readiness wait or a sign-in: one line,
        # exit 130; the writer fence is released below like on every exit.
        print(f"\nclaude-multi-proxy: {command} cancelled", file=sys.stderr)
        return CANCELLED_EXIT
    finally:
        fence.close()
