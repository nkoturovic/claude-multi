"""Read-only Claude settings inputs: managed policy, settings layers, proxy env maps.

Never report setting values: proxy URLs and policy credentials can be secrets.
Unreadable layers contribute nothing; no settings file is created or modified.

Managed policy files live in one root per operating system, the one the
pinned client reads (``managed-settings.json`` plus ``managed-settings.d/*.json``):
``/etc/claude-code`` on Linux and WSL, ``/Library/Application Support/
ClaudeCode`` on macOS and ``C:\\Program Files\\ClaudeCode`` on Windows. Only
files are read; MDM profiles and the Windows registry are not. The policy is
judged the way the client applies it (:func:`policy_source`): each file
parsed like ``JSON.parse`` (a duplicate key's last value wins), the base file
and then the drop-ins in name order merged into one source (objects merged
key by key, arrays joined without repeats, a later scalar wins). A policy
file that exists but cannot be read is reported, never taken as "no
restrictions".
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from . import paths, strict_json


def managed_root_for(platform: str) -> PurePath:
    """The managed-policy directory the client reads on ``platform`` (``sys.platform``)."""

    if platform == "darwin":
        return PurePosixPath("/Library/Application Support/ClaudeCode")
    if platform in ("win32", "cygwin"):
        return PureWindowsPath("C:\\Program Files\\ClaudeCode")
    return PurePosixPath("/etc/claude-code")


MANAGED_ROOT = Path(str(managed_root_for(sys.platform)))
_SETTINGS_CAP = 1 << 20


def _read_bounded(path: Path) -> bytes | None:
    with path.open("rb") as stream:
        raw = stream.read(_SETTINGS_CAP + 1)
    return None if len(raw) > _SETTINGS_CAP else raw


def read_settings(path: Path) -> dict[str, Any]:
    try:
        raw = _read_bounded(path)
        if raw is None:
            return {}
        doc = strict_json.loads(raw)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


def read_settings_as_client(path: Path) -> dict[str, Any]:
    """The layer as the client's JSON parse sees it (JS ``JSON.parse``: a
    duplicate key's LAST value wins; NaN/Infinity are not JSON), within the
    same 1 MiB cap. Only for presence decisions that must match what the
    client applies; every other read stays strict."""

    try:
        raw = _read_bounded(path)
        if raw is None:
            return {}
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _read_policy_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(document, None)``; ``(None, None)`` when the file does not exist;
    ``(None, reason)`` when it exists but cannot be used as the client would
    parse it."""

    try:
        with path.open("rb") as stream:
            raw = stream.read(_SETTINGS_CAP + 1)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or type(exc).__name__})"
    if len(raw) > _SETTINGS_CAP:
        return None, "is larger than 1 MiB"
    try:
        document = json.loads(raw.decode("utf-8", "replace"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, "is not valid JSON"
    if not isinstance(document, dict):
        return None, "is not a JSON object"
    return document, None


def _join_lists(before: list[Any], after: list[Any]) -> list[Any]:
    joined = list(before)
    for item in after:
        if isinstance(item, (str, int, float, bool)) or item is None:
            if any(type(other) is type(item) and other == item for other in joined):
                continue
        joined.append(item)
    return joined


def _merge(base: dict[str, Any], layer: Mapping[str, Any]) -> dict[str, Any]:
    """The client's merge of managed files: objects key by key, arrays
    joined without repeated values, anything else replaced."""

    merged = dict(base)
    for key, value in layer.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        elif isinstance(value, list) and isinstance(current, list):
            merged[key] = _join_lists(current, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class PolicySource:
    """The managed-file policy as the client applies it.

    ``settings`` is the merged source; ``origin`` names, for each top-level
    key (and ``env.<NAME>``), the last file that set it (names-only
    reporting); ``unreadable`` lists ``(file, reason)`` for files that exist
    but cannot be used."""

    settings: dict[str, Any]
    origin: dict[str, Path]
    unreadable: tuple[tuple[Path, str], ...] = ()
    files: tuple[Path, ...] = ()


def policy_source(root: Path = MANAGED_ROOT) -> PolicySource:
    """Merge the managed policy files the way the client does (read only)."""

    settings: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    unreadable: list[tuple[Path, str]] = []
    files = settings_paths(root)
    for path in files:
        document, problem = _read_policy_file(path)
        if problem is not None:
            unreadable.append((path, problem))
        if document is None:
            continue
        settings = _merge(settings, document)
        for key in document:
            origin[key] = path
        env = document.get("env")
        if isinstance(env, dict):
            for name in env:
                origin[f"env.{name}"] = path
    return PolicySource(settings, origin, tuple(unreadable), tuple(files))


def settings_paths(root: Path = MANAGED_ROOT) -> list[Path]:
    try:
        fragments = sorted((root / "managed-settings.d").glob("*.json"))
    except OSError:
        fragments = []
    return [root / "managed-settings.json", *fragments]


def env_layers(environ: Mapping[str, str], cwd: str | Path,
               root: Path = MANAGED_ROOT) -> list[tuple[Path, dict[str, str]]]:
    """The ``env`` maps of the settings layers the managed client reads, in
    order: the user layer, the cwd's project and local files, managed policy.

    The user layer is ``$HOME/.claude/settings.json``, never
    ``$CLAUDE_CONFIG_DIR``: the launcher unsets ``CLAUDE_CONFIG_DIR`` for the
    managed client (``compiler.V2_ENV_UNSET``), so a proxy or bypass there
    never reaches a managed session and one in HOME's file always does."""

    layers = [paths.claude_settings_dir(environ) / "settings.json", Path(cwd) / ".claude/settings.json",
              Path(cwd) / ".claude/settings.local.json", *settings_paths(root)]
    return [(path, settings_env(path)) for path in dict.fromkeys(layers)]


def user_settings_layers(environ: Mapping[str, str], cwd: str | Path) -> list[Path]:
    """The user-written settings files the managed client reads.

    ``$HOME/.claude/settings.json`` (never ``$CLAUDE_CONFIG_DIR``: the
    launcher unsets it for the managed client, ``compiler.V2_ENV_UNSET``),
    then the launch cwd's project and local files."""

    layers = [paths.claude_settings_dir(environ) / "settings.json", Path(cwd) / ".claude/settings.json",
              Path(cwd) / ".claude/settings.local.json"]
    return list(dict.fromkeys(layers))


def permission_mode_layers(environ: Mapping[str, str], cwd: str | Path,
                           root: Path = MANAGED_ROOT) -> list[Path]:
    """The settings layers the permission-mode decision reads: the user
    layers (:func:`user_settings_layers`), then the managed policy files, as
    :func:`env_layers` reads them."""

    return list(dict.fromkeys([*user_settings_layers(environ, cwd), *settings_paths(root)]))


def configures_default_mode(path: Path) -> bool:
    """Presence only: the layer states ``permissions.defaultMode``. The value
    is never read into a report (managed-policy rule above). Parsed like the
    client parses it (:func:`read_settings_as_client`), so a layer the client
    applies despite a duplicate key is never counted as unconfigured."""

    permissions = read_settings_as_client(path).get("permissions")
    return isinstance(permissions, dict) and "defaultMode" in permissions


def unknown_settings_keys(path: Path, known: Iterable[str]) -> list[str]:
    """Top-level keys of a settings file that the pinned client does not know.

    A settings file written by a newer Claude Code can carry keys the pin's
    settings schema lacks; the pin may then skip the whole file (dropping its
    deny rules and its permission mode). Names only, sorted; an unreadable
    or absent file has none."""

    known_keys = frozenset(known)
    return sorted(key for key in read_settings_as_client(path) if key not in known_keys)


def skew_layers(environ: Mapping[str, str], cwd: str | Path,
                known: Iterable[str] | None) -> list[tuple[Path, list[str]]]:
    """``(file, unknown keys)`` for each user settings layer with keys the pin
    does not know. ``known`` None (the pin records no keys) checks nothing."""

    if known is None:
        return []
    known_keys = frozenset(known)
    found = []
    for path in user_settings_layers(environ, cwd):
        keys = unknown_settings_keys(path, known_keys)
        if keys:
            found.append((path, keys))
    return found


def policy_configures_default_mode(root: Path = MANAGED_ROOT) -> bool:
    """Presence only: the managed policy (merged like the client) states
    ``permissions.defaultMode``. Policy outranks the compiled flag settings."""

    permissions = policy_source(root).settings.get("permissions")
    return isinstance(permissions, dict) and "defaultMode" in permissions


def permission_mode_configured(environ: Mapping[str, str], cwd: str | Path,
                               root: Path = MANAGED_ROOT, *,
                               known_keys: Iterable[str] | None = None) -> bool:
    """True when a layer the managed client reads configures a
    permission default mode; the launch then compiles none and the client
    applies the configured one. Decided at launch and resume only.

    With ``known_keys`` (the settings keys the pin knows): when any user
    layer carries keys outside them, the pin may skip that file, so the
    launch compiles the default mode whatever another user layer configures
    (the compiled flag settings outrank the user, project and local files).
    Only a managed policy mode still counts: it outranks the flag settings."""

    if policy_configures_default_mode(root):
        return True
    layers = user_settings_layers(environ, cwd)
    if known_keys is not None:
        known = frozenset(known_keys)
        if any(unknown_settings_keys(path, known) for path in layers):
            return False
    return any(configures_default_mode(path) for path in layers)


def settings_env(path: Path) -> dict[str, str]:
    env = read_settings(path).get("env")
    return {k: v for k, v in env.items() if isinstance(v, str)} if isinstance(env, dict) else {}


@dataclass(frozen=True)
class ManagedLock:
    """One managed-policy setting that affects managed sessions (names only).

    ``kind`` decides the level: ``hooks``, ``credential``, ``version``,
    ``provider`` and ``denied`` block a launch; ``model`` is BLOCKED in
    doctor (the launch names it on stderr); ``note``, ``models`` and
    ``version-unknown`` are Attention. ``detail`` is a computed explanation
    that never contains a policy value.
    """

    path: Path
    key: str
    kind: str
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.kind in BLOCKING_KINDS

    @property
    def effect(self) -> str:
        if self.detail:
            return self.detail
        if self.kind == "hooks":
            return "hooks do not run in managed sessions (the /model fence and lineup notices are off)"
        if self.kind == "model":
            return "the model choice is locked by policy (claude-multi lineups may not apply)"
        if self.key == "allowManagedPermissionRulesOnly":
            return "the session's secret-path denies do not apply"
        if self.key == "apiKeyHelper":
            return ("it replaces the session's gateway credential helper, so managed sessions "
                    "cannot authenticate to the gateway")
        if self.key == "forceLoginOrgUUID":
            return ("it requires a sign-in to a listed organization, which a managed session's "
                    "gateway credential is not")
        return "it may override the session's gateway credential"


def finding_text(lock: ManagedLock) -> str:
    if lock.kind in UNREADABLE_KINDS:
        return f"managed policy {lock.path} {lock.effect}"
    return f"managed policy {lock.path} sets {lock.key}: {lock.effect}"


# Kinds that make a launch refuse (doctor BLOCKED as well). ``unchecked``: a
# policy file the client applies but claude-multi cannot read in full.
BLOCKING_KINDS = frozenset({"hooks", "credential", "version", "provider", "denied", "unchecked"})
# A policy file that exists but cannot be used (``unreadable``: Attention, the
# client cannot parse or read it either; ``unchecked``: BLOCK).
UNREADABLE_KINDS = frozenset({"unreadable", "unchecked"})
POLICY_REMEDY = "ask the policy owner, or use plain claude"


def _unreadable_locks(source: PolicySource) -> list[ManagedLock]:
    locks = []
    for path, reason in source.unreadable:
        if reason == "is larger than 1 MiB":
            locks.append(ManagedLock(path, "", "unchecked", (
                f"{reason}: claude-multi cannot check what it restricts, so a managed session "
                "cannot rely on its hooks and gateway endpoint")))
        else:
            locks.append(ManagedLock(path, "", "unreadable", (
                f"{reason}: the managed client cannot apply it either, and claude-multi checked "
                "nothing in it")))
    return locks


def _source_locks(source: PolicySource) -> list[ManagedLock]:
    doc, origin = source.settings, source.origin
    locks: list[ManagedLock] = []
    for key in ("allowManagedHooksOnly", "disableAllHooks"):
        if doc.get(key) is True:
            locks.append(ManagedLock(origin[key], key, "hooks"))
    for key in ("model", "availableModels"):
        if key in doc:
            locks.append(ManagedLock(origin[key], key, "model"))
    env = doc.get("env")
    if isinstance(env, dict):
        for key in sorted(env):
            if (key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
                or key.startswith("ANTHROPIC_DEFAULT_") and key.endswith("_MODEL")
                or key.startswith("CLAUDE_CODE_SUBAGENT_MODEL")):
                locks.append(ManagedLock(origin.get("env." + key, origin["env"]), "env." + key, "model"))
    for key in ("apiKeyHelper", "forceLoginOrgUUID"):
        if key in doc:
            locks.append(ManagedLock(origin[key], key, "credential"))
    for key in ("allowManagedPermissionRulesOnly", "forceLoginMethod"):
        present = doc.get(key) is True if key == "allowManagedPermissionRulesOnly" else key in doc
        if present:
            locks.append(ManagedLock(origin[key], key, "note"))
    order = {path: index for index, path in enumerate(source.files)}
    return sorted(locks, key=lambda lock: order.get(lock.path, len(order)))


def read_locks(root: Path = MANAGED_ROOT) -> list[ManagedLock]:
    """The policy settings that affect every managed session (names only),
    judged on the merged source (:func:`policy_source`), in file order."""

    return _source_locks(policy_source(root))


# ---------------------------------------------------------------- preflight

_VERSION_PREFIX = re.compile(r"^\s*v?([0-9]+(?:\.[0-9]+)*)")
# Aliases whose model depends on the release or the settings; the client
# ignores them in deniedModels and availableModels.
_IGNORED_MODEL_ALIASES = frozenset({"best", "opusplan", "default"})
_DATED_OR_FAST = re.compile(r"^(?:[0-9]{8}|fast|latest)(?:-.*)?$")


def _version_tuple(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_PREFIX.match(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    a, b = left + (0,) * (width - len(left)), right + (0,) * (width - len(right))
    return (a > b) - (a < b)


def model_id(value: str) -> str:
    """A selector or policy entry as the client compares model ids: lower
    case, without the ``[1m]`` class suffix or a provider prefix."""

    text = value.strip().lower()
    if text.endswith("[1m]"):
        text = text[:-4]
    match = re.search(r"(?:^|[./])(claude-[a-z0-9.:_-]+)$", text)
    if match is not None and match.start(1) > 0:
        text = match.group(1)
    return text


def _family_alias(entry: str) -> bool:
    return "-" not in entry and "." not in entry and entry not in _IGNORED_MODEL_ALIASES


def denied_by(selector: str, entries: Any) -> bool:
    """True when a ``deniedModels`` list denies ``selector``.

    A family alias denies its family; a model id denies itself in every
    spelling (dates, ``-fast``, provider prefixes) and, without a minor
    version, every later minor version."""

    if not isinstance(entries, list):
        return False
    wanted = model_id(selector)
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        denied = model_id(entry)
        if denied in _IGNORED_MODEL_ALIASES:
            continue
        if _family_alias(denied):
            if wanted == denied or wanted.startswith(f"claude-{denied}-"):
                return True
        elif wanted == denied or wanted.startswith(denied + "-"):
            return True
    return False


def allowed_by(selector: str, entries: Any, match: str = "prefix") -> bool:
    """True when an ``availableModels`` list admits ``selector`` under the
    ``availableModelsMatch`` mode (``prefix``, the default, or ``exact``)."""

    if not isinstance(entries, list):
        return True
    wanted = model_id(selector)
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        allowed = model_id(entry)
        if allowed in _IGNORED_MODEL_ALIASES:
            continue
        if _family_alias(allowed):
            if wanted == allowed or wanted.startswith(f"claude-{allowed}-"):
                return True
            continue
        if wanted == allowed:
            return True
        if wanted.startswith(allowed + "-"):
            rest = wanted[len(allowed) + 1:]
            if match != "exact" or _DATED_OR_FAST.match(rest):
                return True
    return False


def _normalized_url(value: Any) -> str | None:
    return value.strip().rstrip("/").lower() if isinstance(value, str) and value.strip() else None


def policy_findings(
    root: Path = MANAGED_ROOT, *, pin_version: str | None = None, base_url: str | None = None,
    selectors: Iterable[str] = (),
) -> list[ManagedLock]:
    """Every managed-policy setting that affects a managed session.

    Judged on the merged managed source (:func:`policy_source`), the way the
    client applies it; each finding names the last file that set its key.
    Policy files that exist but cannot be used come first (``unreadable``
    Attention, ``unchecked`` BLOCK), then the locks of :func:`read_locks`,
    then: ``requiredMinimumVersion`` / ``requiredMaximumVersion`` that exclude
    ``pin_version`` (BLOCK; an unreadable bound is Attention);
    ``allowedProviders`` that does not admit ``base_url`` as a custom
    endpoint (BLOCK: the list must name ``customEndpoint`` and the policy
    must pin ``env.ANTHROPIC_BASE_URL`` to it, in any of its files);
    ``deniedModels`` and ``availableModelsMatch`` (BLOCK when one of
    ``selectors`` is denied, else Attention). Names only: a policy value is
    compared, never reported."""

    wanted = [selector for selector in dict.fromkeys(selectors) if isinstance(selector, str)]
    source = policy_source(root)
    doc, origin = source.settings, source.origin
    findings = _unreadable_locks(source) + _source_locks(source)
    available = doc.get("availableModels") if "availableModels" in doc else None
    match_mode = doc["availableModelsMatch"] if doc.get("availableModelsMatch") in ("prefix", "exact") else "prefix"
    pin = _version_tuple(pin_version) if pin_version else None
    for key, word, sign in (("requiredMinimumVersion", "below the required minimum", -1),
                            ("requiredMaximumVersion", "above the allowed maximum", 1)):
        if key not in doc or pin is None:
            continue
        bound = _version_tuple(doc[key])
        if bound is None:
            findings.append(ManagedLock(origin[key], key, "version-unknown", (
                f"its version bound cannot be compared with Claude Code {pin_version}, "
                "the version claude-multi runs")))
        elif _compare(pin, bound) == sign:
            findings.append(ManagedLock(origin[key], key, "version", (
                f"Claude Code {pin_version}, the version claude-multi runs, is {word}: "
                "the managed client would exit at startup")))
    providers = doc.get("allowedProviders")
    if isinstance(providers, list):
        env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
        if "customEndpoint" not in providers:
            findings.append(ManagedLock(origin["allowedProviders"], "allowedProviders", "provider", (
                "it does not list customEndpoint, so the managed client refuses "
                "claude-multi's gateway endpoint")))
        elif base_url is None or _normalized_url(env.get("ANTHROPIC_BASE_URL")) != _normalized_url(base_url):
            findings.append(ManagedLock(origin["allowedProviders"], "allowedProviders", "provider", (
                "it admits a custom endpoint only at the ANTHROPIC_BASE_URL the managed policy "
                "pins, and that is not claude-multi's gateway")))
    if "deniedModels" in doc:
        denied = [selector for selector in wanted if denied_by(selector, doc["deniedModels"])]
        if denied:
            findings.append(ManagedLock(origin["deniedModels"], "deniedModels", "denied", (
                f"it denies {', '.join(denied)}, bound in this lineup")))
        else:
            findings.append(ManagedLock(origin["deniedModels"], "deniedModels", "models", (
                "a model it denies cannot run in a managed session; a launch whose lineup "
                "binds one refuses")))
    if "availableModelsMatch" in doc:
        refused = [selector for selector in wanted
                   if available is not None and not allowed_by(selector, available, match_mode)]
        if refused:
            findings.append(ManagedLock(origin["availableModelsMatch"], "availableModelsMatch", "denied", (
                f"under it the policy's available models do not admit {', '.join(refused)}, "
                "bound in this lineup")))
        else:
            findings.append(ManagedLock(origin["availableModelsMatch"], "availableModelsMatch", "models", (
                "it narrows which models the policy admits; a launch whose lineup binds a "
                "model it does not admit refuses")))
    return findings
