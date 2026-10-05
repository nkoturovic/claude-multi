"""Custom providers & ordinary models: the legacy user registry.

``~/.config/claude-multi/custom.json`` holds operator-added providers
(Anthropic-compatible endpoint + key env var) and ordinary-session models
— provider-listed (verified endpoints, e.g. Kimi) or manually typed.
Composition admission stays a catalog act: customs carry no lanes/roles/
qualification and never appear near a composition. Each distinct context
bound forms its own ordinary profile (group = shared /model fence + one
compaction policy).

The registry merges into the docs view consumed by the ordinary launch and
render paths only; the trusted catalog never sees it.
"""

from __future__ import annotations

from . import assets

from pathlib import Path
from typing import Any, Callable, Collection, Mapping

from . import catalog, errors, paths, render, sessions, state, strict_json
from . import validate as schema_validate


CUSTOM_QUALIFICATION = "operator-declared (custom.json); not benchmark-verified"


class CustomModelsError(errors.ClaudeMultiError, ValueError):
    """Raised on any custom-registry failure (fail closed)."""


def registry_path(environ: dict[str, str]) -> Path:
    return sessions.config_root(environ) / "custom.json"


def registry_parity(environ: dict[str, str]) -> str | None:
    """Report a shell/service registry mismatch without exposing either file."""

    shell = registry_path(environ)
    service = paths.home(environ) / ".config/claude-multi/custom.json"
    if shell == service:
        return None
    a, b = paths.display(shell, environ), paths.display(service, environ)
    contents: list[bytes | None] = []
    for path, shown in ((shell, a), (service, b)):
        # Read once: an absent file (or one gone since) is simply absent; any
        # other I/O error is reported by path, never raised or shown by content.
        try:
            contents.append(path.read_bytes())
        except (FileNotFoundError, NotADirectoryError):
            contents.append(None)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            return (
                f"custom.json unreadable: {shown} cannot be read ({reason}), so the shell "
                f"({a}, XDG_CONFIG_HOME) and gateway service ({b}) registries cannot be "
                f"compared — restore its access (owner-readable, 0600), keep one registry at "
                f"{b} and run claude-multi without XDG_CONFIG_HOME, then claude-multi-proxy init"
            )
    shell_bytes, service_bytes = contents
    shell_exists, service_exists = shell_bytes is not None, service_bytes is not None
    if not shell_exists and not service_exists:
        return None
    if shell_bytes == service_bytes:
        return None
    mismatch = (
        "the two files differ" if shell_exists and service_exists
        else f"only {a if shell_exists else b} exists"
    )
    return (
        f"custom.json mismatch: this shell reads {a} (XDG_CONFIG_HOME) but the gateway "
        f"service reads {b} (it runs without XDG_CONFIG_HOME); {mismatch} — custom "
        f"models can launch but are not served. Keep one registry at {b} and run "
        "claude-multi without XDG_CONFIG_HOME, then claude-multi-proxy init"
    )


