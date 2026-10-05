"""Disposable no-provider capability probe harness.

This module backs the explicitly gated ``claude-multi-dev probe`` command
used by the client capability probes. It builds
a fully disposable fixture — private HOME/XDG/config/state/cache/runtime
directories, zero provider tokens — and a deterministic loopback-only fake
Anthropic endpoint, so the pinned Claude binary can later be exercised
without any real provider, credential, user transcript, or contact with the
live shared daemon.

Fail-closed refusals:

- missing ``--allow-local-claude`` or ``--fixture-root``
  (``--allow-local-claude`` is presence-based explicit consent: its value
  is ignored and it never relaxes any other check);
- fixture roots that are relative, contain ``..``, resolve through a
  symlink (leaf or any ancestor), are not owner-private, or that equal,
  contain, or enter the live HOME, XDG, ``CLAUDE_CONFIG_DIR``, Claude
  config, claude-multi state, or uid-shared daemon paths (live roots are
  canonicalized before comparison, so symlinked environment roots cannot
  hide an overlap);
- inherited provider credentials in the ambient environment;
- non-loopback or non-http provider endpoints (only the ``127.0.0.1``
  literal is accepted; IPv6 is rejected consistently rather than bound);
- native execution whose trusted executable spec does not pin the exact
  absolute non-symlink path and full SHA-256 of the artifact, or whose
  artifact is group/other-writable.

Daemon-isolation posture: the fixture does NOT redirect Claude's uid-keyed
``/tmp/cc-daemon-<uid>`` daemon domain. Static evidence in
the pinned 2.1.217 binary shows the domain is parameterized per config root
(``cc-daemon-${h5g()}-${e}``, a ``sha256(config-root)[:8]`` subdomain, and the
redaction pattern ``cc-daemon-[0-9a-f]{16}``), so a fixture
``CLAUDE_CONFIG_DIR`` should yield a private daemon domain. The real pinned
binary may therefore run under a narrow allowance (``run_native(...,
allow_real=True)``): CLAUDE_CONFIG_DIR inside the disposable fixture root,
the trusted path+sha256 check, the loopback fake provider, a clean ambient
provider-credential scan, and a pre/post snapshot of the live uid-shared
domain that fails closed with "live daemon domain touched" on any new,
removed, or changed entry. Sibling per-config-root domains next to the
live one (the parameterized ``cc-daemon-${h5g()}-${e}`` naming) are
recorded before and after the run, names only, as the fixture-domain
observation: positive evidence that the fixture received its own domain.
Without ``allow_real`` only fake-executable unit probes (trusted spec
marked ``fake=True``) run.

Scan-only ``environ``: the credential scan and the live-roots check run
against the ``environ`` mapping the caller passes (default: the real
``os.environ``). The client check tests and ``test_scope_probe.py`` pass a
SYNTHETIC ambient mapping (a fake ``HOME`` under their scratch root and a
fixed ``PATH``), which disables both checks against the real environment:
their scratch roots live under the real ``XDG_CACHE_HOME`` when the suite
runs with ``TMPDIR=~/.cache/...``, which ``build_fixture`` would refuse
under the real environment. The child never inherits either mapping; the
network namespace, the fixture tmpdir, and the live-domain tripwire below
do not depend on it.

Network hermeticity (every real run): a real (``fake=False``) spec never
runs on the host network. ``run_native``/``run_native_pty``/the takeover
probe exec the verified artifact inside a loopback-only network namespace
(``bwrap --unshare-net --die-with-parent --dev-bind / /``, host resolver
sockets, the user runtime dir (session bus, agents) and container-engine
sockets masked, same uid so the uid-keyed tripwire still applies). The
fake provider is bridged in over a filesystem unix socket inside the
fixture (``<fixture>/run/provider-<port>.sock``, 0600): a stdlib relay
started inside the namespace listens on ``127.0.0.1:<port>`` and forwards
each connection to that socket, so the client's ``ANTHROPIC_BASE_URL`` is
unchanged while ``api.anthropic.com``, plugin-marketplace git fetches,
DNS, and the live gateway are all unreachable. ``CLAUDE_CODE_TMPDIR`` and
``TMPDIR`` point at ``<fixture>/tmp`` so nothing is written under the
shared ``/tmp/claude-<uid>``. Without a usable bubblewrap the real run
fails closed (:func:`network_isolation_available` names the reason).
:func:`start_isolated_process` runs other trusted-by-path programs (the
S12 gateway) in their own loopback namespace with explicit unix-socket
port bridges in both directions.

The probed process never inherits the ambient environment: it receives only
the constructed disposable variables plus a fixed non-secret dummy token
(or, for helper-only auth probes, no env token at all). The only additions
are typed allowlisted overrides (``validate_probe_env``: never a
credential-shaped name except the bounded helper-TTL integer) and validated
owner-private bin dirs inside the fixture prepended to the fixed PATH.
Captured request evidence is metadata only (method, path, model, tool
names, auth classification/label, body SHA-256, ``max_tokens``, effort and
thinking shape, ``x-claude-code-*`` header names plus short safe-id values,
names of configured content markers found); prompt and transcript content
is never persisted to disk or evidence.

Residual: the lstat/hash-to-exec window cannot be fully closed without
exec-time fd-based verification; a same-uid attacker replacing the pinned
artifact between hashing and exec is a recorded, accepted residual for this
probe harness.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import http.client
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import errors, state, strict_json


class ProbeError(errors.ClaudeMultiError, RuntimeError):
    """Raised on any probe safety violation (fail closed)."""


class PTYTimeoutError(ProbeError):
    """A failed PTY deadline, with phase metadata but no captured text.

    The child group is killed and reaped before this escapes run_native_pty.
    A completed script is not a completed run: client-exit timeouts still fail.
    """

    def __init__(self, phase: str, completed_interactions: int,
                 total_interactions: int, elapsed_seconds: float):
        self.phase = phase
        self.completed_interactions = completed_interactions
        self.total_interactions = total_interactions
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"probe PTY timed out in {phase} phase; "
            f"interactions={completed_interactions}/{total_interactions}; "
            f"elapsed={elapsed_seconds:.3f}s"
        )


DUMMY_TOKEN = "claude-multi-probe-dummy-token"

_LOOPBACK_HOSTS = ("127.0.0.1",)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_BODY_BYTES = 1024 * 1024
_RECORD_CAP = 1024
_MESSAGE_HASH_CAP = 64
_TOOL_NAME_CAP = 64
_METADATA_TEXT_MAX_BYTES = 160
# Test-harness timing: the fake-provider listeners poll for
# shutdown every 10 ms instead of socketserver's 0.5 s default, so stop()
# (and every probe fixture teardown) returns promptly.
_SERVE_POLL_INTERVAL = 0.01
_METADATA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")
_PUBLIC_METADATA_PATHS = frozenset(
    {"/healthz", "/v1/messages", "/v1/messages/count_tokens", "/v1/models"}
)
_MIN_AUTO_COMPACT_WINDOW = 100_000
_MAX_AUTO_COMPACT_WINDOW = 1_000_000
_REAL_RUN_REFUSAL = (
    "refusing real native execution without allow_real: the pinned Claude "
    "binary runs only under the narrow allowance — run_native(..., "
    "allow_real=True) with CLAUDE_CONFIG_DIR inside the disposable fixture "
    "root, the loopback fake provider, a clean ambient credential scan, and "
    "the live-domain pre/post snapshot tripwire; without it only "
    "fake-executable unit probes (trusted spec marked fake=True) run."
)
_PROVIDER_PREFIXES = (
    "ANTHROPIC",
    "CLAUDE",
    "OPENAI",
    "GEMINI",
    "GOOGLE",
    "MISTRAL",
    "COHERE",
    "MOONSHOT",
    "KIMI",
    "DEEPSEEK",
    "AWS",
    "AZURE",
    "GROQ",
    "XAI",
    "HF",
    "HUGGINGFACE",
    "OPENROUTER",
    "TOGETHER",
    "FIREWORKS",
    "PERPLEXITY",
    "LITELLM",
    "HELICONE",
    "PORTKEY",
)
_CREDENTIAL_MARKERS = ("KEY", "TOKEN", "SECRET", "PASS")
# Narrow child-environment overrides for the client verification probes. Each
# entry names its value kind: "flag" (0/1/true/false), "model" (a bounded
# model identifier), or "int" (bounded decimal). Anything else is refused.
_PROBE_ENV_RULES: dict[str, tuple[str, int, int]] = {
    "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": ("flag", 0, 0),
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": ("flag", 0, 0),
    "CLAUDE_CODE_WORKFLOWS": ("flag", 0, 0),
    "CLAUDE_CODE_DISABLE_WORKFLOWS": ("flag", 0, 0),
    "CLAUDE_CODE_SUBAGENT_MODEL": ("model", 0, 0),
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": ("model", 0, 0),
    "ANTHROPIC_DEFAULT_OPUS_MODEL": ("model", 0, 0),
    "ANTHROPIC_DEFAULT_FABLE_MODEL": ("model", 0, 0),
    "ANTHROPIC_DEFAULT_SONNET_MODEL": ("model", 0, 0),
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": ("model", 0, 0),
    # Credential-shaped by name (KEY) but carries only a bounded integer TTL;
    # admitted solely through its integer rule.
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": ("int", 0, 86_400_000),
    "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": ("int", 1, 64),
    "CLAUDE_CODE_RETRY_WATCHDOG": ("flag", 0, 0),
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": ("int", 1, 64),
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": ("int", 1, 16),
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": ("int", 1, 1000),
    "ANTHROPIC_SMALL_FAST_MODEL": ("model", 0, 0),
}
PROBE_ENV_ALLOWLIST = frozenset(_PROBE_ENV_RULES)
_ENV_FLAG_VALUES = frozenset({"0", "1", "true", "false"})
_ENV_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")
_PATH_DIR_ENTRY_CAP = 64
_FIXTURE_EXECUTABLE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FIXTURE_EXECUTABLE_MAX_BYTES = 1024 * 1024
# Fixed non-secret test ids only: short (<= 40 chars) and never shaped like
# a provider credential (sk-/sk-ant-/pk- prefixes are refused outright).
_KNOWN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{3,39}")
_CREDENTIAL_TOKEN_PREFIXES = ("sk-", "sk_", "pk-", "rk-", "sess-", "ghp_", "xai-")
_AUTH_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_RESERVED_AUTH_LABELS = frozenset({"dummy", "other", "absent"})
_KNOWN_TOKEN_CAP = 16
_AUTH_POLICY_STATUSES = {
    401: "authentication_error",
    403: "permission_error",
}
_CONTENT_MARKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_CONTENT_MARKER_CAP = 16
_HINT_HEADER_PREFIX = "x-claude-code-"
_HINT_HEADER_CAP = 32
_HEADER_NAME_CAP = 64
# Hint headers whose short safe-id values may be recorded; every other
# x-claude-code-* header (authorization/signature shapes included) is
# recorded by name only.
_HINT_VALUE_HEADERS = frozenset(
    {
        "x-claude-code-agent-id",
        "x-claude-code-agent-type",
        "x-claude-code-compaction",
        "x-claude-code-context-compacted",
        "x-claude-code-parent-agent-id",
        "x-claude-code-request-class",
        "x-claude-code-session-id",
    }
)
_HINT_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


# ------------------------------------------------------------- credentials


def _credential_shaped(name: str) -> bool:
    """Provider-prefixed variable names carrying a credential marker."""

    upper = name.upper()
    return upper.startswith(_PROVIDER_PREFIXES) and any(
        marker in upper for marker in _CREDENTIAL_MARKERS
    )


def assert_no_provider_credentials(environ: Mapping[str, str]) -> None:
    """Refuse an ambient environment carrying provider credentials.

    Offending variable names are reported; values are never read into
    messages or evidence.
    """

    offenders = sorted(
        name for name, value in environ.items() if value and _credential_shaped(name)
    )
    if offenders:
        raise ProbeError(
            "inherited provider credentials are forbidden in the probe "
            "environment; unset them first: " + ", ".join(offenders)
        )


def validate_probe_env(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Validate narrow child-environment overrides (fail closed).

    Only names in :data:`PROBE_ENV_ALLOWLIST` are accepted, each with a
    typed value rule (boolean flag, bounded model id, or bounded integer).
    Credential-shaped names are refused unless their rule is a bounded
    integer (the helper TTL). Refusals name the variable, never the value.
    """

    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise ProbeError("probe env overrides must be a mapping of names to strings")
    validated: dict[str, str] = {}
    for name, value in overrides.items():
        if not isinstance(name, str) or name not in _PROBE_ENV_RULES:
            shown = (
                name
                if isinstance(name, str) and len(name) <= 80 and name.isprintable()
                else "<unprintable>"
            )
            if isinstance(name, str) and _credential_shaped(name):
                raise ProbeError(
                    f"probe env override {shown} is credential-shaped and refused"
                )
            raise ProbeError(
                f"probe env override {shown} is not on the probe allowlist"
            )
        kind, low, high = _PROBE_ENV_RULES[name]
        if _credential_shaped(name) and kind != "int":
            raise ProbeError(
                f"probe env override {name} is credential-shaped and refused"
            )
        if not isinstance(value, str):
            raise ProbeError(f"probe env override {name} must be a string")
        if kind == "flag":
            valid = value in _ENV_FLAG_VALUES
        elif kind == "model":
            valid = bool(_ENV_MODEL_RE.fullmatch(value))
        else:
            valid = (
                value.isascii()
                and value.isdigit()
                and len(value) <= 12
                and low <= int(value) <= high
            )
        if not valid:
            raise ProbeError(
                f"probe env override {name} has a value outside its {kind} rule"
            )
        validated[name] = value
    return validated


def _check_probe_token(token: str | None) -> None:
    """Env tokens are None (helper-only auth) or fixed short test ids."""

    if token is None:
        return
    if (
        not isinstance(token, str)
        or not _KNOWN_TOKEN_RE.fullmatch(token)
        or token.lower().startswith(_CREDENTIAL_TOKEN_PREFIXES)
    ):
        raise ProbeError(
            "probe env token must be None or a short fixed non-secret test id "
            "(<= 40 chars, never credential-shaped)"
        )