def header_rule_violations(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(provider id, header) pairs the pinned gateway would silently send as Bearer."""

    return sorted(
        (pid, spec.get("header", "x-api-key"))
        for pid, spec in document.get("providers", {}).items()
        if spec.get("auth_kind") == "header"
        and spec.get("header", "x-api-key").lower() != "x-api-key"
    )


def _schema() -> dict[str, Any]:
    return strict_json.load(
        assets.root() / "schemas" / "custom.schema.json"
    )


def _empty() -> dict[str, Any]:
    return {"version": 1, "providers": {}, "models": {}}


def load_registry(environ: dict[str, str]) -> dict[str, Any]:
    """Load + validate the registry; an absent file means an empty one."""

    path = registry_path(environ)
    if not path.exists():
        return _empty()
    try:
        document = strict_json.loads(state.read_private(path))
    except (state.StateError, ValueError) as exc:
        raise CustomModelsError(f"cannot load the custom registry: {exc}") from exc
    if "providers" not in document:  # tolerate a models-only v1 file
        document = {"version": 1, "providers": {}, **document}
    problems = schema_validate.validate(document, _schema(), "$")
    if problems:
        raise CustomModelsError(
            f"invalid custom registry: {'; '.join(problems)}"
        )
    for key in [*document["providers"], *document["models"]]:
        state.check_name(key)
    return document


def save_registry(environ: dict[str, str], document: dict[str, Any]) -> Path:
    """Validate and atomically write the registry (0600, private dir)."""

    sessions.check_state_marker(sessions.state_root(environ))
    problems = schema_validate.validate(document, _schema(), "$")
    if problems:
        raise CustomModelsError(
            f"refusing to save an invalid registry: {'; '.join(problems)}"
        )
    for key in [*document["providers"], *document["models"]]:
        state.check_name(key)
    path = registry_path(environ)
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, strict_json.pretty_file_bytes(document))
    return path


def _mutate(
    environ: dict[str, str],
    mutate: Callable[[dict[str, Any]], None],
    *,
    touches: Callable[[dict[str, Any]], set[str]],
) -> None:
    """Locked load-modify-save (FileLock covers the whole transaction)."""

    sessions.check_state_marker(sessions.state_root(environ))
    path = registry_path(environ)
    state.ensure_private_dir(path.parent)
    lock = state.FileLock(path)
    lock.acquire(blocking=True)
    try:
        registry = load_registry(environ)
        mutate(registry)
        touched = touches(registry)
        for provider_id, header in header_rule_violations(registry):
            if provider_id in touched:
                raise CustomModelsError(
                    f"refusing to save the custom registry: provider {provider_id} uses "
                    f"header auth with {header!r}, and this gateway build honors only "
                    'the x-api-key header — set "header": "x-api-key" for it in '
                    f"{paths.display(path, environ)}, or remove it: claude-multi custom "
                    f"remove-provider {provider_id} (its models go with it)"
                )
        save_registry(environ, registry)
    finally:
        lock.release()


def add_provider(
    environ: dict[str, str],
    provider_id: str,
    *,
    base_url: str,
    auth_kind: str,
    secret_env: str,
    header: str | None = None,
    display: str | None = None,
    catalog_providers: tuple[str, ...] | dict[str, Any] = (),
) -> None:
    state.check_name(provider_id)
    if provider_id in catalog_providers:
        raise CustomModelsError(
            f"provider {provider_id!r} exists in the trusted catalog — "
            "custom entries never shadow catalog ids"
        )
    if render.is_reserved_name(provider_id):
        # Rendering it would refuse (and the gateway unit's run would fail).
        raise CustomModelsError(
            f"provider id {provider_id!r} is reserved for the gateway render sentinel"
        )

    def _apply(registry: dict[str, Any]) -> None:
        registry["providers"][provider_id] = {
            "base_url": base_url,
            "auth_kind": auth_kind,
            "secret_env": secret_env,
            **({"header": header} if header else {}),
            **({"display": display} if display else {}),
        }

    _mutate(environ, _apply, touches=lambda _after: {provider_id})


def add_model(
    environ: dict[str, str],
    model_id: str,
    *,
    wire_model: str,
    provider: str,
    context_tokens: int,
    display: str | None = None,
    created_via: str,
    catalog_providers: dict[str, Any] | None = None,
    catalog_models: Collection[str] = (),
    retired_models: Collection[str] = (),
) -> None:
    """Insert or replace one model; provider may be catalog or custom.

    The id never shadows the trusted catalog (a custom with a catalog id
    would silently override it in the merged ordinary view).
    ``catalog_models`` must be every v2 line key (``catalog_line_ids`` /
    ``Catalog.lines``: New lines included) and ``retired_models`` the
    retired keys and ``@`` bases (``retired_model_ids``).
    """

    catalog_providers = {} if catalog_providers is None else catalog_providers
    state.check_name(model_id)
    if model_id in catalog_models:
        raise CustomModelsError(
            f"model {model_id!r} exists in the trusted catalog — "
            "custom entries never shadow catalog ids"
        )
    if model_id in retired_models:
        raise CustomModelsError(
            f"model id {model_id} is a retired catalog key — custom entries "
            "never shadow catalog ids"
        )
    if provider in catalog_providers:
        # Catalog-direct providers (kimi/qwen) are fine; OAuth pools are
        # not: a pool-backed custom alias renders past catalog admission
        # and fails only upstream.
        kind = catalog_providers[provider]["transport"]["kind"]
        if kind == "oauth-pool":
            raise CustomModelsError(
                f"provider {provider!r} is an OAuth pool — custom models "
                "need a direct (key-based) provider; pool aliases are "
                "catalog admission's domain"
            )

    def _apply(registry: dict[str, Any]) -> None:
        if provider not in registry["providers"] and provider not in catalog_providers:
            raise CustomModelsError(
                f"provider {provider!r} is neither a custom nor a catalog provider"
            )
        registry["models"][model_id] = {
            "wire_model": wire_model,
            "provider": provider,
            "context_tokens": context_tokens,
            "created_via": created_via,
            **({"display": display} if display else {}),
        }

    _mutate(environ, _apply, touches=lambda after: {after["models"][model_id]["provider"]})


def remove_model(environ: dict[str, str], model_id: str) -> bool:
    outcome = {"removed": False}

    def _apply(registry: dict[str, Any]) -> None:
        if model_id in registry["models"]:
            del registry["models"][model_id]
            outcome["removed"] = True

    _mutate(environ, _apply, touches=lambda _after: set())
    return outcome["removed"]


def remove_provider(
    environ: dict[str, str], provider_id: str
) -> tuple[bool, tuple[str, ...]]:
    """Remove a provider, cascading its models only when its auth cannot work."""

    removed = False
    cascaded: tuple[str, ...] = ()

    def _apply(registry: dict[str, Any]) -> None:
        nonlocal removed, cascaded
        if provider_id not in registry["providers"]:
            return
        models = tuple(sorted(
            key for key, spec in registry["models"].items()
            if spec["provider"] == provider_id
        ))
        violating = {pid for pid, _header in header_rule_violations(registry)}
        if models and provider_id not in violating:
            raise CustomModelsError(
                f"provider {provider_id!r} still has custom models — remove them first"
            )
        for model_id in models:
            del registry["models"][model_id]
        del registry["providers"][provider_id]
        removed, cascaded = True, models

    _mutate(environ, _apply, touches=lambda _after: set())
    return removed, cascaded


def profile_for(context_tokens: int) -> str:
    """Ordinary profile id for a custom bound (group = one fence)."""

    return f"custom-{context_tokens}"


def profile_label(profile: str) -> str:
    """Picker section label for a custom profile id."""

    if not profile.startswith("custom-"):
        return profile
    tokens = int(profile.removeprefix("custom-"))
    return f"custom · {tokens // 1024}K context (operator-declared)"


def synthetic_providers(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Catalog-provider-shaped entries for custom providers (render input).

    Direct Anthropic-compatible transport only (v1); no payload contracts
    (no effort overrides), no passthrough routes, family 'custom'.
    """

    providers: dict[str, dict[str, Any]] = {}
    for provider_id in sorted(registry.get("providers", {})):
        spec = registry["providers"][provider_id]
        auth: dict[str, Any] = {
            "kind": spec["auth_kind"],
            "secret_ref": f"env:{spec['secret_env']}",
        }
        if spec["auth_kind"] == "header":
            auth["header"] = spec.get("header", "x-api-key")
        providers[provider_id] = {
            "id": provider_id,
            "display": spec.get("display", provider_id),
            "independence_family": "custom",
            "support": "operator-custom",
            "support_note": "Operator-added provider (custom.json); ordinary sessions only.",
            "adapter": "cliproxy-claude-compatible-v1",
            "transport": {
                "kind": "direct",
                "base_url": spec["base_url"],
                "auth": auth,
            },
            "passthrough_routes": [],
            "payload_contracts": [],
        }
    return providers


def synthetic_entries(
    registry: dict[str, Any], *, cliproxyapi: str
) -> dict[str, dict[str, Any]]:
    """Catalog-line-shaped (models v2) entries for custom models.

    Client-effort by construction: one ``custom-<id>`` selector, efforts
    ``["high"]``, lead-only with no roles, family ``custom``; the
    profile is the declared bound. These entries are never schema-validated
    (``family`` is a synthetic-only key). ``minimum_tested.cliproxyapi`` is
    the installed catalog's gateway baseline: a custom model is first served
    by the current gateway, never by an older one.
    """

    entries: dict[str, dict[str, Any]] = {}
    for model_id in sorted(registry.get("models", {})):
        spec = registry["models"][model_id]
        context = spec["context_tokens"]
        entries[model_id] = {
            "provider": spec["provider"],
            "display": spec.get("display", model_id),
            "generation": "custom",
            "wire_model": spec["wire_model"],
            "selector": f"custom-{model_id}",
            "efforts": ["high"],
            "default_effort": "high",
            "lead": {"effort": "high", "env": {}},
            "capabilities": ["lead"],
            "roles": [],
            "context": {
                "client_tokens": context,
                "declared_tokens": context,
                "provider_tokens": context,
                "scalar_tokens": None,
                "ordinary_profile": profile_for(context),
                "qualification": CUSTOM_QUALIFICATION,
                "validated_tokens": min(context, catalog.CUSTOM_VALIDATED_CAP),
                "user_reported_tokens": context,
            },
            "routing_note": "Custom ordinary model (operator-added).",
            "minimum_tested": {"claude_code": "2.1.216", "cliproxyapi": cliproxyapi},
            "status": "active",
            "registry_overlay": None,
            "family": "custom",
        }
    return entries


def _v2_lines(docs: dict[str, Any]) -> dict[str, Any]:
    """Every catalog line, New included (never the v1 view, which omits them)."""

    return (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]


def catalog_line_ids(docs: dict[str, Any]) -> frozenset[str]:
    """Live catalog line keys a custom id may never take (``status: new`` included)."""

    return frozenset(_v2_lines(docs))


def retired_model_ids(docs: dict[str, Any]) -> frozenset[str]:
    """Retired catalog keys and the bases of ``<line>@<generation>`` keys.

    A custom id equal to one of them would shadow a key that sessions,
    records and continuity still resolve through the retired map.
    """

    retired = docs.get("retired", {"retired": {}})["retired"]
    return frozenset(retired) | frozenset(key.split("@", 1)[0] for key in retired if "@" in key)


def merge_conflicts(docs: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    """Custom ids that collide with the trusted catalog (never applied).

    Add-time guards refuse these, but a hand-edited or outdated registry
    can still contain them — the merge drops them loudly (doctor surfaces
    the list) instead of silently re-routing traffic and secrets. Provider
    ids reserved for the render sentinel are dropped the same way —
    rendering them would refuse and stop the gateway unit from starting.
    The model collision set is every v2 line (New lines included: the v1
    view omits them), every retired key and every ``@`` base.
    """

    catalog_providers = docs["providers"]["providers"]
    lines = catalog_line_ids(docs)
    retired = retired_model_ids(docs)
    return sorted(
        [
            f"provider {provider_id}"
            for provider_id in registry.get("providers", {})
            if provider_id in catalog_providers or render.is_reserved_name(provider_id)
        ]
        + [
            f"model {model_id}"
            for model_id in registry.get("models", {})
            if model_id in lines
        ]
        + [
            f"model {model_id} (retired catalog key)"
            for model_id in registry.get("models", {})
            if model_id in retired and model_id not in lines
        ]
    )


def merge_docs(docs: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """Docs view with custom providers + models merged in (ordinary/render).

    The catalog always wins id collisions (merge_conflicts names the
    dropped custom entries for doctor); a custom can never shadow the
    trusted catalog — a live line (New included), a retired key or an
    ``@`` base — whatever the registry file says. Custom lines land in the
    v2 line map, which ``models`` and ``models-v2`` share (one
    object under both keys, as in a Catalog's docs); the input docs (a
    Catalog's) are copied shallowly and never mutated.
    """

    bad = {pid for pid, _header in header_rule_violations(registry)}
    providers = {
        key: value
        for key, value in synthetic_providers(registry).items()
        if key not in docs["providers"]["providers"]
        and not render.is_reserved_name(key)
        and key not in bad
    }
    baseline = docs["gateway"]["gateway"]["cliproxyapi_baseline"]
    taken = catalog_line_ids(docs) | retired_model_ids(docs)
    entries = {
        key: value
        for key, value in synthetic_entries(registry, cliproxyapi=baseline).items()
        if key not in taken
        and not render.is_reserved_name(value["provider"])
        and value["provider"] not in bad
    }
    if not providers and not entries:
        return docs
    merged = dict(docs)
    if providers:
        providers_doc = dict(docs["providers"])
        providers_doc["providers"] = {**docs["providers"]["providers"], **providers}
        merged["providers"] = providers_doc
    if entries:
        source = docs["models-v2"] if "models-v2" in docs else docs["models"]
        lines_doc = {**source, "models": {**source["models"], **entries}}
        merged["models"] = merged["models-v2"] = lines_doc
    return merged