# ------------------------------------------------------------ path safety


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _live_roots(environ: Mapping[str, str]) -> dict[str, Path]:
    """Live paths a fixture root must never equal, contain, or enter.

    Ordered most-specific first so containment refusals name the tightest
    live domain; equality is checked in a separate first pass.
    """

    home = Path(environ.get("HOME") or Path.home())
    state_home = Path(environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    roots = {
        "Claude config root": home / ".claude",
        "claude-multi state root": state_home / "claude-multi",
        "uid-shared daemon socket": Path(f"/tmp/cc-daemon-{os.geteuid()}"),
        "XDG_CONFIG_HOME": Path(environ.get("XDG_CONFIG_HOME") or home / ".config"),
        "XDG_STATE_HOME": state_home,
        "XDG_CACHE_HOME": Path(environ.get("XDG_CACHE_HOME") or home / ".cache"),
        "XDG_DATA_HOME": Path(
            environ.get("XDG_DATA_HOME") or home / ".local" / "share"
        ),
        "HOME": home,
    }
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime:
        roots["XDG_RUNTIME_DIR"] = Path(runtime)
    claude_dir = environ.get("CLAUDE_CONFIG_DIR")
    if claude_dir:
        roots["CLAUDE_CONFIG_DIR"] = Path(claude_dir)
    return roots


def _resolve_fixture_root(root: Path | str) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ProbeError(f"fixture root {candidate} must be an absolute path")
    if ".." in candidate.parts:
        raise ProbeError(f"fixture root {candidate} must not contain '..'")
    real = Path(os.path.realpath(candidate))
    if real != candidate:
        raise ProbeError(
            f"fixture root {candidate} resolves through a symlink or non-normal "
            f"component to {real}; only real private paths are allowed"
        )
    return real


def _check_live_roots(root: Path, environ: Mapping[str, str]) -> None:
    # Canonicalize every live root: an environment root that passes through
    # a symlink must still refuse a fixture overlapping its real target.
    live_roots = {
        label: Path(os.path.realpath(live))
        for label, live in _live_roots(environ).items()
    }
    for label, live in live_roots.items():
        if root == live:
            raise ProbeError(f"fixture root {root} is the live {label} path")
    for label, live in live_roots.items():
        if _is_within(live, root):
            raise ProbeError(
                f"fixture root {root} contains the live {label} path {live}"
            )
        # Disposable trees under HOME itself are fine; nesting inside a live
        # config/state/daemon tree is never fine.
        if label != "HOME" and _is_within(root, live):
            raise ProbeError(
                f"fixture root {root} is inside the live {label} path {live}"
            )


def _ensure_private(path: Path, what: str) -> Path:
    try:
        return state.ensure_private_dir(path)
    except (state.StateError, OSError) as exc:
        raise ProbeError(
            f"{what} {path} is not a safe private directory: {exc}"
        ) from exc


# --------------------------------------------------------------- endpoints


def check_loopback_url(base_url: str) -> str:
    """Require an explicit http loopback URL with an explicit port."""

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http":
        raise ProbeError(f"provider endpoint {base_url!r} is not loopback http")
    if parts.hostname not in _LOOPBACK_HOSTS:
        raise ProbeError(f"provider endpoint {base_url!r} is not loopback http")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProbeError(
            f"provider endpoint {base_url!r} has an invalid port"
        ) from exc
    if port is None:
        raise ProbeError(f"provider endpoint {base_url!r} requires an explicit port")
    return base_url


# ----------------------------------------------------------- daemon domain


def live_daemon_domain() -> Path:
    """The uid-shared daemon domain the pinned binary namespaces per root."""

    return Path(f"/tmp/cc-daemon-{os.geteuid()}")


def expected_daemon_subdomain(config_dir: Path | str) -> str:
    """Config-root subdomain name the pinned binary derives (static evidence).

    The 2.1.217 binary computes ``sha256(resolve(config_root))[:8]``; fixture
    paths are symlink-free by construction, so ``os.path.realpath`` matches
    the binary's ``path.resolve``.
    """

    resolved = os.path.realpath(config_dir)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class DaemonDomainEntry:
    """One live-domain entry: hash-level metadata only, never contents."""

    name: str
    kind: str  # "dir" | "file" | "sock" | "other" | "vanished"
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class DaemonDomainSnapshot:
    """Shallow snapshot of the uid-shared domain: entry set plus mtimes."""

    domain: Path
    present: bool
    dir_mtime_ns: int | None
    entries: tuple[DaemonDomainEntry, ...]


def snapshot_daemon_domain(domain: Path | None = None) -> DaemonDomainSnapshot:
    """Snapshot the live daemon domain's entry set and mtimes.

    Absence is tolerated and recorded as ``present=False``; the scan is
    shallow (one level) so the live supervisor's internal socket churn
    beneath its own subdomain does not register as a change.
    """

    target = domain if domain is not None else live_daemon_domain()
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return DaemonDomainSnapshot(target, False, None, ())
    if not stat.S_ISDIR(info.st_mode):
        raise ProbeError(f"live daemon domain {target} is not a directory")
    entries: list[DaemonDomainEntry] = []
    with os.scandir(target) as iterator:
        for entry in iterator:
            try:
                meta = os.lstat(entry.path)
            except FileNotFoundError:
                entries.append(DaemonDomainEntry(entry.name, "vanished", 0, 0))
                continue
            if stat.S_ISDIR(meta.st_mode):
                kind = "dir"
            elif stat.S_ISREG(meta.st_mode):
                kind = "file"
            elif stat.S_ISSOCK(meta.st_mode):
                kind = "sock"
            else:
                kind = "other"
            entries.append(
                DaemonDomainEntry(entry.name, kind, meta.st_mtime_ns, meta.st_size)
            )
    entries.sort(key=lambda item: item.name)
    return DaemonDomainSnapshot(target, True, info.st_mtime_ns, tuple(entries))


def fixture_daemon_domains(live: Path) -> tuple[str, ...]:
    """Names of sibling per-config-root daemon domains next to ``live``.

    Metadata-only directory listing (names only, never contents). The
    parameterized ``cc-daemon-${h5g()}-${e}`` naming implies non-default
    config roots get sibling domains next to the uid-shared one; the live
    uid-shared domain itself is excluded. Absence is tolerated.
    """

    try:
        with os.scandir(live.parent) as iterator:
            names = [
                entry.name
                for entry in iterator
                if entry.name.startswith("cc-daemon-") and entry.name != live.name
            ]
    except FileNotFoundError:
        return ()
    return tuple(sorted(names))


def _domain_changes(
    before: DaemonDomainSnapshot, after: DaemonDomainSnapshot
) -> tuple[str, ...]:
    """Name-level diff of two snapshots; empty means byte-identical."""

    if before.domain != after.domain:
        raise ProbeError("daemon domain snapshots cover different domains")
    changes: list[str] = []
    if before.present != after.present:
        changes.append("domain-appeared" if after.present else "domain-vanished")
    elif before.present and before.dir_mtime_ns != after.dir_mtime_ns:
        changes.append("domain-dir-mtime")
    prior = {entry.name: entry for entry in before.entries}
    current = {entry.name: entry for entry in after.entries}
    for name in sorted(current.keys() - prior.keys()):
        changes.append(f"added:{name}")
    for name in sorted(prior.keys() - current.keys()):
        changes.append(f"removed:{name}")
    for name in sorted(prior.keys() & current.keys()):
        if prior[name] != current[name]:
            changes.append(f"changed:{name}")
    return tuple(changes)


@dataclass(frozen=True)
class DaemonDomainObservation:
    """Metadata-only pre/post record of the live-domain tripwire.

    ``fixture_domains_before``/``fixture_domains_after`` are the sibling
    per-config-root domain names observed next to the live domain (never
    their contents); ``fixture_subdomain_observed`` is true when the
    fixture's expected config-root hash subdomain is visible either as a
    live-domain entry or inside a sibling domain name.
    """

    domain: str
    present_before: bool
    present_after: bool
    entries_before: tuple[str, ...]
    entries_after: tuple[str, ...]
    unchanged: bool
    expected_fixture_subdomain: str
    fixture_subdomain_observed: bool
    fixture_domains_before: tuple[str, ...] = ()
    fixture_domains_after: tuple[str, ...] = ()


def _enforce_live_domain_untouched(
    before: DaemonDomainSnapshot,
    after: DaemonDomainSnapshot,
    *,
    fixture: "ProbeFixture",
    fixture_domains_before: tuple[str, ...] = (),
    fixture_domains_after: tuple[str, ...] = (),
) -> DaemonDomainObservation:
    """Fail closed unless the live uid-shared domain is byte-identical.

    Any new, removed, or changed entry (or the domain (dis)appearing) is a
    hard failure: the run touched the live daemon domain. This tripwire is
    strictly observe-only and never attempts remediation inside the live
    domain, including entries that appear fixture-owned.
    """

    expected = expected_daemon_subdomain(fixture.claude_config_dir)
    observed = any(entry.name == expected for entry in after.entries) or any(
        expected in name for name in fixture_domains_after
    )
    changes = _domain_changes(before, after)
    if changes:
        raise ProbeError(
            "live daemon domain touched: "
            + ", ".join(changes)
            + " (observe-only; no remediation performed)"
        )
    return DaemonDomainObservation(
        domain=str(after.domain),
        present_before=before.present,
        present_after=after.present,
        entries_before=tuple(entry.name for entry in before.entries),
        entries_after=tuple(entry.name for entry in after.entries),
        unchanged=True,
        expected_fixture_subdomain=expected,
        fixture_subdomain_observed=observed,
        fixture_domains_before=fixture_domains_before,
        fixture_domains_after=fixture_domains_after,
    )


# ----------------------------------------------------------------- fixture


@dataclass(frozen=True)
class ProbeCompactionPolicy:
    """Narrow allowlisted compaction controls for disposable native probes."""

    auto_compact_window: int
    auto_compact_percent: int
    max_context_tokens: int | None = None

    def environ(self) -> dict[str, str]:
        if not _MIN_AUTO_COMPACT_WINDOW <= self.auto_compact_window <= _MAX_AUTO_COMPACT_WINDOW:
            raise ProbeError(
                "probe auto-compaction window must be between 100000 and 1000000"
            )
        if not 1 <= self.auto_compact_percent <= 100:
            raise ProbeError("probe auto-compaction percentage must be 1..100")
        env = {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.auto_compact_window),
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(self.auto_compact_percent),
        }
        if self.max_context_tokens is not None:
            if self.max_context_tokens <= 0:
                raise ProbeError("probe max context tokens must be positive")
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(self.max_context_tokens)
        return env

    def settings_env(self) -> dict[str, str]:
        """The same validated policy, durable in flag settings."""

        return self.environ()


@dataclass(frozen=True)
class ProbeFixture:
    """Disposable probe filesystem domain; every path lives under ``root``."""

    root: Path
    home: Path
    project_dir: Path
    xdg_config_home: Path
    xdg_state_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    xdg_runtime_dir: Path
    claude_config_dir: Path
    # Private temp root (0700) for TMPDIR and CLAUDE_CODE_TMPDIR: the pinned
    # client otherwise writes tasks/*.output and friends under the uid-shared
    # /tmp/claude-<uid>/<cwd-slug>/ that live sessions also use.
    tmp_dir: Path

    def environ(
        self,
        *,
        base_url: str | None = None,
        compaction: ProbeCompactionPolicy | None = None,
        token: str | None = DUMMY_TOKEN,
        child_env: Mapping[str, str] | None = None,
        path_dirs: tuple[Path | str, ...] | list[Path | str] = (),
    ) -> dict[str, str]:
        """Disposable process environment; never derived from os.environ.

        With ``base_url`` the loopback fake provider is wired in using the
        fixed non-secret dummy token; without it no provider variable exists.
        ``token=None`` opts out of the env token (helper-only auth probes:
        ``ANTHROPIC_BASE_URL`` stays, ``ANTHROPIC_AUTH_TOKEN`` is absent); any
        other token must be a short fixed non-secret test id.
        ``compaction`` may add only the three typed context controls above.
        ``child_env`` may add only :data:`PROBE_ENV_ALLOWLIST` names with
        typed values (:func:`validate_probe_env`). ``path_dirs`` are
        validated fixture-internal private bin dirs prepended to the fixed
        ``/usr/bin:/bin`` (:func:`validate_fixture_path_dirs`).
        """

        _check_probe_token(token)
        overrides = validate_probe_env(child_env)
        prepended = validate_fixture_path_dirs(self, path_dirs)
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config_home),
            "XDG_STATE_HOME": str(self.xdg_state_home),
            "XDG_CACHE_HOME": str(self.xdg_cache_home),
            "XDG_DATA_HOME": str(self.xdg_data_home),
            "XDG_RUNTIME_DIR": str(self.xdg_runtime_dir),
            "CLAUDE_CONFIG_DIR": str(self.claude_config_dir),
            # Fixture-private temp root: nothing lands in /tmp/claude-<uid>.
            "TMPDIR": str(self.tmp_dir),
            "CLAUDE_CODE_TMPDIR": str(self.tmp_dir),
            # Explicit minimal PATH: no empty entry, never the ambient PATH;
            # only validated fixture-internal bin dirs may be prepended.
            "PATH": ":".join([*(str(path) for path in prepended), "/usr/bin", "/bin"]),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_UPDATES": "1",
        }
        if base_url is not None:
            env["ANTHROPIC_BASE_URL"] = check_loopback_url(base_url)
            if token is not None:
                env["ANTHROPIC_AUTH_TOKEN"] = token
        if compaction is not None:
            env.update(compaction.environ())
        env.update(overrides)
        return env


def build_fixture(
    root: Path | str, *, environ: Mapping[str, str] | None = None
) -> ProbeFixture:
    """Create and validate the disposable fixture domain (fail closed).

    An existing fixture is revalidated and reused; every directory must be
    owner-controlled mode 0700 and no path may be a symlink.
    """

    ambient = os.environ if environ is None else environ
    resolved = _resolve_fixture_root(root)
    _check_live_roots(resolved, ambient)
    _ensure_private(resolved, "fixture root")
    home = _ensure_private(resolved / "home", "disposable HOME")
    xdg = _ensure_private(resolved / "xdg", "disposable XDG root")
    runtime = _ensure_private(resolved / "runtime", "disposable runtime root")
    return ProbeFixture(
        root=resolved,
        home=home,
        project_dir=_ensure_private(home / "project", "disposable project directory"),
        xdg_config_home=_ensure_private(xdg / "config", "disposable XDG config"),
        xdg_state_home=_ensure_private(xdg / "state", "disposable XDG state"),
        xdg_cache_home=_ensure_private(xdg / "cache", "disposable XDG cache"),
        xdg_data_home=_ensure_private(xdg / "data", "disposable XDG data"),
        xdg_runtime_dir=runtime,
        claude_config_dir=_ensure_private(
            resolved / "claude-config", "disposable Claude config root"
        ),
        tmp_dir=_ensure_private(resolved / "tmp", "disposable temp root"),
    )


def fixture_manifest(fixture: ProbeFixture) -> dict[str, Any]:
    """Machine-readable fixture summary: paths only, never tokens."""

    return {
        "version": 1,
        "root": str(fixture.root),
        "dirs": {
            "home": str(fixture.home),
            "project_dir": str(fixture.project_dir),
            "xdg_config_home": str(fixture.xdg_config_home),
            "xdg_state_home": str(fixture.xdg_state_home),
            "xdg_cache_home": str(fixture.xdg_cache_home),
            "xdg_data_home": str(fixture.xdg_data_home),
            "xdg_runtime_dir": str(fixture.xdg_runtime_dir),
            "claude_config_dir": str(fixture.claude_config_dir),
            "tmp_dir": str(fixture.tmp_dir),
        },
    }


def _fixture_private_dir(fixture: ProbeFixture, path: Path | str, what: str) -> Path:
    """Validate one exact owner-private directory inside a built fixture."""

    root = _resolve_fixture_root(fixture.root)
    _ensure_private(root, "fixture root")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ProbeError(f"{what} {candidate} must be an absolute path")
    if ".." in candidate.parts:
        raise ProbeError(f"{what} {candidate} must not contain '..'")
    real = Path(os.path.realpath(candidate))
    if real != candidate:
        raise ProbeError(
            f"{what} {candidate} resolves through a symlink or non-normal "
            f"component to {real}"
        )
    if real == root or not _is_within(real, root):
        raise ProbeError(f"{what} {real} is not inside fixture root {root}")
    return _ensure_private(real, what)


def validate_fixture_path_dirs(
    fixture: ProbeFixture, dirs: tuple[Path | str, ...] | list[Path | str]
) -> tuple[Path, ...]:
    """Validate bin dirs a probe may prepend to the fixed child PATH.

    Each entry must be an existing, exact (absolute, no ``..``, no symlink
    component), owner-private directory of exactly mode 0700 strictly inside
    the fixture root, neither (inside) the fixture Claude config root nor
    (inside) the project directory, free of the ``:`` PATH separator and
    control characters, and listed once. Nothing is created: a missing
    directory is refused.
    """

    if isinstance(dirs, (str, bytes, Path)):
        raise ProbeError("probe PATH dirs must be a sequence of directories")
    entries = tuple(dirs)
    if len(entries) > _PATH_DIR_ENTRY_CAP:
        raise ProbeError("probe PATH dirs exceed the bounded entry count")
    validated: list[Path] = []
    for entry in entries:
        if not isinstance(entry, (str, Path)):
            raise ProbeError("probe PATH dir entries must be paths")
        text = str(entry)
        if not text or ":" in text or not text.isprintable():
            raise ProbeError(
                "probe PATH dir must be nonempty, printable, and free of ':'"
            )
        candidate = Path(text)
        if candidate.is_absolute() and ".." not in candidate.parts:
            if not os.path.lexists(candidate):
                raise ProbeError(f"probe PATH dir {candidate} does not exist")
        path = _fixture_private_dir(fixture, candidate, "probe PATH dir")
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        if mode != 0o700:
            raise ProbeError(
                f"probe PATH dir {path} must be exactly mode 0700 (is {mode:04o})"
            )
        reserved = {
            Path(os.path.realpath(fixture.claude_config_dir)): "the Claude config root",
            Path(os.path.realpath(fixture.project_dir)): "the project directory",
        }
        for reserved_path, label in reserved.items():
            if path == reserved_path or _is_within(path, reserved_path):
                raise ProbeError(
                    f"probe PATH dir {path} must not be (inside) {label}"
                )
        if path in validated:
            raise ProbeError(f"probe PATH dir {path} is listed twice")
        # PATH lookups must not escape the fixture through a symlinked entry.
        with os.scandir(path) as iterator:
            for count, item in enumerate(iterator, start=1):
                if count > _PATH_DIR_ENTRY_CAP:
                    raise ProbeError(
                        f"probe PATH dir {path} exceeds the bounded entry count"
                    )
                if item.is_symlink():
                    raise ProbeError(
                        f"probe PATH dir {path} contains a symlink entry "
                        f"{item.name!r}"
                    )
        validated.append(path)
    return tuple(validated)


def write_fixture_settings(
    fixture: ProbeFixture, relpath: str, document: dict[str, Any]
) -> Path:
    """Atomically write 0600 JSON strictly inside a disposable fixture.

    Refuse traversal and symlink components before creating any directory.
    This is a test harness writer, not a writer of live Claude settings.
    """

    root = _resolve_fixture_root(fixture.root)
    _ensure_private(root, "fixture root")
    relative = Path(relpath)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ProbeError("fixture settings path must be a nonempty relative path")
    target = root / relative
    if Path(os.path.realpath(target)) != target:
        raise ProbeError("fixture settings path resolves through a symlink")
    if not isinstance(document, dict):
        raise ProbeError("fixture settings must be a JSON object")
    data = strict_json.canonical_file_bytes(document)
    try:
        state.ensure_private_dir(target.parent)
        state.atomic_write(target, data)
    except (OSError, state.StateError) as exc:
        raise ProbeError(f"could not write fixture settings: {exc}") from exc
    return target


def write_fixture_executable(
    fixture: ProbeFixture, bin_dir: Path | str, name: str, body: bytes | str
) -> Path:
    """Write one owner-only (0700) executable into a fixture PATH dir.

    ``bin_dir`` is validated exactly like a PATH entry
    (:func:`validate_fixture_path_dirs`); ``name`` is a plain safe file
    name; the body is bounded. The write is atomic and symlink-refusing.
    Fake launchers (for example a recording ``claude-multi``) are test
    fixtures, never trusted artifacts.
    """

    (directory,) = validate_fixture_path_dirs(fixture, (bin_dir,))
    if not isinstance(name, str) or not _FIXTURE_EXECUTABLE_NAME_RE.fullmatch(name):
        raise ProbeError("fixture executable name must be a plain safe file name")
    data = body.encode("utf-8") if isinstance(body, str) else body
    if not isinstance(data, bytes) or not data:
        raise ProbeError("fixture executable body must be nonempty bytes or text")
    if len(data) > _FIXTURE_EXECUTABLE_MAX_BYTES:
        raise ProbeError("fixture executable body exceeds the bounded size")
    target = directory / name
    if os.path.lexists(target):
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode):
            raise ProbeError(f"fixture executable {target} is a symlink")
        # Only an existing owner-executable regular file may be replaced;
        # never a data file such as settings.json.
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
            raise ProbeError(
                f"fixture executable {target} would overwrite an existing "
                "non-executable file"
            )
    try:
        state.atomic_write(target, data)
        os.chmod(target, 0o700)
    except (OSError, state.StateError) as exc:
        raise ProbeError(
            f"could not write fixture executable {target}: {exc}"
        ) from exc
    return target


def seed_fixture_project_trust(
    fixture: ProbeFixture, project: Path | str
) -> Path:
    """Trust one exact disposable project in the fixture's native config.

    This is fixture onboarding only. It never trusts the live HOME or writes
    outside the validated private fixture root. Existing config
    is strictly parsed and merged so unrelated keys survive; malformed or
    unexpected shapes fail closed instead of being replaced.
    """

    project_dir = _fixture_private_dir(fixture, project, "fixture project")
    config_dir = _fixture_private_dir(
        fixture, fixture.claude_config_dir, "fixture Claude config root"
    )
    config_path = config_dir / ".claude.json"
    if os.path.lexists(config_path):
        real_config = Path(os.path.realpath(config_path))
        if real_config != config_path:
            raise ProbeError(
                f"fixture Claude config {config_path} is a symlink or non-normal path"
            )
        try:
            document = strict_json.loads(state.read_private(config_path))
        except (OSError, state.StateError, strict_json.StrictJSONError) as exc:
            raise ProbeError(
                f"fixture Claude config {config_path} is unsafe or malformed: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise ProbeError(
                f"fixture Claude config {config_path} must be a JSON object"
            )
    else:
        document = {}

    if "projects" not in document:
        projects = {}
    else:
        projects = document["projects"]
        if not isinstance(projects, dict):
            raise ProbeError(
                f"fixture Claude config {config_path} projects must be a JSON object"
            )
    key = str(project_dir)
    if key not in projects:
        existing = {}
    else:
        existing = projects[key]
        if not isinstance(existing, dict):
            raise ProbeError(
                f"fixture Claude config {config_path} project {key!r} must be a JSON object"
            )
    merged_projects = dict(projects)
    merged_projects[key] = {**existing, "hasTrustDialogAccepted": True}
    merged = {**document, "projects": merged_projects}
    try:
        state.atomic_write(config_path, strict_json.canonical_file_bytes(merged))
    except (OSError, state.StateError) as exc:
        raise ProbeError(
            f"could not seed fixture project trust in {config_path}: {exc}"
        ) from exc
    return config_path


def write_evidence(
    fixture: ProbeFixture, name: str, record: dict[str, Any]
) -> Path:
    """Persist a machine-readable probe artifact inside the private fixture."""

    evidence_dir = state.ensure_private_dir(fixture.root / "evidence")
    path = evidence_dir / f"{state.check_name(name)}.json"
    state.atomic_write(path, strict_json.canonical_file_bytes(record))
    return path


# ------------------------------------------------------------ fake provider


def _bounded_metadata_text(value: str, pattern: re.Pattern[str]) -> str:
    """Keep short safe identifiers; hash arbitrary request-controlled text."""

    data = value.encode("utf-8")
    if len(data) <= _METADATA_TEXT_MAX_BYTES and pattern.fullmatch(value):
        return value
    return f"sha256:{strict_json.sha256_hex(data)}:bytes={len(data)}"


def _bounded_request_path(value: str) -> str:
    """Retain only known credential-free endpoints; hash every other target."""

    parsed = urllib.parse.urlsplit(value)
    if not parsed.query and not parsed.fragment and parsed.path in _PUBLIC_METADATA_PATHS:
        return parsed.path
    data = value.encode("utf-8")
    return f"sha256:{strict_json.sha256_hex(data)}:bytes={len(data)}"


def _bounded_int(value: Any) -> int | None:
    """Plain non-negative integers only (bools and huge values dropped)."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 10**12:
        return None
    return value


def _effort_metadata(
    document: dict[str, Any],
) -> tuple[str | None, str | None, int | None]:
    """``output_config.effort`` plus the ``thinking`` type/budget shape."""

    effort: str | None = None
    output_config = document.get("output_config")
    if isinstance(output_config, dict) and isinstance(output_config.get("effort"), str):
        effort = _bounded_metadata_text(output_config["effort"], _METADATA_ID_RE)
    thinking_type: str | None = None
    budget: int | None = None
    thinking = document.get("thinking")
    if isinstance(thinking, dict):
        if isinstance(thinking.get("type"), str):
            thinking_type = _bounded_metadata_text(thinking["type"], _METADATA_ID_RE)
        budget = _bounded_int(thinking.get("budget_tokens"))
    return effort, thinking_type, budget


def _header_items(headers: Any) -> list[tuple[str, str]]:
    """All (name, value) pairs of an HTTPMessage or a plain mapping."""

    try:
        items = list(headers.items())
    except AttributeError:
        return []
    return [
        (name, value)
        for name, value in items
        if isinstance(name, str) and isinstance(value, str)
    ]


def _header_metadata(
    items: list[tuple[str, str]],
) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...], bool
]:
    """Header names (all, bounded) and x-claude-code-* hint metadata.

    Returns ``(all_names, hint_names, hint_values, hint_truncated)``. Header
    values are never retained except short safe-id values of the allowlisted
    hint headers; anything else is hashed or omitted.
    """

    names: list[str] = []
    for name, _value in items:
        lowered = _bounded_metadata_text(name.lower(), _METADATA_ID_RE)
        if lowered not in names:
            names.append(lowered)
    all_names = tuple(sorted(names)[:_HEADER_NAME_CAP])
    hints = sorted(
        {name for name in names if name.startswith(_HINT_HEADER_PREFIX)}
    )
    hint_names = tuple(hints[:_HINT_HEADER_CAP])
    values: list[tuple[str, str]] = []
    for name, value in items:
        lowered = name.lower()
        if lowered not in _HINT_VALUE_HEADERS or lowered not in hint_names:
            continue
        values.append((lowered, _bounded_metadata_text(value, _HINT_VALUE_RE)))
    return (
        all_names,
        hint_names,
        tuple(sorted(values)[:_HINT_HEADER_CAP]),
        len(hints) > len(hint_names),
    )


def _validate_known_tokens(known_tokens: Mapping[str, str] | None) -> dict[str, str]:
    """Fixed non-secret test tokens mapped to distinct safe labels."""

    if known_tokens is None:
        return {}
    if not isinstance(known_tokens, Mapping):
        raise ProbeError("known_tokens must map fixed test tokens to labels")
    if len(known_tokens) > _KNOWN_TOKEN_CAP:
        raise ProbeError("known_tokens exceeds the bounded entry count")
    validated: dict[str, str] = {}
    for token, label in known_tokens.items():
        _check_probe_token(token)
        if token == DUMMY_TOKEN:
            raise ProbeError("known_tokens must not relabel the fixed dummy token")
        if not isinstance(label, str) or not _AUTH_LABEL_RE.fullmatch(label):
            raise ProbeError("known_tokens labels must be short safe ids")
        if label in _RESERVED_AUTH_LABELS:
            raise ProbeError(
                f"known_tokens label {label!r} is reserved "
                "(dummy/other/absent)"
            )
        if label in validated.values():
            raise ProbeError(f"known_tokens label {label!r} is listed twice")
        validated[token] = label
    return validated


def _validate_content_markers(markers: Mapping[str, str] | None) -> dict[str, bytes]:
    """Marker name -> fixed marker bytes searched in request bodies."""

    if markers is None:
        return {}
    if not isinstance(markers, Mapping):
        raise ProbeError("content_markers must map marker names to marker strings")
    if len(markers) > _CONTENT_MARKER_CAP:
        raise ProbeError("content_markers exceeds the bounded entry count")
    validated: dict[str, bytes] = {}
    for name, marker in markers.items():
        if not isinstance(name, str) or not _AUTH_LABEL_RE.fullmatch(name):
            raise ProbeError("content marker names must be short safe ids")
        if not isinstance(marker, str) or not _CONTENT_MARKER_RE.fullmatch(marker):
            raise ProbeError(
                "content markers must be 8-128 chars of [A-Za-z0-9._:-] so "
                "JSON escaping cannot hide them"
            )
        validated[name] = marker.encode("ascii")
    return validated


@dataclass(frozen=True)
class RequestRecord:
    """Captured request metadata; prompt/transcript content is never stored."""

    method: str
    path: str
    model: str | None
    tool_names: tuple[str, ...]
    tool_count: int
    tool_names_truncated: bool
    has_system: bool
    system_json_bytes: int
    messages_json_bytes: int
    message_count: int
    message_sha256s: tuple[str, ...]
    message_hashes_truncated: bool
    auth: str  # "dummy" | "other" | "absent" | a known-token label
    body_sha256: str
    # Probe metadata; defaults keep older evidence and callers valid.
    max_tokens: int | None = None
    effort: str | None = None  # output_config.effort (bounded id)
    thinking_type: str | None = None  # thinking.type (bounded id)
    thinking_budget_tokens: int | None = None
    hint_header_names: tuple[str, ...] = ()  # x-claude-code-* names only
    hint_header_values: tuple[tuple[str, str], ...] = ()  # safe-id values
    hint_headers_truncated: bool = False
    markers_found: tuple[str, ...] = ()  # configured marker names, never text
    # 401/403 answered by the auth policy before the responder; 500 means
    # the policy raised or returned an invalid decision (failed closed).
    auth_rejected_status: int | None = None


@dataclass(frozen=True)
class RequestContext:
    """Per-request metadata handed to context-aware responders.

    Carries only what the recorded metadata already exposes — the bounded
    :class:`RequestRecord`, the 1-based request ordinal, and lower-cased
    header names (never header values). It lives in memory only.
    """

    ordinal: int
    header_names: tuple[str, ...]
    record: RequestRecord

    @property
    def auth(self) -> str:
        return self.record.auth


@dataclass(frozen=True)
class SseResponse:
    """Server-sent-events reply for clients that request ``stream: true``.

    Each event is an ``(event, data)`` pair serialized per the Anthropic
    streaming wire shape; payloads carry the same canned content as the
    JSON replies, never request content.
    """

    events: tuple[tuple[str, dict[str, Any]], ...]


def _default_responder(document: dict[str, Any], path: str) -> tuple[int, dict[str, Any]]:
    """Deterministic canned Anthropic-shaped reply.

    Echoes the requested model; emits a fixed ``tool_use`` block for the
    first (or forced) tool only when the client supplies tools with an
    ``any``/``tool`` tool_choice, else a fixed text block. This keeps
    registry/deny tool-use probes deterministic: the fake only ever echoes
    what the client actually sent.
    """

    model = document.get("model")
    tools = document.get("tools") or []
    tool_names = [
        tool["name"]
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    tool_choice = document.get("tool_choice")
    if tool_names and isinstance(tool_choice, dict) and tool_choice.get("type") in (
        "any",
        "tool",
    ):
        forced = tool_choice.get("name")
        name = forced if forced in tool_names else tool_names[0]
        content = [
            {"type": "tool_use", "id": "toolu_probe_0001", "name": name, "input": {}}
        ]
        stop_reason = "tool_use"
    else:
        content = [{"type": "text", "text": "PROBE-OK"}]
        stop_reason = "end_turn"
    return 200, {
        "id": "msg_probe_0001",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model if isinstance(model, str) else "probe-model",
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def reject_auth_labels(
    *labels: str, status: int = 401
) -> Callable[[RequestContext], int | None]:
    """Auth policy answering ``status`` for requests with these auth labels.

    Example: ``FakeAnthropicProvider(known_tokens={"tok-a": "A"},
    auth_policy=reject_auth_labels("A"))`` answers 401 to every request
    presenting ``tok-a`` before the responder runs. Labels are the
    recorded auth classifications ("dummy", "absent", "other", or a
    known-token label).
    """

    if status not in _AUTH_POLICY_STATUSES:
        raise ProbeError("auth policy status must be 401 or 403")
    if not labels:
        raise ProbeError("auth policy needs at least one auth label")
    for label in labels:
        if not isinstance(label, str) or not _AUTH_LABEL_RE.fullmatch(label):
            raise ProbeError("auth policy labels must be short safe ids")
    rejected = frozenset(labels)

    def policy(context: RequestContext) -> int | None:
        return status if context.auth in rejected else None

    return policy


class _QuietTeardownMixin:
    def handle_error(self, request: Any, client_address: Any) -> None:
        # A probed client SIGKILLed mid-request resets the connection; that
        # teardown noise is expected and never evidentiary.
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)  # type: ignore[misc]


class _ProviderServer(_QuietTeardownMixin, ThreadingHTTPServer):
    pass


_UNIX_PATH_DIRECT_MAX = 100  # sun_path is 108 bytes; stay well below it


def _unix_address(path: Path | str) -> tuple[str, int | None]:
    """A bindable/connectable address for a possibly long socket path.

    Paths near the ``sun_path`` limit go through an ``O_PATH`` descriptor of
    the parent directory (``/proc/self/fd/<n>/<name>``); the caller closes
    the returned descriptor once the bind/connect call returned.
    """

    text = str(path)
    if len(text.encode("utf-8")) < _UNIX_PATH_DIRECT_MAX:
        return text, None
    descriptor = os.open(
        os.path.dirname(text), os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
    )
    return f"/proc/self/fd/{descriptor}/{os.path.basename(text)}", descriptor


def unix_connect(path: Path | str, *, timeout: float | None = 30.0) -> Any:
    """Connect a stream socket to a filesystem unix socket (long-path safe)."""

    address, descriptor = _unix_address(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(address)
    except BaseException:
        client.close()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return client


class _UnixProviderServer(_QuietTeardownMixin, socketserver.ThreadingUnixStreamServer):
    """The fake provider's handler, served on a private filesystem socket.

    Path-based unix sockets cross network namespaces, so a relay inside a
    loopback-only namespace reaches the fake provider through this socket.
    The socket is created with mode 0600 (umask) inside an owner-private
    0700 fixture directory.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        address, descriptor = _unix_address(self.server_address)
        previous = os.umask(0o177)
        try:
            self.socket.bind(address)
        finally:
            os.umask(previous)
            if descriptor is not None:
                os.close(descriptor)


@dataclass(frozen=True)
class ProviderReply:
    """A scripted response with narrowly validated retry headers."""

    status: int
    payload: dict[str, Any] | SseResponse
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.headers, tuple):
            raise ProbeError("probe response retry headers must be a tuple")
        for pair in self.headers:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ProbeError("probe response retry header must be a name/value pair")
            name, value = pair
            if (
                not isinstance(name, str)
                or name not in {"retry-after", "retry-after-ms", "x-should-retry"}
                or not isinstance(value, str)
                or re.fullmatch(r"[0-9]{1,6}|true|false", value) is None
            ):
                raise ProbeError("probe response retry header is not allowlisted")


class _ProviderHandler(BaseHTTPRequestHandler):
    server_version = "claude-multi-probe/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def address_string(self) -> str:
        # Unix-socket peers have an empty client_address.
        address = self.client_address
        if isinstance(address, tuple) and address:
            return str(address[0])
        return "unix"

    def _send_json(
        self, status: int, payload: dict[str, Any], headers=()
    ) -> None:
        data = strict_json.canonical_file_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, status: int, payload: SseResponse, headers=()) -> None:
        chunks: list[bytes] = []
        for event, data in payload.events:
            chunks.append(f"event: {event}\n".encode("utf-8"))
            chunks.append(b"data: " + strict_json.canonical_bytes(data) + b"\n\n")
        body = b"".join(chunks)
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _error(self, status: int, kind: str, message: str) -> None:
        self._send_json(
            status, {"type": "error", "error": {"type": kind, "message": message}}
        )

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            self._send_json(
                200,
                {
                    "data": [
                        {"id": "gpt-multi-sol-high", "type": "model"},
                        {"id": "claude-multi-qwen38-max", "type": "model"},
                        {"id": "claude-multi-kimi-k3", "type": "model"},
                        {"id": "claude-fable-5", "type": "model"},
                    ],
                    "has_more": False,
                    "first_id": "gpt-multi-sol-high",
                    "last_id": "claude-fable-5",
                },
            )
            return
        self._error(404, "not_found_error", "probe: unknown path")

    def do_POST(self) -> None:
        provider: "FakeAnthropicProvider" = self.server.provider  # type: ignore[attr-defined]
        values = self.headers.get_all("Content-Length") or []
        if not values:
            self.close_connection = True
            self._error(411, "invalid_request_error", "probe: missing Content-Length")
            return
        if len(set(values)) != 1:
            self.close_connection = True
            self._error(
                400, "invalid_request_error", "probe: conflicting Content-Length"
            )
            return
        try:
            length = int(values[0])
        except ValueError:
            self.close_connection = True
            self._error(
                400, "invalid_request_error", "probe: non-numeric Content-Length"
            )
            return
        if length < 0:
            self.close_connection = True
            self._error(
                400, "invalid_request_error", "probe: negative Content-Length"
            )
            return
        if length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._error(
                413,
                "invalid_request_error",
                f"probe: body exceeds {_MAX_BODY_BYTES} bytes",
            )
            return
        body = self.rfile.read(length) if length else b""
        document: Any = None
        try:
            if body:
                document = strict_json.loads(body)
        except strict_json.StrictJSONError:
            document = None
        context = provider._record(
            self.command, self.path, self.headers, body, document
        )
        rejected = context.record.auth_rejected_status
        if rejected == 500:
            self._error(500, "api_error", "probe: invalid auth policy result")
            return
        if rejected is not None:
            # The auth policy answers before the responder ever runs.
            self._error(
                rejected,
                _AUTH_POLICY_STATUSES[rejected],
                "probe: credential rejected by the auth policy",
            )
            return
        if not isinstance(document, dict):
            self._error(
                400, "invalid_request_error", "probe: body is not a JSON object"
            )
            return
        reply = provider._respond(document, self.path, context)
        if isinstance(reply, ProviderReply):
            status, payload, headers = reply.status, reply.payload, reply.headers
        else:
            status, payload = reply
            headers = ()
        if isinstance(payload, SseResponse):
            self._send_sse(status, payload, headers)
        else:
            self._send_json(status, payload, headers)


class FakeAnthropicProvider:
    """Deterministic loopback-only fake Anthropic endpoint.

    Binds ``127.0.0.1`` on an ephemeral port, answers ``GET /healthz`` and
    ``POST`` JSON requests with canned deterministic responses, and records
    request metadata in memory only. Nothing is written to disk and no
    request body, header value, or credential is retained — only the
    metadata captured in :class:`RequestRecord`.

    Responders are either the legacy ``responder(document, path)`` callable
    or an object exposing ``respond(document, path, context)`` that also
    receives the per-request :class:`RequestContext`. ``known_tokens`` maps
    fixed non-secret test tokens to auth labels (the dummy token keeps the
    ``"dummy"`` label; unknown tokens stay ``"other"``). ``auth_policy``
    runs before the responder and may answer 401/403 for a request (see
    :func:`reject_auth_labels`). ``content_markers`` maps marker names to
    fixed marker strings; only the names of markers found in a request body
    are recorded, never the body.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        responder: Callable[
            [dict[str, Any], str], tuple[int, dict[str, Any] | SseResponse]
        ]
        | Any
        | None = None,
        known_tokens: Mapping[str, str] | None = None,
        auth_policy: Callable[[RequestContext], int | None] | None = None,
        content_markers: Mapping[str, str] | None = None,
    ):
        if host not in _LOOPBACK_HOSTS:
            raise ProbeError(f"fake provider host {host!r} is not a loopback literal")
        if auth_policy is not None and not callable(auth_policy):
            raise ProbeError("fake provider auth_policy must be callable")
        self._host = host
        self._responder = responder or _default_responder
        respond = getattr(self._responder, "respond", None)
        if not callable(respond) and not callable(self._responder):
            raise ProbeError(
                "fake provider responder must be callable(document, path) or "
                "expose respond(document, path, context)"
            )
        self._known_tokens = _validate_known_tokens(known_tokens)
        self._auth_policy = auth_policy
        self._content_markers = _validate_content_markers(content_markers)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Private unix listeners (same handler) keyed by socket path; they
        # bridge the provider into loopback-only network namespaces.
        self._unix_servers: dict[Path, tuple[_UnixProviderServer, threading.Thread]] = {}
        self._records: list[RequestRecord] = []
        self._dropped = 0
        self._ordinal = 0
        self._records_lock = threading.Lock()

    def start(self) -> "FakeAnthropicProvider":
        if self._server is not None:
            raise ProbeError("fake provider is already started")
        server = _ProviderServer((self._host, 0), _ProviderHandler)
        server.daemon_threads = True
        server.provider = self  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": _SERVE_POLL_INTERVAL},
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        for path, (server, thread) in list(self._unix_servers.items()):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self._unix_servers.clear()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise ProbeError("fake provider is not started")
        return int(self._server.server_address[1])

    def unix_socket(self, directory: Path | str) -> Path:
        """Also serve this provider on ``<directory>/provider-<port>.sock``.

        ``directory`` must be an existing owner-private 0700 directory (the
        caller validates it is inside the fixture). The listener lives until
        :meth:`stop`; a second call for the same directory reuses it. An
        existing non-socket entry at the path is refused, never replaced.
        """

        if self._server is None:
            raise ProbeError("fake provider is not started")
        base = Path(directory)
        try:
            info = os.lstat(base)
        except FileNotFoundError as exc:
            raise ProbeError(f"provider socket directory {base} does not exist") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            raise ProbeError(
                f"provider socket directory {base} is not an owner-private 0700 directory"
            )
        path = base / f"provider-{self.port}.sock"
        if path in self._unix_servers:
            return path
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise ProbeError(f"provider socket path {path} exists and is not a socket")
            os.unlink(path)  # a stale socket from an earlier provider on this port
        server = _UnixProviderServer(str(path), _ProviderHandler)
        server.provider = self  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": _SERVE_POLL_INTERVAL},
            daemon=True,
        )
        thread.start()
        self._unix_servers[path] = (server, thread)
        return path

    def __enter__(self) -> "FakeAnthropicProvider":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise ProbeError("fake provider is not started")
        host, port = self._server.server_address[:2]
        formatted = f"[{host}]" if ":" in host else host
        return f"http://{formatted}:{port}"

    @property
    def requests(self) -> tuple[RequestRecord, ...]:
        with self._records_lock:
            return tuple(self._records)

    @property
    def dropped_requests(self) -> int:
        """Records dropped after the bounded cap; spam stays evidentiary."""

        with self._records_lock:
            return self._dropped

    def _respond(
        self,
        document: dict[str, Any],
        path: str,
        context: RequestContext | None = None,
    ) -> ProviderReply | tuple[int, dict[str, Any] | SseResponse]:
        respond = getattr(self._responder, "respond", None)
        if callable(respond):
            if context is None:
                raise ProbeError("context-aware responder requires a request context")
            return respond(document, path, context)
        return self._responder(document, path)

    def _auth_label(self, headers: Any) -> str:
        presented = headers.get("x-api-key") or ""
        if not presented:
            bearer = headers.get("Authorization") or ""
            presented = bearer[7:] if bearer.startswith("Bearer ") else bearer
        if not presented:
            return "absent"
        if presented == DUMMY_TOKEN:
            return "dummy"
        return self._known_tokens.get(presented, "other")

    def _record(
        self, method: str, path: str, headers: Any, body: bytes, document: Any
    ) -> RequestContext:
        auth = self._auth_label(headers)
        header_names, hint_names, hint_values, hints_truncated = _header_metadata(
            _header_items(headers)
        )
        markers_found = tuple(
            sorted(
                name
                for name, marker in self._content_markers.items()
                if marker in body
            )
        )
        max_tokens: int | None = None
        effort: str | None = None
        thinking_type: str | None = None
        thinking_budget_tokens: int | None = None
        model: str | None = None
        tool_names: tuple[str, ...] = ()
        tool_count = 0
        tool_names_truncated = False
        has_system = False
        system_json_bytes = 0
        messages_json_bytes = 0
        message_count = 0
        message_sha256s: tuple[str, ...] = ()
        message_hashes_truncated = False
        if isinstance(document, dict):
            candidate = document.get("model")
            model = (
                _bounded_metadata_text(candidate, _METADATA_ID_RE)
                if isinstance(candidate, str)
                else None
            )
            max_tokens = _bounded_int(document.get("max_tokens"))
            effort, thinking_type, thinking_budget_tokens = _effort_metadata(
                document
            )
            tools = document.get("tools")
            if isinstance(tools, list):
                names = [
                    tool["name"]
                    for tool in tools
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                ]
                tool_count = len(names)
                tool_names = tuple(
                    _bounded_metadata_text(name, _METADATA_ID_RE)
                    for name in names[:_TOOL_NAME_CAP]
                )
                tool_names_truncated = len(names) > len(tool_names)
            has_system = "system" in document
            if has_system:
                system_json_bytes = len(strict_json.canonical_bytes(document["system"]))
            messages = document.get("messages")
            if isinstance(messages, list):
                messages_json_bytes = len(strict_json.canonical_bytes(messages))
                message_count = len(messages)
                hashed = messages[:_MESSAGE_HASH_CAP]
                message_sha256s = tuple(
                    strict_json.sha256_hex(strict_json.canonical_bytes(message))
                    for message in hashed
                )
                message_hashes_truncated = len(messages) > len(hashed)
        record = RequestRecord(
            method=method,
            path=_bounded_request_path(path),
            model=model,
            tool_names=tool_names,
            tool_count=tool_count,
            tool_names_truncated=tool_names_truncated,
            has_system=has_system,
            system_json_bytes=system_json_bytes,
            messages_json_bytes=messages_json_bytes,
            message_count=message_count,
            message_sha256s=message_sha256s,
            message_hashes_truncated=message_hashes_truncated,
            auth=auth,
            body_sha256=strict_json.sha256_hex(body),
            max_tokens=max_tokens,
            effort=effort,
            thinking_type=thinking_type,
            thinking_budget_tokens=thinking_budget_tokens,
            hint_header_names=hint_names,
            hint_header_values=hint_values,
            hint_headers_truncated=hints_truncated,
            markers_found=markers_found,
        )
        with self._records_lock:
            self._ordinal += 1
            ordinal = self._ordinal
        context = RequestContext(
            ordinal=ordinal, header_names=header_names, record=record
        )
        if self._auth_policy is not None:
            try:
                decision = self._auth_policy(context)
            except Exception:
                # A broken policy must never let the request through.
                decision = 500
            if decision is not None and decision not in _AUTH_POLICY_STATUSES:
                decision = 500
            if decision is not None:
                record = replace(record, auth_rejected_status=decision)
                context = replace(context, record=record)
        with self._records_lock:
            if len(self._records) >= _RECORD_CAP:
                self._dropped += 1
            else:
                self._records.append(record)
        return context


# ------------------------------------------------------- network isolation

# Host resolver endpoints reachable through the filesystem even from a
# loopback-only network namespace (unix sockets cross namespaces): masked
# with an empty tmpfs so name lookups fail instead of leaking DNS queries.
_RESOLVER_MASKS = (
    "/run/systemd/resolve",
    "/run/dbus",
    "/run/nscd",
    "/run/avahi-daemon",
)
# Other host control sockets a same-uid process could use to act outside the
# namespace: the user runtime dir (session bus, systemd
# user manager, ssh/gpg agents; the fixture brings its own XDG_RUNTIME_DIR)
# and container-engine sockets (docker group membership is root-equivalent).
# Directories get an empty tmpfs, single sockets a /dev/null bind.
_CONTROL_DIR_MASKS = ("/run/user/{uid}", "/run/podman", "/run/containerd")
_CONTROL_SOCKET_MASKS = ("/run/docker.sock", "/var/run/docker.sock")
_BRIDGE_CONNECTION_CAP = 128
_BRIDGE_CHUNK = 65536
_ISOLATION_CACHE: dict[str, str | None] = {}

# Runs inside the namespace as ``python3 -I -S -c <source> <spec> <exe> ...``:
# binds the bridge listeners, forks one relay process (stdio on /dev/null,
# parent-death SIGKILL, same process group), then execs the target with only
# the listed environment keys. Wrapper failures exit 125.
_NETNS_WRAPPER_SOURCE = r'''
import json, os, socket, sys, threading
def fail(message):
    os.write(2, ("claude-multi probe netns wrapper: " + message + "\n").encode())
    os._exit(125)
def unix_address(path):
    if len(path.encode()) < 100:
        return path, None
    fd = os.open(os.path.dirname(path), os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    return "/proc/self/fd/%d/%s" % (fd, os.path.basename(path)), fd
spec = json.loads(sys.argv[1])
target = sys.argv[2:]
if not target:
    fail("no target argv")
listeners = []
try:
    for port, path in spec["egress"]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(port)))
        sock.listen(64)
        listeners.append((sock, "egress", path, None))
    for path, port in spec["ingress"]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address, fd = unix_address(path)
        old = os.umask(0o177)
        try:
            sock.bind(address)
        finally:
            os.umask(old)
            if fd is not None:
                os.close(fd)
        sock.listen(64)
        listeners.append((sock, "ingress", path, int(port)))
except OSError as exc:
    fail("bridge bind failed: %s" % exc)
if listeners:
    parent = os.getpid()
    child = os.fork()
    if child == 0:
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(1, 9, 0, 0, 0)
        except Exception:
            pass
        if os.getppid() != parent:
            os._exit(0)
        null = os.open("/dev/null", os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        slots = threading.BoundedSemaphore(int(spec["cap"]))
        chunk = int(spec["chunk"])
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(chunk)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        def relay(conn, kind, path, port):
            try:
                if kind == "egress":
                    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    address, fd = unix_address(path)
                    try:
                        upstream.connect(address)
                    finally:
                        if fd is not None:
                            os.close(fd)
                else:
                    upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
                    upstream.settimeout(None)
            except OSError:
                conn.close()
                slots.release()
                return
            back = threading.Thread(target=pump, args=(upstream, conn), daemon=True)
            back.start()
            pump(conn, upstream)
            back.join()
            conn.close()
            upstream.close()
            slots.release()
        def serve(sock, kind, path, port):
            while True:
                try:
                    conn, _ = sock.accept()
                except OSError:
                    threading.Event().wait(0.05)
                    continue
                if not slots.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(target=relay, args=(conn, kind, path, port), daemon=True).start()
        for entry in listeners:
            threading.Thread(target=serve, args=entry, daemon=True).start()
        while os.getppid() == parent:
            threading.Event().wait(0.5)
        os._exit(0)
    for sock, _kind, _path, _port in listeners:
        sock.close()
identity = spec.get("identity")
if identity is not None:
    try:
        info = os.stat(target[0])
    except OSError as exc:
        fail("target vanished before exec: %s" % exc)
    if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] != identity:
        fail("target changed between verification and exec")
env = {key: os.environ[key] for key in spec["env_keys"] if key in os.environ}
try:
    os.execve(target[0], target, env)
except OSError as exc:
    fail("exec failed: %s" % exc)
'''

_ISOLATION_CHECK_SOURCE = r'''
import os, socket, sys
names = sorted(name for _index, name in socket.if_nameindex())
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
sock.close()
print("uid=%d ifaces=%s" % (os.geteuid(), ",".join(names)))
'''


_OVERFLOW_UID_PATH = "/proc/sys/kernel/overflowuid"


def _overflow_uid() -> int:
    try:
        return int(Path(_OVERFLOW_UID_PATH).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 65534


def _root_owned_path(path: Path) -> str | None:
    """None when ``path`` is a root-owned, non-group/other-writable file.

    A /nix/store file whose owner shows as the kernel overflow uid also
    passes: inside a user namespace (the Nix build sandbox) the real owner
    (root) is unmapped, so no process in the namespace can chmod or chown it.
    """

    try:
        info = os.lstat(path)
    except OSError as exc:
        return f"{path} is not inspectable ({exc})"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_uid != 0 and not (
        str(path).startswith("/nix/store/")
        and info.st_uid == _overflow_uid()
        and info.st_uid != os.geteuid()
    ):
        return f"{path} is not root-owned"
    if stat.S_IMODE(info.st_mode) & 0o022:
        return f"{path} is group/other-writable"
    if not os.access(path, os.X_OK):
        return f"{path} is not executable"
    if str(path).startswith("/nix/store/"):
        return None
    for parent in path.parents:
        try:
            meta = os.lstat(parent)
        except OSError as exc:
            return f"{parent} is not inspectable ({exc})"
        if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) & 0o022:
            return f"{parent} (a parent of {path}) is not a root-owned non-writable directory"
    return None


def _resolve_bwrap() -> Path:
    """Absolute, validated bubblewrap (root-owned or a /nix/store path)."""

    found = shutil.which("bwrap") or shutil.which(
        "bwrap", path="/usr/bin:/bin:/run/current-system/sw/bin"
    )
    if not found:
        raise ProbeError(
            "network isolation unavailable: bubblewrap (bwrap) is not on PATH"
        )
    real = Path(os.path.realpath(found))
    problem = _root_owned_path(real)
    if problem is not None:
        raise ProbeError(f"network isolation unavailable: untrusted bwrap: {problem}")
    return real


def _wrapper_python() -> Path:
    real = Path(os.path.realpath(sys.executable))
    if not real.is_absolute() or not real.is_file() or not os.access(real, os.X_OK):
        raise ProbeError(
            f"network isolation unavailable: interpreter {sys.executable!r} is unusable"
        )
    return real



@dataclass(frozen=True)
class HomeView:
    """ProtectHome=tmpfs model for offline probes (not a seccomp proof)."""
    home: Path
    bind_rw: tuple[Path, ...]
    bind_ro: tuple[Path, ...]
    bind_ro_optional: tuple[Path, ...] = ()

    def argv(self) -> list[str]:
        home = Path(self.home)
        if not home.is_absolute() or ".." in home.parts or home == Path("/"):
            raise ProbeError("home view needs an absolute non-root home")
        mounts = []
        seen = set()
        for flag, paths in (("--bind", self.bind_rw), ("--ro-bind", self.bind_ro),
                            ("--ro-bind-try", self.bind_ro_optional)):
            for item in paths:
                path = Path(item)
                if (not path.is_absolute() or ".." in path.parts or path == home
                        or not path.is_relative_to(home) or path in seen):
                    raise ProbeError("home view binds must be unique absolute paths below home")
                seen.add(path)
                # Source symlinks must not make a writable bind escape HOME.
                if flag == "--bind" and not path.resolve().is_relative_to(home.resolve()):
                    raise ProbeError("home view writable source escapes home")
                mounts.append((path, flag))
        result = ["--tmpfs", str(home)]
        for path, flag in sorted(mounts, key=lambda pair: (len(pair[0].parts), str(pair[0]))):
            result += [flag, str(path), str(path)]
        return [*result, "--remount-ro", str(home)]

def _isolation_prefix(
    bwrap: Path, cwd: Path | str | None, *, pid_namespace: bool = False
) -> list[str]:
    """The bwrap argv prefix: loopback-only netns, same uid, same pgid."""

    argv = [str(bwrap), "--unshare-net", "--die-with-parent", "--dev-bind", "/", "/"]
    if pid_namespace:
        argv += ["--unshare-pid", "--proc", "/proc"]
    seen: set[str] = set()
    uid = str(os.geteuid())
    for mask in (*_RESOLVER_MASKS, *(d.format(uid=uid) for d in _CONTROL_DIR_MASKS)):
        real = os.path.realpath(mask)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        argv += ["--tmpfs", real]
    for socket_path in _CONTROL_SOCKET_MASKS:
        real = os.path.realpath(socket_path)
        if real in seen or not os.path.exists(real) or os.path.isdir(real):
            continue
        seen.add(real)
        argv += ["--ro-bind", "/dev/null", real]
    if cwd is not None:
        argv += ["--chdir", str(cwd)]
    return argv


def network_isolation_available(*, refresh: bool = False) -> str | None:
    """None when real runs can be network-isolated, else the reason.

    Checks once per process (``refresh`` re-checks): a validated bwrap
    creates a network namespace whose only interface is ``lo``, loopback
    binds work, and the uid is unchanged. No packet leaves the namespace.
    """

    if not refresh and "result" in _ISOLATION_CACHE:
        return _ISOLATION_CACHE["result"]
    reason: str | None = None
    try:
        bwrap = _resolve_bwrap()
        python = _wrapper_python()
        completed = subprocess.run(
            [*_isolation_prefix(bwrap, None), "--", str(python), "-I", "-S", "-c",
             _ISOLATION_CHECK_SOURCE],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        expected = f"uid={os.geteuid()} ifaces=lo"
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout).strip()[-200:]
            reason = (
                "network isolation unavailable: bwrap could not create a "
                f"network namespace (exit {completed.returncode}: {tail})"
            )
        elif completed.stdout.strip() != expected:
            reason = (
                "network isolation unavailable: the namespace is not "
                f"loopback-only same-uid ({completed.stdout.strip()[:200]!r})"
            )
    except ProbeError as exc:
        reason = str(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        reason = f"network isolation unavailable: {exc}"
    _ISOLATION_CACHE["result"] = reason
    return reason


def _require_isolation() -> tuple[Path, Path]:
    reason = network_isolation_available()
    if reason is not None:
        raise ProbeError(f"refusing real native execution: {reason}")
    return _resolve_bwrap(), _wrapper_python()


@dataclass(frozen=True)
class PortBridge:
    """One TCP<->unix-socket bridge across the namespace boundary.

    ``egress``: the isolated process connects to ``127.0.0.1:port`` inside
    its namespace and reaches the outside filesystem socket
    ``socket_path`` (a fake upstream). ``ingress``: an outside client
    connects to ``socket_path`` (created by the bridge, mode 0600) and
    reaches ``127.0.0.1:port`` inside the namespace.
    """

    port: int
    socket_path: Path
    direction: str = "egress"


def _validate_bridges(bridges: tuple[PortBridge, ...] | list[PortBridge]) -> tuple[PortBridge, ...]:
    validated: list[PortBridge] = []
    seen_ports: set[tuple[str, int]] = set()
    if len(bridges) > 16:
        raise ProbeError("isolated process bridges exceed the bounded count")
    for bridge in bridges:
        if not isinstance(bridge, PortBridge):
            raise ProbeError("isolated process bridges must be PortBridge values")
        if bridge.direction not in ("egress", "ingress"):
            raise ProbeError("bridge direction must be 'egress' or 'ingress'")
        if isinstance(bridge.port, bool) or not isinstance(bridge.port, int) or not 1 <= bridge.port <= 65535:
            raise ProbeError("bridge port must be an integer in 1..65535")
        key = (bridge.direction, bridge.port)
        if key in seen_ports:
            raise ProbeError(f"bridge {bridge.direction} port {bridge.port} is listed twice")
        seen_ports.add(key)
        path = Path(bridge.socket_path)
        if not path.is_absolute() or ".." in path.parts:
            raise ProbeError(f"bridge socket {path} must be an exact absolute path")
        parent = path.parent
        try:
            info = os.lstat(parent)
        except FileNotFoundError as exc:
            raise ProbeError(f"bridge socket directory {parent} does not exist") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or Path(os.path.realpath(parent)) != parent
        ):
            raise ProbeError(
                f"bridge socket directory {parent} is not an exact owner-private 0700 directory"
            )
        if bridge.direction == "ingress" and os.path.lexists(path):
            raise ProbeError(f"ingress bridge socket {path} already exists")
        validated.append(PortBridge(bridge.port, path, bridge.direction))
    return tuple(validated)


def isolated_argv(
    command: list[str] | tuple[str, ...],
    *,
    env_keys: list[str] | tuple[str, ...],
    bridges: tuple[PortBridge, ...] | list[PortBridge] = (),
    cwd: Path | str | None = None,
    identity: tuple[int, int, int, int] | None = None,
    home_view: HomeView | None = None,
    pid_namespace: bool = False,
) -> list[str]:
    """The exact argv that runs ``command`` in a loopback-only namespace.

    Shape: ``bwrap --unshare-net --die-with-parent --dev-bind / /
    [--tmpfs <resolver dir>]... [--chdir <cwd>] -- <python> -I -S -c
    <wrapper> <spec-json> <command...>``. Fails closed (ProbeError) when
    isolation is unavailable.
    """

    bwrap, python = _require_isolation()
    checked = _validate_bridges(bridges)
    spec = {
        "egress": [[b.port, str(b.socket_path)] for b in checked if b.direction == "egress"],
        "ingress": [[str(b.socket_path), b.port] for b in checked if b.direction == "ingress"],
        "env_keys": sorted(env_keys),
        "identity": list(identity) if identity is not None else None,
        "cap": _BRIDGE_CONNECTION_CAP,
        "chunk": _BRIDGE_CHUNK,
    }
    return [
        *_isolation_prefix(bwrap, cwd, pid_namespace=pid_namespace),
        *(home_view.argv() if home_view is not None else []),
        "--",
        str(python),
        "-I",
        "-S",
        "-c",
        _NETNS_WRAPPER_SOURCE,
        json.dumps(spec, sort_keys=True, separators=(",", ":")),
        *[str(part) for part in command],
    ]


def start_isolated_process(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path | str,
    env: Mapping[str, str],
    bridges: tuple[PortBridge, ...] | list[PortBridge] = (),
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    home_view: HomeView | None = None,
) -> subprocess.Popen:
    """Start a trusted-by-path program in its own loopback-only namespace.

    ``argv[0]`` must be an absolute, non-symlink, regular executable that
    is not group/other-writable (for example a ``/nix/store`` gateway). The
    process gets exactly ``env``, runs in a new session/process group
    (:func:`stop_isolated_process` SIGKILLs the whole group), and bwrap's
    ``--die-with-parent`` kills it when the calling thread's process dies
    (so a SIGTERM to the test run cannot orphan it). Call this from the
    thread that outlives the process (parent-death is per thread).
    """

    if not argv:
        raise ProbeError("isolated process argv must not be empty")
    program = Path(str(argv[0]))
    if not program.is_absolute() or Path(os.path.realpath(program)) != program:
        raise ProbeError(f"isolated program {program} must be an exact absolute non-symlink path")
    try:
        info = os.lstat(program)
    except FileNotFoundError as exc:
        raise ProbeError(f"isolated program {program} does not exist") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
        raise ProbeError(f"isolated program {program} must be a non-writable regular file")
    if not os.access(program, os.X_OK):
        raise ProbeError(f"isolated program {program} is not executable")
    if not isinstance(env, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ProbeError("isolated process env must map strings to strings")
    command = isolated_argv(
        [str(part) for part in argv],
        env_keys=list(env),
        bridges=bridges,
        cwd=cwd,
        identity=(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
        home_view=home_view,
    )
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
    )


def stop_isolated_process(process: subprocess.Popen, *, timeout: float = 10.0) -> None:
    """SIGKILL the isolated process group and reap it (idempotent)."""

    if process.poll() is None:
        _kill_process_group(process)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


class UnixHTTPServer(_QuietTeardownMixin, socketserver.ThreadingUnixStreamServer):
    """Threaded HTTP handler server on a private filesystem socket (0600).

    For fake upstreams that an isolated process reaches through an egress
    :class:`PortBridge`. ``BaseHTTPRequestHandler`` subclasses must not rely
    on ``client_address`` (it is empty for unix peers).
    """

    daemon_threads = True

    def server_bind(self) -> None:
        _UnixProviderServer.server_bind(self)  # type: ignore[arg-type]


class UnixHTTPConnection(http.client.HTTPConnection):
    """``http.client.HTTPConnection`` over a filesystem unix socket."""

    def __init__(self, path: Path | str, *, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = Path(path)

    def connect(self) -> None:
        self.sock = unix_connect(self._unix_path, timeout=self.timeout)


def _provider_bridge(
    fixture: "ProbeFixture", provider: "FakeAnthropicProvider"
) -> PortBridge:
    """Egress bridge: in-namespace ``127.0.0.1:<port>`` -> provider socket."""

    run_dir = _ensure_private(fixture.root / "run", "fixture bridge socket directory")
    run_dir = _fixture_private_dir(fixture, run_dir, "fixture bridge socket directory")
    return PortBridge(provider.port, provider.unix_socket(run_dir), "egress")


# -------------------------------------------------------------- native run


@dataclass(frozen=True)
class TrustedExecutable:
    """Pinned executable identity: exact resolved path and full SHA-256.

    ``fake=True`` marks a unit-test fake executable; anything else is a
    real native artifact and runs only under the narrow ``allow_real``
    allowance (fixture-root config dir, loopback fake provider, live-domain
    snapshot tripwire). ``owned_root`` is claude-multi's owned-copies root
    when the executable is the owned copy found there
    (:func:`trusted_from_contract`): every run holds that version's use
    lock, whatever environment the run itself uses.
    """

    resolved_path: Path
    sha256: str
    fake: bool = False
    owned_root: Path | None = None


@dataclass(frozen=True)
class PinnedBinary:
    """Operator-host real-binary gate outcome for probe test classes."""

    trusted: TrustedExecutable | None
    contract: dict[str, Any] | None
    boundary: str | None  # a "BOUNDARY: ..." skip message, or None

    @property
    def version(self) -> str:
        from . import pin

        try:
            return pin.version(self.contract or {})
        except (KeyError, TypeError, ValueError):
            return "unknown"


def pinned_binary_gate(contract_path: Path | str, *, what: str,
                       environ: Mapping[str, str] | None = None) -> PinnedBinary:
    """Shared setUpClass gate for real-binary probe classes (fail closed).

    Returns a BOUNDARY skip message (never raises) when the contract is
    missing, unreadable, malformed, or lacks a usable executable; when the
    pinned binary is missing or drifted from its sha256; or when network
    isolation is unavailable (:func:`network_isolation_available`).
    """

    path = Path(contract_path)

    def boundary(message: str) -> PinnedBinary:
        return PinnedBinary(None, None, f"BOUNDARY: {message}")

    if not path.is_file():
        return boundary(
            f"catalog/native-contract.json unavailable; {what} run only on the "
            "operator host with the pinned Claude installation"
        )
    isolation = network_isolation_available()
    if isolation is not None:
        return boundary(
            f"{isolation}; {what} run only inside a loopback-only network namespace"
        )
    try:
        contract = strict_json.loads(path.read_bytes())
    except (OSError, strict_json.StrictJSONError) as exc:
        return boundary(
            f"native-contract.json is unreadable or malformed ({exc}); {what} "
            "require the pinned contract"
        )
    if not isinstance(contract, dict):
        return boundary(
            f"native-contract.json is not a JSON object; {what} require the "
            "pinned contract"
        )
    try:
        trusted = trusted_from_contract(contract, environ)
    except ProbeError as exc:
        return boundary(
            f"native contract lacks a usable pinned executable ({exc}); {what} skipped"
        )
    binary = trusted.resolved_path
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return boundary(
            f"pinned Claude binary {binary} unavailable; {what} run only on the "
            "operator host"
        )
    if _sha256_file(binary) != trusted.sha256:
        return boundary(
            f"pinned Claude binary {binary} drifted from the native-contract "
            "sha256; re-pin before running real probes"
        )
    return PinnedBinary(trusted, contract, None)


# The candidate file a re-pin verified, handed to its evidence suite so the
# real-binary probes find a build outside the standard locations (still
# accepted only when its size and sha256 match the contract under test).
REPIN_CANDIDATE_ENV = "CLAUDE_MULTI_REPIN_CANDIDATE"


def trusted_from_contract(
    native_contract: dict[str, Any], environ: Mapping[str, str] | None = None,
) -> TrustedExecutable:
    """The trusted spec of the pin: a local file matching its sha256 and size.

    The contract is path-free, so the file is found where acquisition looks:
    the candidate a re-pin hands its evidence suite
    (:data:`REPIN_CANDIDATE_ENV`, set only by ``claude-multi-dev repin``),
    then the native versions directories of ``environ`` (a file named after
    its version, beside the other installed versions the probes compare
    with), then the retained copy and claude-multi's owned copy. The
    ``claude`` on PATH is never consulted. Only a file whose size and full
    sha256 match this platform's record qualifies. The owned copy carries
    its owned root, so its runs lock it there (:func:`run_native`).
    """

    from . import acquire, errors as errors_mod, pin

    env = os.environ if environ is None else environ
    try:
        version = pin.version(native_contract)
        platform = pin.host_platform()
        record = pin.platform_record(native_contract, platform)
    except (KeyError, TypeError, ValueError, errors_mod.ClaudeMultiError) as exc:
        raise ProbeError(f"native contract lacks a pinned Claude Code identity: {exc}") from exc
    if record is None:
        raise ProbeError(f"native contract pins no {platform} build of Claude Code {version}")
    digest = record.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ProbeError("native contract sha256 must be 64 lowercase hex digits")
    local = acquire.local_candidates(version, platform, {**env, "PATH": ""})
    handed = env.get(REPIN_CANDIDATE_ENV)
    found = [*([Path(handed)] if handed and os.path.isabs(handed) else []),
             *(item.path for item in local if item.kind == "native"),
             *(item.path for item in local if item.kind == "retained"),
             pin.owned_path(env, version, platform)]
    for path in found:
        if os.path.lexists(path) and acquire.matches(path, record):
            resolved = Path(os.path.realpath(path))
            owned = pin.owned_root(env) if pin.owned_version_of(resolved, env) is not None else None
            return TrustedExecutable(resolved, digest, fake=False, owned_root=owned)
    raise ProbeError(
        f"no local file matches Claude Code {version} ({platform}); set it up with "
        f"`{pin.SETUP_COMMAND}`"
    )


@dataclass(frozen=True)
class NativeRunResult:
    """Bounded outcome of one gated native run inside the fixture."""

    argv: tuple[str, ...]
    returncode: int | None  # None when the timeout killed the child
    timed_out: bool
    stdout: str
    stderr: str
    requests: tuple[RequestRecord, ...]
    daemon: DaemonDomainObservation | None = None


@dataclass(frozen=True)
class PTYInteraction:
    """Wait for one terminal marker, then send one bounded byte sequence.

    By default, terminal output already buffered after the marker is discarded
    for matching purposes so a repeated UI glyph cannot satisfy the next
    interaction. ``preserve_after_wait`` is for explicit two-stage barriers
    where the following marker may arrive in the same PTY read.

    ``before_send`` is an optional zero-argument callback run after the
    marker matched and before ``send`` is written (for example editing a
    fixture agent file before typing ``/reload-plugins``). It runs in the
    harness process, must finish within ``callback_timeout`` seconds (and
    the overall PTY deadline), and any exception or overrun fails the run
    closed with the live-domain tripwire still enforced. ``send`` may be
    empty for a callback-only step.
    """

    wait_for: bytes
    send: bytes
    preserve_after_wait: bool = False
    before_send: Callable[[], None] | None = None
    callback_timeout: float = 30.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_trusted_executable(trusted: TrustedExecutable) -> Path:
    """Verify the artifact exactly matches the pinned spec (fail closed).

    Identity is the exact absolute non-symlink path plus the full content
    SHA-256; group/other-writable artifacts are refused. Owner identity is
    deliberately not required: a root-owned immutable artifact at the exact
    pinned path with the pinned hash is acceptable, and an arbitrary user
    script is rejected unless a spec pins its exact path and hash.
    """

    path = trusted.resolved_path
    if not isinstance(trusted.sha256, str) or not _SHA256_RE.fullmatch(
        trusted.sha256
    ):
        raise ProbeError("trusted executable spec sha256 must be 64 lowercase hex")
    if not path.is_absolute():
        raise ProbeError(f"probe executable {path} must be an absolute path")
    real = Path(os.path.realpath(path))
    if real != path:
        raise ProbeError(
            f"probe executable {path} must not contain non-normal components "
            f"or resolve through a symlink"
        )
    try:
        info = os.lstat(real)
    except FileNotFoundError as exc:
        raise ProbeError(f"probe executable {real} does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProbeError(f"probe executable {real} is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ProbeError(
            f"probe executable {real} must not be group/other-writable"
        )
    if not os.access(real, os.X_OK):
        raise ProbeError(f"probe executable {real} is not executable")
    if _sha256_file(real) != trusted.sha256:
        raise ProbeError(
            f"probe executable {real} content hash does not match the trusted "
            "executable spec"
        )
    return real


def _hold_owned_copy(trusted: TrustedExecutable, environ: Mapping[str, str]) -> Any:
    """The shared use lock of claude-multi's owned copy, or None when
    ``trusted`` names any other file.

    The owned root is the one recorded where the copy was found
    (:attr:`TrustedExecutable.owned_root`), so a run in a fixture
    environment still locks it; else the owned copy of ``environ`` counts.
    Taken before the hash check; a run hands the descriptor to the client
    (``pass_fds``, through the isolation wrapper's exec), so pruning keeps
    the copy while the client and its children run. Raises
    :class:`ProbeError` when the version is gone or its lock cannot be
    taken safely."""

    from . import pin

    real = Path(os.path.realpath(trusted.resolved_path))
    if trusted.owned_root is not None:
        root = Path(trusted.owned_root)
        if Path(os.path.realpath(root)) != real.parent.parent:
            raise ProbeError(f"probe executable {real} is not an owned copy under {root}")
    elif pin.owned_version_of(real, environ) is not None:
        root = pin.owned_root(environ)
    else:
        return None
    try:
        return pin.take_use_lock_in(root, real.parent.name, shared=True)
    except FileNotFoundError as exc:
        raise ProbeError(f"probe executable {real} does not exist") from exc
    except (OSError, errors.ClaudeMultiError) as exc:
        raise ProbeError(f"the use lock of probe executable {real} cannot be taken: {exc}") from exc


def _kept_fds(hold: Any) -> tuple[int, ...]:
    """The descriptors a native run hands to its client (the use lock)."""

    descriptor = None if hold is None else hold.descriptor
    return () if descriptor is None else (descriptor,)


_MAX_CALLBACK_TIMEOUT = 300.0


def _env_options(
    fixture: ProbeFixture,
    token: str | None,
    child_env: Mapping[str, str] | None,
    path_dirs: tuple[Path | str, ...] | list[Path | str],
) -> dict[str, Any]:
    """Pre-validate child-env options before any hashing or provider start."""

    _check_probe_token(token)
    validated_env = validate_probe_env(child_env)
    validated_dirs = validate_fixture_path_dirs(fixture, path_dirs)
    return {
        "token": token,
        "child_env": validated_env,
        "path_dirs": validated_dirs,
    }


def _run_bounded_callback(
    callback: Callable[[], None], *, timeout: float, deadline: float
) -> None:
    """Run one PTY step callback with a bounded wall-clock budget.

    The callback runs on a daemon thread so an overrun cannot wedge the
    harness; an exception or overrun raises ProbeError (fail closed).
    """

    budget = min(timeout, deadline - time.monotonic())
    if budget <= 0:
        raise ProbeError("probe PTY timed out before a step callback")
    failure: list[BaseException] = []

    def target() -> None:
        try:
            callback()
        except BaseException as exc:  # reported to the harness thread
            failure.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        raise ProbeError(
            f"probe PTY step callback exceeded its {budget:.1f}s budget"
        )
    if failure:
        raise ProbeError(
            f"probe PTY step callback failed: {type(failure[0]).__name__}: "
            f"{failure[0]}"
        ) from failure[0]


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _native_command(
    executable: Path,
    args: list[str] | tuple[str, ...],
    *,
    real: bool,
    fixture: "ProbeFixture",
    provider: "FakeAnthropicProvider",
    env: Mapping[str, str],
    cwd: Path,
    pid_namespace: bool = False,
) -> tuple[tuple[str, ...], list[str]]:
    """(reported argv, spawned argv) for one native run.

    A real spec always runs through :func:`isolated_argv` (loopback-only
    namespace, provider bridged over the fixture socket, identity re-check
    right before exec); fake unit specs run directly.
    """

    command = (str(executable), *[str(argument) for argument in args])
    if not real:
        return command, list(command)
    info = os.stat(executable)
    spawned = isolated_argv(
        list(command),
        env_keys=list(env),
        bridges=(_provider_bridge(fixture, provider),),
        cwd=cwd,
        identity=(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
        pid_namespace=pid_namespace,
    )
    return command, spawned


def _assert_real_run_allowed(
    fixture: ProbeFixture, environ: Mapping[str, str]
) -> None:
    """Narrow allowance for the pinned binary (fail closed).

    The real spec may run only when the fixture's CLAUDE_CONFIG_DIR lives
    inside the disposable fixture root — the per-config-root daemon domain
    the static binary evidence implies — and the ambient environment carries
    no provider credentials. The trusted path+sha256 check, the loopback
    fake-provider check, and the live-domain pre/post snapshot tripwire run
    alongside in ``run_native``.
    """

    assert_no_provider_credentials(environ)
    root = Path(os.path.realpath(fixture.root))
    _check_live_roots(root, environ)
    config = Path(os.path.realpath(fixture.claude_config_dir))
    if config == root or not _is_within(config, root):
        raise ProbeError(
            "refusing real native execution: CLAUDE_CONFIG_DIR "
            f"{config} is not inside the disposable fixture root {root}"
        )
    try:
        info = os.lstat(config)
    except FileNotFoundError as exc:
        raise ProbeError(
            f"refusing real native execution: CLAUDE_CONFIG_DIR {config} "
            "does not exist"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise ProbeError(
            f"refusing real native execution: CLAUDE_CONFIG_DIR {config} "
            "is not an owner-private directory"
        )
    # Network hermeticity is part of the allowance: no namespace, no run.
    _require_isolation()


def run_native(
    args: list[str] | tuple[str, ...],
    *,
    trusted: TrustedExecutable,
    fixture: ProbeFixture,
    provider: FakeAnthropicProvider | None = None,
    timeout: float = 60.0,
    allow_real: bool = False,
    environ: Mapping[str, str] | None = None,
    live_daemon_domain: Path | None = None,
    compaction: ProbeCompactionPolicy | None = None,
    token: str | None = DUMMY_TOKEN,
    child_env: Mapping[str, str] | None = None,
    path_dirs: tuple[Path | str, ...] | list[Path | str] = (),
) -> NativeRunResult:
    """Run the trusted executable once inside the disposable fixture.

    Without ``allow_real`` a real (``fake=False``) spec is refused before any
    hashing or provider startup. With ``allow_real`` the pinned binary may
    run under the narrow allowance: CLAUDE_CONFIG_DIR inside the disposable
    fixture root, the trusted path+sha256 check, the loopback fake provider,
    and a clean ambient provider-credential scan (``environ`` is scan-only;
    the child never inherits it). The live uid-shared daemon domain is
    snapshotted immediately before and after the run and must be
    byte-identical — any new, removed, or changed entry fails closed with
    "live daemon domain touched". Fake-executable unit probes are verified
    against the pinned spec (exact path, full SHA-256, non-writable) before
    anything starts. The child receives only the fixture environment
    (loopback fake provider with the dummy token, zero real credentials, no
    ambient variables), runs in a new session/process group with the exact
    private project subdirectory as CWD and a null stdin. ``compaction`` is a
    typed narrow override for only the context-capacity/percentage controls.
    ``token``, ``child_env`` and ``path_dirs`` pass through to
    :meth:`ProbeFixture.environ` (token opt-out, allowlisted typed env,
    validated fixture bin dirs) and are validated before anything starts.
    The whole process group is SIGKILLed and reaped on timeout or error. A
    caller-supplied provider keeps its lifecycle; an internally created one is
    stopped deterministically. claude-multi's own copy runs under its use
    lock (:func:`_hold_owned_copy`), taken before the hash check and
    inherited by the client.
    """

    if timeout <= 0:
        raise ProbeError("probe run timeout must be positive")
    project_dir = _fixture_private_dir(
        fixture, fixture.project_dir, "fixture native working directory"
    )
    env_options = _env_options(fixture, token, child_env, path_dirs)
    real = not trusted.fake
    if real and not allow_real:
        # Refuse real specs before any hashing or provider startup.
        raise ProbeError(_REAL_RUN_REFUSAL)
    ambient = os.environ if environ is None else environ
    if real:
        _assert_real_run_allowed(fixture, ambient)
    hold = _hold_owned_copy(trusted, ambient)
    owned = provider is None
    try:
        executable = _verify_trusted_executable(trusted)
        if owned:
            provider = FakeAnthropicProvider().start()
        assert provider is not None
        if real:
            if not isinstance(provider, FakeAnthropicProvider):
                raise ProbeError(
                    "refusing real native execution: the provider must be the "
                    "loopback fake"
                )
            check_loopback_url(provider.base_url)
            live = (
                live_daemon_domain
                if live_daemon_domain is not None
                else Path(f"/tmp/cc-daemon-{os.geteuid()}")
            )
            domain_before = snapshot_daemon_domain(live)
            siblings_before = fixture_daemon_domains(live)
        else:
            domain_before = None
            siblings_before = ()
        env = fixture.environ(
            base_url=provider.base_url, compaction=compaction, **env_options
        )
        command, spawned = _native_command(
            executable,
            args,
            real=real,
            fixture=fixture,
            provider=provider,
            env=env,
            cwd=project_dir,
        )
        process = subprocess.Popen(
            spawned,
            cwd=project_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=_kept_fds(hold),
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode: int | None = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
            stdout, stderr = process.communicate()
            returncode = None
        except BaseException:
            _kill_process_group(process)
            process.wait()
            raise
        daemon: DaemonDomainObservation | None = None
        if domain_before is not None:
            domain_after = snapshot_daemon_domain(live)
            siblings_after = fixture_daemon_domains(live)
            # Hard failure on any change: the run touched the live domain.
            daemon = _enforce_live_domain_untouched(
                domain_before,
                domain_after,
                fixture=fixture,
                fixture_domains_before=siblings_before,
                fixture_domains_after=siblings_after,
            )
        return NativeRunResult(
            argv=command,
            returncode=returncode,
            timed_out=timed_out,
            stdout=stdout[-4000:],
            stderr=stderr[-4000:],
            requests=provider.requests,
            daemon=daemon,
        )
    finally:
        if owned and provider is not None:
            provider.stop()
        if hold is not None:
            hold.release()


def run_native_pty(
    args: list[str] | tuple[str, ...],
    interactions: list[PTYInteraction] | tuple[PTYInteraction, ...],
    *,
    trusted: TrustedExecutable,
    fixture: ProbeFixture,
    provider: FakeAnthropicProvider | None = None,
    timeout: float = 120.0,
    allow_real: bool = False,
    environ: Mapping[str, str] | None = None,
    live_daemon_domain: Path | None = None,
    compaction: ProbeCompactionPolicy | None = None,
    rows: int = 32,
    columns: int = 120,
    pid_namespace: bool = False,
    token: str | None = DUMMY_TOKEN,
    child_env: Mapping[str, str] | None = None,
    path_dirs: tuple[Path | str, ...] | list[Path | str] = (),
) -> NativeRunResult:
    """Drive a trusted native client through a bounded disposable PTY script.

    Interactions may carry a ``before_send`` callback (see
    :class:`PTYInteraction`); ``token``, ``child_env``, ``path_dirs`` and
    the owned copy's use lock behave as in :func:`run_native`. Relaunch
    probes simply call this again with the same fixture and ``--resume``
    argv.
    """

    if timeout <= 0:
        raise ProbeError("probe PTY timeout must be positive")
    project_dir = _fixture_private_dir(
        fixture, fixture.project_dir, "fixture native working directory"
    )
    if rows <= 0 or columns <= 0:
        raise ProbeError("probe PTY dimensions must be positive")
    if len(interactions) > 256:
        raise ProbeError("probe PTY script exceeds the bounded step count")
    for interaction in interactions:
        if not isinstance(interaction, PTYInteraction):
            raise ProbeError("probe PTY steps must be PTYInteraction values")
        if not interaction.wait_for:
            raise ProbeError("probe PTY wait marker must not be empty")
        if len(interaction.wait_for) > 4096 or len(interaction.send) > 65536:
            raise ProbeError("probe PTY interaction exceeds bounded size")
        if interaction.before_send is not None:
            if not callable(interaction.before_send):
                raise ProbeError("probe PTY before_send must be callable")
            if not 0 < interaction.callback_timeout <= _MAX_CALLBACK_TIMEOUT:
                raise ProbeError(
                    "probe PTY callback timeout must be within (0, 300] seconds"
                )
    env_options = _env_options(fixture, token, child_env, path_dirs)
    real = not trusted.fake
    if real and not allow_real:
        raise ProbeError(_REAL_RUN_REFUSAL)
    ambient = os.environ if environ is None else environ
    if real:
        _assert_real_run_allowed(fixture, ambient)
    hold = _hold_owned_copy(trusted, ambient)
    owned = provider is None
    master: int | None = None
    slave: int | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        executable = _verify_trusted_executable(trusted)
        if owned:
            provider = FakeAnthropicProvider().start()
        assert provider is not None
        if real:
            if not isinstance(provider, FakeAnthropicProvider):
                raise ProbeError(
                    "refusing real native execution: the provider must be the "
                    "loopback fake"
                )
            check_loopback_url(provider.base_url)
            live = (
                live_daemon_domain
                if live_daemon_domain is not None
                else Path(f"/tmp/cc-daemon-{os.geteuid()}")
            )
            domain_before = snapshot_daemon_domain(live)
            siblings_before = fixture_daemon_domains(live)
        else:
            domain_before = None
            siblings_before = ()
        env = fixture.environ(
            base_url=provider.base_url, compaction=compaction, **env_options
        )
        command, spawned = _native_command(
            executable,
            args,
            real=real,
            fixture=fixture,
            provider=provider,
            env=env,
            cwd=project_dir,
            pid_namespace=pid_namespace,
        )
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )
        process = subprocess.Popen(
            spawned,
            cwd=project_dir,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            pass_fds=_kept_fds(hold),
            process_group=0,
        )
        os.close(slave)
        slave = None
        output = bytearray()
        cursor = 0
        started = time.monotonic()
        deadline = started + timeout
        completed_interactions = 0

        def timed_out(phase: str) -> PTYTimeoutError:
            return PTYTimeoutError(
                phase, completed_interactions, len(interactions),
                time.monotonic() - started,
            )

        def read_more(
            until: bytes | None, *, preserve_after_wait: bool = False
        ) -> None:
            nonlocal cursor
            while until is None or until not in output[cursor:]:
                if process is None or process.poll() is not None:
                    if until is not None and until not in output[cursor:]:
                        raise ProbeError(
                            f"probe PTY exited before marker {until!r}; output="
                            f"{bytes(output[-4000:])!r}"
                        )
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timed_out("client-exit" if until is None else "marker")
                ready, _, _ = select.select([master], [], [], min(0.2, remaining))
                if not ready:
                    if until is None:
                        return
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        return
                    raise
                if not chunk:
                    return
                output.extend(chunk)
                if len(output) > 2 * 1024 * 1024:
                    dropped = len(output) - 2 * 1024 * 1024
                    del output[:dropped]
                    cursor = max(0, cursor - dropped)
            if until is not None:
                match = output.find(until, cursor)
                cursor = (
                    match + len(until) if preserve_after_wait else len(output)
                )

        try:
            for interaction in interactions:
                read_more(
                    interaction.wait_for,
                    preserve_after_wait=interaction.preserve_after_wait,
                )
                if interaction.before_send is not None:
                    _run_bounded_callback(
                        interaction.before_send,
                        timeout=interaction.callback_timeout,
                        deadline=deadline,
                    )
                pending = memoryview(interaction.send)
                while pending:
                    written = os.write(master, pending)
                    if written <= 0:
                        raise ProbeError("probe PTY write made no progress")
                    pending = pending[written:]
                completed_interactions += 1
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise timed_out("client-exit")
                read_more(None)
            while True:
                ready, _, _ = select.select([master], [], [], 0)
                if not ready:
                    break
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
        except BaseException:
            if process.poll() is None:
                _kill_process_group(process)
                process.wait()
            if domain_before is not None:
                domain_after = snapshot_daemon_domain(live)
                siblings_after = fixture_daemon_domains(live)
                _enforce_live_domain_untouched(
                    domain_before,
                    domain_after,
                    fixture=fixture,
                    fixture_domains_before=siblings_before,
                    fixture_domains_after=siblings_after,
                )
            raise
        daemon: DaemonDomainObservation | None = None
        if domain_before is not None:
            domain_after = snapshot_daemon_domain(live)
            siblings_after = fixture_daemon_domains(live)
            daemon = _enforce_live_domain_untouched(
                domain_before,
                domain_after,
                fixture=fixture,
                fixture_domains_before=siblings_before,
                fixture_domains_after=siblings_after,
            )
        rendered = bytes(output[-16000:]).decode("utf-8", errors="replace")
        return NativeRunResult(
            argv=command,
            returncode=process.returncode,
            timed_out=False,
            stdout=rendered,
            stderr="",
            requests=provider.requests,
            daemon=daemon,
        )
    finally:
        if process is not None and process.poll() is None:
            _kill_process_group(process)
            process.wait()
        if master is not None:
            os.close(master)
        if slave is not None:
            os.close(slave)
        if owned and provider is not None:
            provider.stop()
        if hold is not None:
            hold.release()


# ------------------------------------------------------ scripted delegation


_SUBAGENT_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_AGENT_TOOL_NAMES = ("Agent", "Task")
_UNKNOWN_TYPE_RE = re.compile(
    r"unknown|unavailable|not (?:found|available|registered|supported)|"
    r"no such|unregistered",
    re.IGNORECASE,
)


def _message_payload(
    content: list[dict[str, Any]],
    *,
    model: Any,
    stop_reason: str,
    message_id: str,
    input_tokens: int = 1,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model if isinstance(model, str) else "probe-model",
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "output_tokens": 1,
        },
    }


def _sse_message(message: dict[str, Any]) -> SseResponse:
    """Wrap a canned message payload in the Anthropic SSE wire shape."""

    start_message = {
        **message,
        "content": [],
        "usage": {**message["usage"], "output_tokens": 0},
    }
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {"type": "message_start", "message": start_message},
        )
    ]
    for index, block in enumerate(message["content"]):
        if block.get("type") == "text":
            start_block: dict[str, Any] = {"type": "text", "text": ""}
            delta: dict[str, Any] = {"type": "text_delta", "text": block["text"]}
        elif block.get("type") == "tool_use":
            start_block = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            }
            delta = {
                "type": "input_json_delta",
                "partial_json": strict_json.canonical_bytes(
                    block.get("input", {})
                ).decode("utf-8"),
            }
        else:
            continue
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": start_block,
                },
            )
        )
        events.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            )
        )
        events.append(
            ("content_block_stop", {"type": "content_block_stop", "index": index})
        )
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message["stop_reason"],
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 1},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return SseResponse(tuple(events))


class CompactionResponder:
    """Route-aware fake responses that drive one near-limit retained turn.

    The first Messages reply reports ``near_limit_tokens`` and later replies
    report one token. Count-token calls report the near-limit value only while
    exactly one Messages request has completed. Only counters are retained;
    request content is never stored or inspected.
    """

    def __init__(self, near_limit_tokens: int, *, first_response_chars: int = 0):
        if near_limit_tokens <= 0:
            raise ProbeError("compaction responder token count must be positive")
        if first_response_chars < 0 or first_response_chars > _MAX_BODY_BYTES // 2:
            raise ProbeError("compaction responder first response size is unsafe")
        self.near_limit_tokens = near_limit_tokens
        self.first_response_chars = first_response_chars
        self._message_requests = 0
        self._count_token_requests = 0
        self._lock = threading.Lock()

    @property
    def message_requests(self) -> int:
        with self._lock:
            return self._message_requests

    @property
    def count_token_requests(self) -> int:
        with self._lock:
            return self._count_token_requests

    def __call__(
        self, document: dict[str, Any], path: str
    ) -> tuple[int, dict[str, Any] | SseResponse]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        with self._lock:
            if route.endswith("/count_tokens"):
                self._count_token_requests += 1
                tokens = (
                    self.near_limit_tokens
                    if self._message_requests == 1
                    else 1
                )
                return 200, {"input_tokens": tokens}
            if not route.endswith("/messages"):
                return 404, {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": "probe: unknown path",
                    },
                }
            first = self._message_requests == 0
            self._message_requests += 1
            filler = ""
            if first and self.first_response_chars:
                parts: list[str] = []
                size = 0
                index = 0
                while size < self.first_response_chars:
                    part = f" probe-token-{index:06d}"
                    parts.append(part)
                    size += len(part)
                    index += 1
                filler = "".join(parts)[: self.first_response_chars]
            text = "PROBE-OK" + filler
            payload = _message_payload(
                [{"type": "text", "text": text}],
                model=document.get("model"),
                stop_reason="end_turn",
                message_id=f"msg_probe_compaction_{self._message_requests:04d}",
                input_tokens=self.near_limit_tokens if first else 1,
            )
            return 200, _sse_message(payload) if document.get("stream") else payload


class AutomaticCompactionResponder:
    """Main-turn script driving deterministic automatic compaction.

    Claude Code also issues small auxiliary model calls with no Agent tool.
    Those receive a separate low-usage response and do not advance the script.
    Main turns one and two seed compactable groups; main turn three reports
    150K cache-aware usage; main turn four reports 165K, above the deterministic
    162K reactive threshold for a 200K capacity; and main turn five exercises
    the post-threshold session. The lifecycle hook, not auxiliary-request
    classification, is the proof of automatic compaction.
    """

    def __init__(self) -> None:
        self._message_requests = 0
        self._main_requests = 0
        self._auxiliary_requests = 0
        self._count_token_requests = 0
        self._lock = threading.Lock()

    @property
    def message_requests(self) -> int:
        with self._lock:
            return self._message_requests

    @property
    def main_requests(self) -> int:
        with self._lock:
            return self._main_requests

    @property
    def auxiliary_requests(self) -> int:
        with self._lock:
            return self._auxiliary_requests

    @property
    def count_token_requests(self) -> int:
        with self._lock:
            return self._count_token_requests

    @staticmethod
    def _is_main_request(document: dict[str, Any]) -> bool:
        tools = document.get("tools")
        if not isinstance(tools, list):
            return False
        return any(
            isinstance(tool, dict) and tool.get("name") in _AGENT_TOOL_NAMES
            for tool in tools
        )

    def __call__(
        self, document: dict[str, Any], path: str
    ) -> tuple[int, dict[str, Any] | SseResponse]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        with self._lock:
            if route.endswith("/count_tokens"):
                self._count_token_requests += 1
                return 200, {"input_tokens": 1}
            if not route.endswith("/messages"):
                return 404, {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": "probe: unknown path",
                    },
                }
            self._message_requests += 1
            if self._is_main_request(document):
                self._main_requests += 1
                kind = "main"
                ordinal = self._main_requests
            else:
                self._auxiliary_requests += 1
                kind = "auxiliary"
                ordinal = self._auxiliary_requests

        text = "PROBE-OK" if kind == "main" else "PROBE-AUX"
        input_tokens = 1
        cache_read_input_tokens = 0
        if kind == "main" and ordinal == 1:
            input_tokens = 500
        elif kind == "main" and ordinal == 2:
            input_tokens = 1000
        elif kind == "main" and ordinal == 3:
            input_tokens = 2000
            cache_read_input_tokens = 147999
        elif kind == "main" and ordinal == 4:
            input_tokens = 2000
            cache_read_input_tokens = 162999

        payload = _message_payload(
            [{"type": "text", "text": text}],
            model=document.get("model"),
            stop_reason="end_turn",
            message_id=f"msg_probe_automatic_{kind}_{ordinal:04d}",
            input_tokens=input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        if document.get("stream"):
            return 200, _sse_message(payload)
        return 200, payload


class ScriptedDelegationResponder:
    """Provider script: force one Agent delegation, classify follow-up.

    The first request that offers an Agent/Task tool gets a canned
    ``tool_use`` reply naming ``subagent_type``; every later request is
    classified in memory (never persisted):

    - ``second-request-stream``: a fresh stream with no tool_result for the
      forced call — the spawn attempt is observable -> accepted;
    - ``tool_result-success``: the forced call came back without an
      unknown-type error -> accepted;
    - ``tool_result-unknown-type``: the forced call came back with an error
      naming an unknown/unavailable type -> refused;
    - ``tool_result-other-error``: an error not naming the type ->
      indeterminate;
    - ``agent-tool-absent``: the client never offered an Agent/Task tool ->
      indeterminate;
    - ``no-followup``: the client ended after the forced call ->
      indeterminate.

    With ``hold`` set, the forced reply waits for the event (bounded by
    ``hold_timeout``) so a caller can keep the session alive while it
    manipulates the fixture (takeover probe).
    """

    def __init__(
        self,
        subagent_type: str,
        *,
        hold: threading.Event | None = None,
        hold_timeout: float = 120.0,
        delegated_input_limit_bytes: int | None = None,
    ):
        if not _SUBAGENT_TYPE_RE.fullmatch(subagent_type or ""):
            raise ProbeError(
                "scripted delegation subagent_type must be a safe agent id"
            )
        if delegated_input_limit_bytes is not None and delegated_input_limit_bytes <= 0:
            raise ProbeError("delegated input limit must be positive")
        self.subagent_type = subagent_type
        self.tool_use_id = "toolu_probe_delegation_0001"
        self._prompt = "Reply with exactly: CLAUDE-MULTI-PROBE-DELEGATION-OK"
        self._hold = hold
        self._hold_timeout = hold_timeout
        self._delegated_input_limit_bytes = delegated_input_limit_bytes
        self._classification = "indeterminate"
        self._branch = "no-followup"
        self._forced = False
        self._lock = threading.Lock()

    @property
    def classification(self) -> str:
        with self._lock:
            return self._classification

    @property
    def branch(self) -> str:
        with self._lock:
            return self._branch

    def _classify(self, branch: str, classification: str) -> None:
        # First decisive signal wins; later requests keep the verdict.
        if self._classification == "indeterminate":
            self._classification = classification
            self._branch = branch

    def _tool_results(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        messages = document.get("messages")
        if not isinstance(messages, list):
            return results
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == self.tool_use_id
                ):
                    results.append(block)
        return results

    @staticmethod
    def _result_text(block: dict[str, Any]) -> str:
        content = block.get("content")
        if isinstance(content, str):
            return content
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                ):
                    parts.append(item["text"])
        return "\n".join(parts)

    @staticmethod
    def _request_input_bytes(document: dict[str, Any]) -> int:
        total = 0
        if "system" in document:
            total += len(strict_json.canonical_bytes(document["system"]))
        messages = document.get("messages")
        if isinstance(messages, list):
            total += len(strict_json.canonical_bytes(messages))
        return total

    def __call__(
        self, document: dict[str, Any], path: str
    ) -> tuple[int, dict[str, Any] | SseResponse]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        if not route.endswith("/messages"):
            if route.endswith("/count_tokens"):
                return 200, {"input_tokens": 1}
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        with self._lock:
            return self._respond_locked(document)

    def _respond_locked(
        self, document: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | SseResponse]:
        model = document.get("model")
        stream = bool(document.get("stream"))
        if not self._forced:
            tools = document.get("tools") or []
            names = {
                tool.get("name") for tool in tools if isinstance(tool, dict)
            }
            tool = next(
                (name for name in _AGENT_TOOL_NAMES if name in names), None
            )
            if tool is None:
                self._branch = "agent-tool-absent"
                payload = _message_payload(
                    [{"type": "text", "text": "PROBE-OK"}],
                    model=model,
                    stop_reason="end_turn",
                    message_id="msg_probe_delegation_plain",
                )
                return 200, _sse_message(payload) if stream else payload
            if self._hold is not None:
                # Bounded: a caller that never releases still completes.
                self._hold.wait(timeout=self._hold_timeout)
            self._forced = True
            payload = _message_payload(
                [
                    {
                        "type": "tool_use",
                        "id": self.tool_use_id,
                        "name": tool,
                        "input": {
                            "description": "claude-multi probe delegation",
                            "subagent_type": self.subagent_type,
                            "prompt": self._prompt,
                        },
                    }
                ],
                model=model,
                stop_reason="tool_use",
                message_id="msg_probe_delegation_force",
            )
            return 200, _sse_message(payload) if stream else payload
        results = self._tool_results(document)
        if (
            not results
            and self._delegated_input_limit_bytes is not None
            and self._request_input_bytes(document)
            > self._delegated_input_limit_bytes
        ):
            self._branch = "delegated-model-context-overflow"
            return 400, {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "probe: delegated model context window exceeded",
                },
            }
        if not results:
            if self._delegated_input_limit_bytes is None:
                self._classify("second-request-stream", "accepted")
        else:
            block = results[0]
            if block.get("is_error"):
                if _UNKNOWN_TYPE_RE.search(self._result_text(block)):
                    self._classify("tool_result-unknown-type", "refused")
                else:
                    self._classify("tool_result-other-error", "indeterminate")
            else:
                self._classify("tool_result-success", "accepted")
        payload = _message_payload(
            [{"type": "text", "text": "PROBE-OK"}],
            model=model,
            stop_reason="end_turn",
            message_id="msg_probe_delegation_followup",
        )
        return 200, _sse_message(payload) if stream else payload


@dataclass(frozen=True)
class DelegationResult:
    """Metadata-only outcome of one scripted delegation probe."""

    subagent_type: str
    classification: str  # "accepted" | "refused" | "indeterminate"
    branch: str
    scope_dir: str
    returncode: int | None
    timed_out: bool
    requests: tuple[RequestRecord, ...]
    daemon: DaemonDomainObservation | None
    evidence: Path | None


def _run_metadata(result: NativeRunResult) -> dict[str, Any]:
    stdout_bytes = result.stdout.encode("utf-8")
    stderr_bytes = result.stderr.encode("utf-8")
    return {
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "argv_count": len(result.argv),
        "argv_flags": [
            token for token in result.argv[1:] if token.startswith("-")
        ],
        "stdout_bytes": len(stdout_bytes),
        "stdout_sha256": strict_json.sha256_hex(stdout_bytes),
        "stderr_bytes": len(stderr_bytes),
        "stderr_sha256": strict_json.sha256_hex(stderr_bytes),
    }


def run_scripted_delegation(
    subagent_type: str,
    *,
    scope_dir: Path | str,
    trusted: TrustedExecutable,
    fixture: ProbeFixture,
    environ: Mapping[str, str] | None = None,
    timeout: float = 120.0,
    hold: threading.Event | None = None,
    evidence_name: str | None = None,
    live_daemon_domain: Path | None = None,
    delegated_input_limit_bytes: int | None = None,
) -> DelegationResult:
    """Run one scripted delegation against the pinned binary.

    Launches the trusted executable headlessly against the loopback fake
    provider with ``--add-dir <scope_dir>``, forces an
    ``Agent(subagent_type=<name>)`` call on the first turn, and classifies
    the client's follow-up (accepted / refused / indeterminate). The run is
    gated by the narrow real-binary allowance with the live-domain snapshot
    tripwire. Evidence is metadata-only: branch labels, request metadata,
    flag names, hashes — never prompt text, transcripts, full argv, stdio
    text, or response bodies.
    """

    if trusted.fake:
        raise ProbeError("scripted delegation requires the real pinned spec")
    ambient = os.environ if environ is None else environ
    root = Path(os.path.realpath(fixture.root))
    scope = Path(os.path.realpath(scope_dir))
    if scope == root or not _is_within(scope, root):
        raise ProbeError(
            f"delegation scope {scope} is not inside the disposable fixture "
            f"root {root}"
        )
    scope = _fixture_private_dir(fixture, scope, "delegation scope")
    seed_fixture_project_trust(fixture, fixture.project_dir)
    # The external --add-dir is an independently exact workspace boundary;
    # trust it too so agent discovery reaches the requested frontmatter model.
    seed_fixture_project_trust(fixture, scope)
    turn = (
        "Use the Agent tool exactly once with subagent_type "
        f"{subagent_type} and report its reply."
    )
    responder = ScriptedDelegationResponder(
        subagent_type,
        hold=hold,
        delegated_input_limit_bytes=delegated_input_limit_bytes,
    )
    provider = FakeAnthropicProvider(responder=responder).start()
    try:
        argv = (
            "-p",
            turn,
            "--add-dir",
            str(scope),
            "--dangerously-skip-permissions",
        )
        try:
            outcome = run_native(
                argv,
                trusted=trusted,
                fixture=fixture,
                provider=provider,
                timeout=timeout,
                allow_real=True,
                environ=ambient,
                live_daemon_domain=live_daemon_domain,
            )
        except ProbeError as exc:
            if evidence_name is not None:
                write_evidence(
                    fixture,
                    evidence_name,
                    {
                        "version": 1,
                        "kind": "scripted-delegation",
                        "subagent_type": subagent_type,
                        "scope_dir": str(scope),
                        "turn_sha256": strict_json.sha256_hex(
                            turn.encode("utf-8")
                        ),
                        "classification": responder.classification,
                        "branch": responder.branch,
                        "requests": [
                            asdict(record) for record in provider.requests
                        ],
                        "error": str(exc),
                    },
                )
            raise
        result = DelegationResult(
            subagent_type=subagent_type,
            classification=responder.classification,
            branch=responder.branch,
            scope_dir=str(scope),
            returncode=outcome.returncode,
            timed_out=outcome.timed_out,
            requests=outcome.requests,
            daemon=outcome.daemon,
            evidence=None,
        )
        if evidence_name is not None:
            path = write_evidence(
                fixture,
                evidence_name,
                {
                    "version": 1,
                    "kind": "scripted-delegation",
                    "subagent_type": subagent_type,
                    "scope_dir": str(scope),
                    "turn_sha256": strict_json.sha256_hex(turn.encode("utf-8")),
                    "classification": result.classification,
                    "branch": result.branch,
                    "run": _run_metadata(outcome),
                    "requests": [asdict(record) for record in outcome.requests],
                    "daemon": (
                        asdict(outcome.daemon)
                        if outcome.daemon is not None
                        else None
                    ),
                },
            )
            result = replace(result, evidence=path)
        return result
    finally:
        provider.stop()


# ------------------------------------------------------------ takeover probe


def _default_versions_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The native Claude Code versions directory of this HOME (read only)."""

    from . import acquire

    return acquire.native_versions_dirs(os.environ if environ is None else environ)[0]


@dataclass(frozen=True)
class RetainedBinary:
    """One hash-verified retained pinned binary."""

    version: str
    path: Path
    sha256: str
    pinned_in_contract: bool


def _retained_record(binary: RetainedBinary) -> dict[str, Any]:
    """JSON-safe metadata record of one retained binary."""

    return {
        "version": binary.version,
        "path": str(binary.path),
        "sha256": binary.sha256,
        "pinned_in_contract": binary.pinned_in_contract,
    }


def _contract_hash_for(
    contract: dict[str, Any], version: str, path: Path
) -> str | None:
    """Pinned hash for a retained version, if the contract records one."""

    from . import errors as errors_mod, pin

    verified = contract.get("verified") if isinstance(contract, dict) else None
    if not isinstance(verified, list):
        return None
    try:
        platform = pin.host_platform()
    except errors_mod.ClaudeMultiError:
        return None
    for entry in verified:
        if not isinstance(entry, dict) or entry.get("version") != version:
            continue
        platforms = entry.get("platforms")
        record = platforms.get(platform) if isinstance(platforms, dict) else None
        digest = record.get("sha256") if isinstance(record, dict) else None
        if isinstance(digest, str):
            return digest
    return None


def _retained_binary(
    version: str, *, versions_dir: Path, contract: dict[str, Any]
) -> RetainedBinary:
    """Locate and hash-verify one retained binary (fail closed on mismatch).

    A version the contract does not pin is hash-verified for integrity only;
    the computed hash is recorded in evidence and the probe proceeds.
    """

    path = versions_dir / version
    real = Path(os.path.realpath(path))
    if real != path:
        raise ProbeError(f"retained binary {path} resolves through a symlink")
    try:
        info = os.lstat(real)
    except FileNotFoundError as exc:
        raise ProbeError(f"retained binary {real} does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProbeError(f"retained binary {real} is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ProbeError(f"retained binary {real} must not be group/other-writable")
    if not os.access(real, os.X_OK):
        raise ProbeError(f"retained binary {real} is not executable")
    digest = _sha256_file(real)
    pinned = _contract_hash_for(contract, version, real)
    if pinned is not None and pinned != digest:
        raise ProbeError(
            f"retained binary {version} content hash does not match the "
            "native contract"
        )
    return RetainedBinary(version, real, digest, pinned is not None)


@dataclass(frozen=True)
class TakeoverProbeResult:
    """Metadata-only outcome of the fixture takeover probe."""

    verdict: str  # "takeover-delegation-accepted" | "takeover-delegation-refused" | "takeover-delegation-indeterminate"
    old_binary: RetainedBinary
    new_binary: RetainedBinary
    supervisor_artifacts: tuple[str, ...]
    takeover_markers: tuple[str, ...]
    delegation: DelegationResult | None
    daemon: DaemonDomainObservation | None
    evidence: Path | None


def _daemon_artifacts(config_dir: Path) -> tuple[str, ...]:
    """Names under the fixture config root's own daemon dir (fixture-local)."""

    daemon = config_dir / "daemon"
    try:
        return tuple(sorted(entry.name for entry in os.scandir(daemon)))
    except (FileNotFoundError, NotADirectoryError):
        return ()


def run_takeover_probe(
    subagent_type: str,
    *,
    scope_dir: Path | str,
    fixture: ProbeFixture,
    contract: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    versions_dir: Path | None = None,
    old_version: str = "2.1.216",
    new_version: str = "2.1.217",
    supervisor_timeout: float = 20.0,
    takeover_timeout: float = 45.0,
    session_timeout: float = 180.0,
    evidence_name: str = "takeover-probe",
    live_daemon_domain: Path | None = None,
) -> TakeoverProbeResult:
    """Fixture supervisor takeover must keep the scope.

    Launches a fixture session through a fixture-owned ``claude`` symlink
    targeting ``old_version`` (both retained binaries hash-verified; an
    unpinned old version's computed hash is recorded and the probe
    proceeds), holds the scripted first turn, waits bounded seconds for the
    fixture's own supervisor to appear under the fixture config root, swaps
    the symlink to ``new_version``, waits bounded seconds for takeover
    evidence in the fixture-root daemon artifacts, then releases the turn
    and asserts scripted delegation to ``subagent_type`` still succeeds.

    Every precondition fails closed with a recorded verdict: a fixture that
    runs no resident supervisor headlessly, a still uid-shared domain, or
    takeover never observed all raise ProbeError after writing evidence —
    the takeover then requires manual verification.
    """

    ambient = os.environ if environ is None else environ
    _assert_real_run_allowed(fixture, ambient)
    if versions_dir is None:
        versions_dir = _default_versions_dir(environ)
    root = Path(os.path.realpath(fixture.root))
    scope = Path(os.path.realpath(scope_dir))
    if scope == root or not _is_within(scope, root):
        raise ProbeError(
            f"takeover scope {scope} is not inside the disposable fixture "
            f"root {root}"
        )
    old_binary = _retained_binary(
        old_version, versions_dir=versions_dir, contract=contract
    )
    new_binary = _retained_binary(
        new_version, versions_dir=versions_dir, contract=contract
    )

    def _fail(message: str, **extra: Any) -> None:
        record: dict[str, Any] = {
            "version": 1,
            "kind": "takeover-probe",
            "subagent_type": subagent_type,
            "scope_dir": str(scope),
            "old_binary": _retained_record(old_binary),
            "new_binary": _retained_record(new_binary),
            "error": message,
        }
        record.update(extra)
        write_evidence(fixture, evidence_name, record)
        raise ProbeError(message)

    bin_dir = state.ensure_private_dir(fixture.root / "bin")
    link = bin_dir / "claude"
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    if link.exists() or link.is_symlink():
        raise ProbeError(f"fixture claude link {link} could not be replaced")
    os.symlink(old_binary.path, link)

    hold = threading.Event()
    responder = ScriptedDelegationResponder(
        subagent_type, hold=hold, hold_timeout=session_timeout
    )
    provider = FakeAnthropicProvider(responder=responder).start()
    live = (
        live_daemon_domain
        if live_daemon_domain is not None
        else Path(f"/tmp/cc-daemon-{os.geteuid()}")
    )
    domain_before = snapshot_daemon_domain(live)
    siblings_before = fixture_daemon_domains(live)
    turn = (
        "Use the Agent tool exactly once with subagent_type "
        f"{subagent_type} and report its reply."
    )
    argv = (
        str(link),
        "-p",
        turn,
        "--add-dir",
        str(scope),
        "--dangerously-skip-permissions",
    )
    takeover_env = fixture.environ(base_url=provider.base_url)
    # Hermetic like run_native: loopback-only namespace, provider bridged
    # over the fixture socket. No exec-time identity pin: the probe swaps
    # the fixture symlink by design; both targets were hash-verified above.
    spawned = isolated_argv(
        list(argv),
        env_keys=list(takeover_env),
        bridges=(_provider_bridge(fixture, provider),),
        cwd=fixture.home,
    )
    process = subprocess.Popen(
        spawned,
        cwd=fixture.home,
        env=takeover_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        # Precondition: the fixture must run its own resident supervisor,
        # observable via fixture-root daemon artifacts, without touching the
        # live uid-shared domain.
        deadline = time.monotonic() + supervisor_timeout
        artifacts: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            artifacts = _daemon_artifacts(fixture.claude_config_dir)
            if artifacts:
                break
            interim = snapshot_daemon_domain(live_daemon_domain)
            if _domain_changes(domain_before, interim):
                _fail(
                    "takeover probe precondition unmet: the fixture session "
                    "touched the live uid-shared daemon domain "
                    "(domain still uid-shared); the takeover check moves to user "
                    "acceptance",
                    live_domain_changes=list(
                        _domain_changes(domain_before, interim)
                    ),
                )
            time.sleep(0.25)
        if not artifacts:
            _fail(
                "takeover probe precondition unmet: no resident fixture "
                "supervisor observable headlessly within "
                f"{supervisor_timeout}s (no daemon artifacts under the "
                "fixture config root); the takeover check moves to user acceptance",
                supervisor_artifacts=[],
            )

        # Swap the fixture symlink to the new binary; bounded takeover wait.
        os.unlink(link)
        os.symlink(new_binary.path, link)
        markers: list[str] = []
        baseline = artifacts
        deadline = time.monotonic() + takeover_timeout
        while time.monotonic() < deadline:
            current = _daemon_artifacts(fixture.claude_config_dir)
            if current != baseline:
                markers.append("fixture-daemon-artifacts-changed")
            if process.poll() is not None:
                markers.append("session-process-replaced")
                break
            if markers:
                break
            time.sleep(0.5)
        if not markers:
            _fail(
                "takeover not observed within "
                f"{takeover_timeout}s after the fixture relink; the takeover check "
                "is unresolved headlessly and moves to user acceptance",
                supervisor_artifacts=list(artifacts),
            )

        # Release the held turn; the (possibly respawned) session must still
        # delegate to the marker agent through the carried scope.
        hold.set()
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=session_timeout)
            returncode: int | None = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
            stdout, stderr = process.communicate()
            returncode = None
        domain_after = snapshot_daemon_domain(live)
        siblings_after = fixture_daemon_domains(live)
        daemon = _enforce_live_domain_untouched(
            domain_before,
            domain_after,
            fixture=fixture,
            fixture_domains_before=siblings_before,
            fixture_domains_after=siblings_after,
        )
        delegation = DelegationResult(
            subagent_type=subagent_type,
            classification=responder.classification,
            branch=responder.branch,
            scope_dir=str(scope),
            returncode=returncode,
            timed_out=timed_out,
            requests=provider.requests,
            daemon=daemon,
            evidence=None,
        )
        verdict = f"takeover-delegation-{delegation.classification}"
        run_record = {
            "returncode": returncode,
            "timed_out": timed_out,
            "argv_count": len(argv),
            "argv_flags": [token for token in argv[1:] if token.startswith("-")],
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stdout_sha256": strict_json.sha256_hex(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
            "stderr_sha256": strict_json.sha256_hex(stderr.encode("utf-8")),
        }
        path = write_evidence(
            fixture,
            evidence_name,
            {
                "version": 1,
                "kind": "takeover-probe",
                "subagent_type": subagent_type,
                "scope_dir": str(scope),
                "verdict": verdict,
                "old_binary": _retained_record(old_binary),
                "new_binary": _retained_record(new_binary),
                "supervisor_artifacts": list(artifacts),
                "takeover_markers": markers,
                "delegation": {
                    "classification": delegation.classification,
                    "branch": delegation.branch,
                },
                "run": run_record,
                "requests": [asdict(record) for record in provider.requests],
                "daemon": asdict(daemon) if daemon is not None else None,
            },
        )
        return TakeoverProbeResult(
            verdict=verdict,
            old_binary=old_binary,
            new_binary=new_binary,
            supervisor_artifacts=artifacts,
            takeover_markers=tuple(markers),
            delegation=delegation,
            daemon=daemon,
            evidence=path,
        )
    finally:
        hold.set()
        if process.poll() is None:
            _kill_process_group(process)
            process.wait()
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        provider.stop()


# --------------------------------------------------------------------- CLI



# ---------------------------------------------------------------- the exact-client check
_XC_AGENT = "cm-qualify-agent"
_XC_TOOL_NUMBER = 1
# Explicit probe markers: the lead prompt carries the lead marker and the
# cm-* agent's definition body (its system prompt, never sent on the lead's
# requests) the agent marker. A request is a probe request by its marker —
# never by its model, so a substituted model is observed, not dropped.
_XC_LEAD_MARKER = "cmxc-lead-probe-5d1e"
_XC_AGENT_MARKER = "cmxc-agent-probe-5d1e"
_XC_MARKERS = {"lead": _XC_LEAD_MARKER, "agent": _XC_AGENT_MARKER}
_XC_PROMPT = f"qualify probe {_XC_LEAD_MARKER}"


class _ExactClientRecorder:
    """Metadata-only responder: the request model, the output_config.effort
    id, the thinking type and max_tokens of every lead-class and agent-class
    request; spawns one cm-* agent when asked.

    The class comes from the probe markers (and the agent-id hint), never
    from the model: a request on another model is recorded with the model it
    carried, so a substitution classifies as ``model-rewritten``. Auxiliary
    requests (the client's ``auxiliary`` request class, or no probe marker)
    and token counting are excluded explicitly."""

    def __init__(self, model: str, *, spawn: bool = False):
        self.model = model
        self.spawn = spawn
        self.spawned = False
        self.lead: list[dict[str, Any]] = []
        self.child: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    @staticmethod
    def request_class(record: RequestRecord) -> str | None:
        """``lead``, ``agent`` or None (auxiliary / not a probe request)."""

        hints = dict(record.hint_header_values)
        if hints.get("x-claude-code-request-class") == "auxiliary":
            return None
        if hints.get("x-claude-code-agent-id") or "agent" in record.markers_found:
            return "agent"
        if "lead" in record.markers_found:
            return "lead"
        return None

    def respond(self, document: dict[str, Any], path: str, context: RequestContext) -> tuple[int, Any]:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        record = context.record
        content: list[dict[str, Any]] = [{"type": "text", "text": "PROBE-OK"}]
        stop = "end_turn"
        with self.lock:
            kind = self.request_class(record)
            if kind is not None:
                row = {"model": record.model, "effort": record.effort, "thinking": record.thinking_type,
                       "max_tokens": record.max_tokens}
                (self.child if kind == "agent" else self.lead).append(row)
                if self.spawn and kind == "lead" and not self.spawned and "Agent" in record.tool_names:
                    self.spawned = True
                    content = [{"type": "tool_use", "id": f"toolu_cmxc_{_XC_TOOL_NUMBER}", "name": "Agent",
                                "input": {"description": "qualify probe", "subagent_type": _XC_AGENT,
                                          "prompt": "Reply PROBE-OK", "run_in_background": False}}]
                    stop = "tool_use"
        payload = _message_payload(content, model=document.get("model"), stop_reason=stop,
                                   message_id="msg_cmxc", input_tokens=1)
        return 200, _sse_message(payload) if document.get("stream") else payload


def _context_window(text: str) -> int | None:
    match = re.search(r"\*\*Tokens:\*\*\s*\S+\s*/\s*([0-9.]+)([km]?)", text)
    if not match:
        return None
    return int(float(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]])


def run_exact_client_check(request: Any, *, native_contract: dict[str, Any],
                           environ: Mapping[str, str] | None = None,
                           timeout: float = 120.0) -> Any:
    """The offline exact-client check of one operator pool line.

    The pinned Claude Code client (trusted path + sha256) runs inside
    ``bwrap --unshare-net`` against a loopback fake provider with the line's
    production compile: as the Direct lead at every declared effort, as a
    cm-* agent, and for ``/context``. Zero provider requests; only request
    metadata is observed. Without the pinned executable or a trusted bwrap
    the outcome is ``inconclusive``/``unavailable`` ("exact-client proof
    unavailable on this platform"), never a pass.
    """

    from . import qualify as qualify_mod

    unavailable = qualify_mod.ExactClientOutcome(qualify_mod.INCONCLUSIVE, "unavailable")
    if network_isolation_available() is not None:
        return unavailable
    hold = None
    try:
        trusted = trusted_from_contract(native_contract, environ)
        # claude-multi's own copy stays locked across every run of the check
        # (each run also hands its own lock to the client it starts).
        hold = _hold_owned_copy(trusted, os.environ if environ is None else environ)
        _verify_trusted_executable(trusted)
    except (ProbeError, OSError, errors.ClaudeMultiError):
        if hold is not None:
            hold.release()
        return unavailable
    try:
        return _exact_client_runs(request, trusted, timeout)
    finally:
        if hold is not None:
            hold.release()


def _exact_client_runs(request: Any, trusted: TrustedExecutable, timeout: float) -> Any:
    """The runs of :func:`run_exact_client_check` (the executable verified)."""

    import tempfile

    from . import qualify as qualify_mod
    from . import scope as scope_mod

    root = Path(tempfile.mkdtemp(prefix="cm-qualify-xc-"))
    os.chmod(root, 0o700)
    environ = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
    count = 0

    def fixture() -> ProbeFixture:
        nonlocal count
        count += 1
        built = build_fixture(root / f"run-{count}", environ=environ)
        seed_fixture_project_trust(built, built.project_dir)
        return built

    def run(built: ProbeFixture, responder: Any, *, prompt: str, extra: tuple[str, ...],
            document: dict[str, Any]) -> NativeRunResult:
        settings = write_fixture_settings(built, "scope/settings.json", document)
        argv = ("-p", prompt, "--dangerously-skip-permissions", "--model", request.selector,
                "--settings", str(settings), *extra)
        with FakeAnthropicProvider(responder=responder, content_markers=_XC_MARKERS) as provider:
            return run_native(argv, trusted=trusted, fixture=built, provider=provider, allow_real=True,
                              environ=environ, timeout=timeout)

    try:
        observations: dict[str, Any] = {"lead": {}, "agent": {}}
        document = strict_json.loads(strict_json.canonical_bytes(dict(request.settings)))
        for effort in request.efforts:
            built = fixture()
            live = scope_mod.write_scope(built.root / "state", str(uuid.uuid4()), request.plan)
            responder = _ExactClientRecorder(request.wire)
            result = run(built, responder, prompt=_XC_PROMPT,
                         extra=("--add-dir", str(live), "--effort", effort), document=document)
            if result.timed_out or result.returncode != 0:
                return qualify_mod.ExactClientOutcome(qualify_mod.INCONCLUSIVE, "no-run")
            observations["lead"][effort] = {"effort": effort, "rows": responder.lead}
        agent_efforts = request.efforts[-1:] if request.efforts else ()
        for effort in agent_efforts:
            built = fixture()
            live = scope_mod.write_scope(built.root / "state", str(uuid.uuid4()), request.plan)
            directory = state.ensure_private_dir(built.project_dir / ".claude/agents")
            state.atomic_write(directory / f"{_XC_AGENT}.md", (
                f"---\nname: {_XC_AGENT}\ndescription: claude-multi qualification probe\n"
                f"model: {request.selector}\neffort: {effort}\n---\nReply PROBE-OK. {_XC_AGENT_MARKER}\n").encode())
            responder = _ExactClientRecorder(request.wire, spawn=True)
            lead_effort = request.efforts[0]
            result = run(built, responder, prompt=_XC_PROMPT,
                         extra=("--add-dir", str(live), "--effort", lead_effort), document=document)
            if result.timed_out or result.returncode != 0 or not responder.spawned:
                return qualify_mod.ExactClientOutcome(qualify_mod.INCONCLUSIVE, "no-run")
            observations["agent"][effort] = {"effort": effort, "rows": responder.child}
        window: dict[str, int | None] = {}
        for kind in ("client", "compiled"):
            built = fixture()
            live = scope_mod.write_scope(built.root / "state", str(uuid.uuid4()), request.plan)
            result = run(built, _ExactClientRecorder(request.wire), prompt="/context",
                         extra=("--add-dir", str(live)),
                         document=document if kind == "compiled" else {"availableModels": [request.selector]})
            if result.returncode != 0:
                return qualify_mod.ExactClientOutcome(qualify_mod.INCONCLUSIVE, "no-run")
            window[kind] = _context_window(result.stdout)
        observations["window"] = window
        return qualify_mod.exact_client_class(
            observations, wire=request.wire, efforts=request.efforts, client_effort=request.client_effort,
            output_limit=request.output_limit, window=dict(request.expected_window),
        )
    except ProbeError:
        return qualify_mod.ExactClientOutcome(qualify_mod.INCONCLUSIVE, "no-run")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def probe_cli(
    positionals: list[str],
    flags: dict[str, Any],
    run_argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Gated CLI for the disposable probe harness; returns a process exit code.

    ``--allow-local-claude`` is presence-based explicit consent: its value
    is ignored and it never relaxes any other check. ``probe init`` takes no
    executable argv. ``probe run`` requires ``--native-contract FILE`` and
    passes any argv after ``--`` to the contract-pinned executable; with the
    separate presence-based ``--allow-real-execution`` flag the real contract
    binary runs under the narrow allowance, otherwise the CLI run path
    refuses the real (contract) spec, so its evidence branch is reachable
    only from API-level fake-spec unit probes.
    """

    ambient = os.environ if environ is None else environ
    try:
        if "allow-local-claude" not in flags:
            raise ProbeError("probe requires explicit --allow-local-claude consent")
        fixture_root = flags.get("fixture-root")
        if not isinstance(fixture_root, str) or not fixture_root:
            raise ProbeError("probe requires --fixture-root PATH")
        assert_no_provider_credentials(ambient)
        action = positionals[0] if positionals else ""
        if action == "init":
            if run_argv:
                raise ProbeError("probe init takes no executable argv")
            fixture = build_fixture(fixture_root, environ=ambient)
            sys.stdout.write(
                strict_json.canonical_file_bytes(fixture_manifest(fixture)).decode(
                    "utf-8"
                )
            )
            return 0
        if action == "run":
            contract_path = flags.get("native-contract")
            if not isinstance(contract_path, str) or not contract_path:
                raise ProbeError("probe run requires --native-contract FILE")
            contract = strict_json.load(Path(contract_path))
            if not isinstance(contract, dict):
                raise ProbeError("native contract must be a JSON object")
            trusted = trusted_from_contract(contract, ambient)
            fixture = build_fixture(fixture_root, environ=ambient)
            result = run_native(
                run_argv,
                trusted=trusted,
                fixture=fixture,
                allow_real="allow-real-execution" in flags,
            )
            # Metadata-only evidence (ora-28): this branch is reachable only
            # from API-level fake-spec probes, and even there raw argv and
            # stdio text are never persisted.
            argv_joined = "\n".join(result.argv).encode("utf-8")
            stdout_bytes = result.stdout.encode("utf-8")
            stderr_bytes = result.stderr.encode("utf-8")
            evidence = {
                "version": 1,
                "kind": "probe-run",
                "run": {
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "argv_count": len(result.argv),
                    "argv_sha256": strict_json.sha256_hex(argv_joined),
                    "stdout_bytes": len(stdout_bytes),
                    "stdout_sha256": strict_json.sha256_hex(stdout_bytes),
                    "stderr_bytes": len(stderr_bytes),
                    "stderr_sha256": strict_json.sha256_hex(stderr_bytes),
                },
                "requests": [asdict(record) for record in result.requests],
            }
            path = write_evidence(fixture, "last-run", evidence)
            print(
                f"probe run: returncode={result.returncode} "
                f"timed_out={result.timed_out} requests={len(result.requests)}"
            )
            print(f"evidence: {path}")
            if result.timed_out or result.returncode != 0:
                return 2
            return 0
        raise ProbeError(
            f"unknown probe action {action!r}; "
            "expected 'init' or 'run'"
        )
    except (errors.ClaudeMultiError, OSError) as exc:
        print(f"claude-multi-dev: probe: {exc}", file=sys.stderr)
        return 2
