"""Providers and credentials: plans and applies for every change of what the
gateway serves (API keys, an account provider's API-key transport, route approval, apply,
removing a provider you added, your own endpoints and servers on your
network, on and off), and the provider picker's entries.

Every serving change runs one transaction (:func:`served_transaction`): the
served-change phase (its locks and the preview's sample, revalidated), the
plan's facts checked again, the writes with their exact undo, the render
(undone on any failure, an interrupt included) and, after the locks are
released, the verified reload. Nothing here prints or prompts; values come
in as :class:`claude_multi.setup.model.Secret`.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from claude_multi import account_pools, catalog, errors, operator as operator_mod, paths, profile as profile_mod
from claude_multi import secret_store, sessions, settings as settings_mod, strict_json
from claude_multi.setup import model, texts

READ_ONLY = model.READ_ONLY_SETUP
ACCOUNT_PROVIDERS = {pool.provider: name for name, pool in account_pools.pools().items()}
_FIRST = ("anthropic", "openai", "openrouter")


def _cmd() -> Any:
    import claude_multi.cli.commands.providers as providers_cmd

    return providers_cmd


def _refusal(exc: BaseException) -> model.Refused:
    """A command-layer refusal as a setup refusal (its text and remedy kept)."""

    return model.Refused(str(exc), remedy=getattr(exc, "remedy", None))


def store_of(runtime: Any) -> secret_store.FileSecretStore:
    try:
        return secret_store.default_store(runtime.gateway_environ())
    except secret_store.SecretStoreError as exc:
        raise _refusal(exc) from exc


def key_file_display(runtime: Any) -> str:
    try:
        return paths.display(secret_store.secret_env_path(runtime.gateway_environ()), runtime.environ)
    except secret_store.SecretStoreError as exc:
        raise _refusal(exc) from exc


def key_file_identity(runtime: Any) -> tuple[str, str]:
    """``(source, real path)`` of the API-key file in use now: what a key
    plan was confirmed for."""

    try:
        location = secret_store.key_file_location(runtime.gateway_environ())
    except secret_store.SecretStoreError as exc:
        raise _refusal(exc) from exc
    return location.source, os.path.realpath(location.path)


def open_planned_store(runtime: Any, planned: tuple[str, str]) -> secret_store.FileSecretStore:
    """The key file a plan was confirmed for, opened inside the transaction
    (its locks are held, and a new key file is selected only under them): a
    different file selected meanwhile refuses with nothing written."""

    env = runtime.gateway_environ()
    try:
        location = secret_store.key_file_location(env)
    except secret_store.SecretStoreError as exc:
        raise _refusal(exc) from exc
    if (location.source, os.path.realpath(location.path)) != tuple(planned):
        raise model.Stale(texts.KEY_FILE_WHAT)
    return secret_store.FileSecretStore(location.path, environ=env)


def set_secret(undo: model.UndoStack, store: Any, name: str, value: str) -> None:
    """Save one key; its undo (the previous value back, or the key removed)
    is registered first and runs only while the saved value is this one."""

    previous = store.get(name)
    guarded_write(undo, lambda: store.set(name, value), landed=lambda: store.get(name) == value,
                  restore=lambda: store.set(name, previous) if previous is not None else store.delete(name))


def delete_secret(undo: model.UndoStack, store: Any, name: str) -> None:
    """Remove one key; its undo puts the value back while it is still gone."""

    previous = store.get(name)
    if previous is None:
        return
    guarded_write(undo, lambda: store.delete(name), landed=lambda: store.get(name) is None,
                  restore=lambda: store.set(name, previous))


def _ledger_now(fresh: Any) -> dict[str, Any]:
    return operator_mod.ledger_document(operator_mod.load_ledger(fresh.env, fresh.schemas))


def ledger_write(undo: model.UndoStack, fresh: Any, change: Callable[[dict[str, Any]], None],
                 written: Callable[[Mapping[str, Any]], bool],
                 revert: Callable[[dict[str, Any]], None]) -> None:
    """One ledger change with its undo registered first: ``revert`` runs only
    while the ledger still shows this change (``written``, checked again
    under the ledger's lock)."""

    def put_back(document: dict[str, Any]) -> None:
        if written(document):
            revert(document)

    guarded_write(undo, lambda: operator_mod.update_ledger(fresh.env, fresh.schemas, change),
                  landed=lambda: written(_ledger_now(fresh)),
                  restore=lambda: operator_mod.update_ledger(fresh.env, fresh.schemas, put_back))


def grant_route(undo: model.UndoStack, fresh: Any, provider: Any) -> None:
    """The route approval of a provider you added, with its exact undo."""

    pid = provider.provider_id
    previous = fresh.ledger.routes.get(pid) if fresh.ledger is not None else None
    record = operator_mod.route_record(provider, at=operator_mod.utc_stamp())

    def grant(document: dict[str, Any]) -> None:
        document["routes"][pid] = record

    def revert(document: dict[str, Any]) -> None:
        if previous is None:
            document["routes"].pop(pid, None)
        else:
            document["routes"][pid] = previous

    ledger_write(undo, fresh, grant, lambda document: document["routes"].get(pid) == record, revert)


def _file_bytes(path: Any) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def write_declaration(undo: model.UndoStack, fresh: Any, provider_id: str, raw: bytes,
                      previous: bytes | None = None) -> None:
    """Write a providers.d file (a new one, or one replacing ``previous``);
    its undo, registered first, puts ``previous`` back (removes a new file)
    while the file still holds these bytes."""

    target = operator_mod.providers_dir(fresh.env) / f"{provider_id}.json"
    guarded_write(undo, lambda: operator_mod.write_provider_bytes(fresh.env, provider_id, raw),
                  landed=lambda: _file_bytes(target) == raw,
                  restore=lambda: _cmd().restore_file(fresh.env, provider_id, previous))


def remove_declaration(undo: model.UndoStack, fresh: Any, provider_id: str, previous: bytes) -> None:
    """Remove a providers.d file; its undo writes it back while it is still gone."""

    target = operator_mod.providers_dir(fresh.env) / f"{provider_id}.json"
    guarded_write(undo, lambda: operator_mod.remove_provider_file(fresh.env, provider_id),
                  landed=lambda: not os.path.lexists(target),
                  restore=lambda: _cmd().restore_file(fresh.env, provider_id, previous))


# ------------------------------------------------------------------ transactions


@dataclass(frozen=True)
class Committed:
    """What a transaction did: the render result and its notes."""

    result: Any
    notes: tuple[str, ...]
    reload: str


def _run_undo(undo: model.UndoStack) -> str | None:
    """Run every undo step; the first failure in words, or None."""

    try:
        undo.run()
    except BaseException as failure:  # every step was attempted; the caller keeps its own error
        return f"{type(failure).__name__}: {failure}"
    return None


def undo_then_raise(undo: model.UndoStack, exc: BaseException) -> None:
    """Undo, then let ``exc`` (the original failure) go on; an undo that did
    not complete is added to it, never put in its place."""

    failed = _run_undo(undo)
    if failed is not None:
        exc.add_note(texts.UNDO_INCOMPLETE.format(detail=failed))


_undo_then_raise = undo_then_raise


def guarded_write(undo: model.UndoStack, write: Callable[[], Any], *, landed: Callable[[], bool],
                  restore: Callable[[], None]) -> Any:
    """One write whose exact undo is registered before it runs. The undo
    restores only while the state still shows this write (``landed``): a
    failure before the bytes were replaced changes nothing, and one after
    they were — durability not confirmed, or an interrupt — is put back."""

    def step() -> None:
        if landed():
            restore()

    undo.push(step)
    return write()


def _render(runtime: Any, undo: model.UndoStack, verb: str) -> tuple[Any, tuple[str, ...]]:
    """The explicit render under the held locks; a refusal (or an interrupt)
    runs the exact undo and raises. A config already published keeps."""

    from claude_multi import proxy as proxy_mod

    notes: list[str] = []
    try:
        _target, result, report = runtime.render_gateway(policy="explicit")
    except proxy_mod.ConfigPublishedError as exc:
        _target, result, report = exc.rendered
        notes.append(f"the new gateway configuration was published but its durability is unconfirmed "
                     f"({exc}); the change is kept — claude-multi providers apply confirms it")
    except (errors.ClaudeMultiError, OSError) as exc:
        failed = _run_undo(undo)
        message = texts.RENDER_REFUSED.format(reason=exc)
        if failed is not None:
            message += " " + texts.UNDO_INCOMPLETE.format(detail=failed)
        raise model.Refused(message) from exc
    except BaseException as exc:
        _undo_then_raise(undo, exc)
        raise
    notes.extend(f"operator: {note}" for note in report.operator)
    return result, tuple(notes)


def served_transaction(runtime: Any, *, verb: str, preflight: Any,
                       write: Callable[[Any, model.UndoStack], None],
                       require: Callable[[Any], None] | None = None) -> Committed:
    """One locked commit with exact undo, the render, then the verified reload."""

    cmd = _cmd()
    try:
        with cmd.operator_write(runtime, preflight=preflight):
            fresh = cmd.operator_context(runtime)
            if require is not None:
                require(fresh)
            undo = model.UndoStack()
            try:
                write(fresh, undo)
            except BaseException as exc:
                _undo_then_raise(undo, exc)
                raise
            result, notes = _render(runtime, undo, verb)
    except cmd.OperatorCommandError as exc:
        raise _refusal(exc) from exc
    outcome = runtime.verify_reload(result.sentinel)
    return Committed(result, notes, outcome.status)


def preflight(runtime: Any, verb: str, **kwargs: Any) -> Any:
    """The shared served-change preview (no output; an unknown live impact refuses)."""

    cmd = _cmd()
    try:
        return cmd.served_preflight(runtime, verb, show=False, **kwargs)
    except cmd.OperatorCommandError as exc:
        raise _refusal(exc) from exc
    except (errors.ClaudeMultiError, OSError) as exc:
        raise _refusal(exc) from exc


def served_sample(runtime: Any) -> str:
    """The served-change sample as it is now, for a plan that takes it
    before reading its own facts (passed on to :func:`preflight` as
    ``sample``): a change after it refuses the apply."""

    cmd = _cmd()
    try:
        return cmd._sample(runtime)
    except (errors.ClaudeMultiError, OSError) as exc:
        raise _refusal(exc) from exc


def reload_text(runtime: Any, status: str, *, display: str | None = None, lines: int | None = None) -> str:
    """The reload outcome in words (``texts.RELOAD_TEXT``)."""

    from claude_multi import service

    if status == "reloaded" and display is not None and lines is not None:
        if lines:
            return texts.RELOAD_TEXT["reloaded+models"].format(n=lines, display=display)
        return texts.RELOAD_TEXT["reloaded+none"].format(display=display)
    if status == "restart_required":
        try:
            hint = service.hint("restart", home=runtime.home)
        except (ValueError, OSError, errors.ClaudeMultiError):
            hint = "claude-multi gateway restart"
        return texts.RELOAD_TEXT["restart_required"].format(restart_hint=hint)
    return texts.RELOAD_TEXT.get(status, texts.RELOAD_TEXT["reloaded"])


def _verb_lines(committed: Committed, line: str) -> tuple[str, ...]:
    return (line, *committed.notes)


# ------------------------------------------------------------------ provider facts


@dataclass(frozen=True)
class KeyedProvider:
    """A provider that takes an API key: its name, secret and origin."""

    provider_id: str
    display: str
    secret_name: str
    origin: str  # "catalog" | "yours"
    lines: int


def _merged_docs(runtime: Any) -> dict[str, Any]:
    ctx = _cmd().operator_context(runtime)
    return operator_mod.merge_docs(ctx.docs, ctx.layer)


def line_count(runtime: Any, provider_id: str, ctx: Any | None = None) -> int:
    ctx = ctx if ctx is not None else _cmd().operator_context(runtime)
    count = sum(1 for entry in ctx.docs["models-v2"]["models"].values() if entry.get("provider") == provider_id)
    return count + sum(1 for line in ctx.layer.lines.values() if line.provider_id == provider_id)


def transport_names(docs: Mapping[str, Any], provider_id: str) -> dict[str, str]:
    """``display``, ``account`` and ``models`` of an account provider, for
    the texts of its account/API-key transport."""

    provider = docs["providers"]["providers"].get(provider_id) or {}
    display = str(provider.get("display") or provider_id)
    pool = str((provider.get("transport") or {}).get("pool") or "")
    return {"display": display, "account": texts.ACCOUNT_KINDS.get(pool, f"{display} account"),
            "models": texts.TRANSPORT_MODELS.get(provider_id, f"{display} models")}


def _key_alternative(ctx: Any, provider_id: str) -> Any:
    """The reviewed API-key alternative of a catalog account provider, or None."""

    provider = ctx.docs["providers"]["providers"].get(provider_id)
    if provider is None or provider["transport"]["kind"] != operator_mod.TRANSPORT_POOL:
        return None
    return operator_mod.transport_alternative(provider_id, operator_mod.TRANSPORT_API_KEY)


def keyed_provider(runtime: Any, provider_id: str, ctx: Any | None = None) -> KeyedProvider:
    """The keyed provider ``provider_id`` (catalog or yours); refuses the
    account providers, keyless ones and unknown ids with the fix. An account
    provider's API key is keyed only while its key transport is in use."""

    ctx = ctx if ctx is not None else _cmd().operator_context(runtime)
    alternative = _key_alternative(ctx, provider_id)
    if alternative is not None:
        names = transport_names(ctx.docs, provider_id)
        display = names["display"]
        if not alternative.available:
            raise model.Refused(texts.KEY_TRANSPORT_CLOSED.format(**names),
                                line=texts.KEY_TRANSPORT_CLOSED_LINE.format(**names, id=provider_id),
                                fix=model.Fix(f"Enter on {display}", f"claude-multi providers sign-in {provider_id}"))
        if current_transport(runtime, provider_id, ctx) == operator_mod.TRANSPORT_API_KEY:
            # The API key in use: replacing it is a key change.
            return KeyedProvider(provider_id, display, alternative.secret_name, "catalog",
                                 line_count(runtime, provider_id, ctx))
        raise model.Refused(texts.KEY_ACCOUNT_TRANSPORT.format(**names),
                            line=texts.KEY_ACCOUNT_TRANSPORT_LINE.format(**names, id=provider_id),
                            fix=model.Fix(f"Enter on {display}",
                                          f"claude-multi providers transport {provider_id} api-key"))
    own = ctx.layer.providers.get(provider_id)
    if own is not None:
        display = str(own.entry.get("display") or provider_id)
        if own.secret_name is None:
            raise model.Refused(texts.KEY_NOT_NEEDED.format(display=display))
        return KeyedProvider(provider_id, display, own.secret_name, "yours", line_count(runtime, provider_id, ctx))
    provider = ctx.docs["providers"]["providers"].get(provider_id)
    if provider is None:
        raise model.Refused(texts.KEY_UNKNOWN_PROVIDER.format(id=provider_id))
    display = str(provider.get("display") or provider_id)
    ref = ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref")
    if not isinstance(ref, str):
        raise model.Refused(texts.KEY_NOT_NEEDED.format(display=display))
    return KeyedProvider(provider_id, display, ref.removeprefix("env:"), "catalog",
                         line_count(runtime, provider_id, ctx))


def _secret_length(store: Any, name: str) -> int | None:
    try:
        value = store.get(name)
    except secret_store.SecretStoreError as exc:
        raise _refusal(exc) from exc
    return None if value is None else len(value)


def profiles_using(runtime: Any, provider_id: str) -> tuple[str, ...]:
    """Loadable profiles that bind any line of ``provider_id`` (sorted)."""

    try:
        lcat = runtime.lineup_catalog()
        names = runtime.profiles.names()
    except (errors.ClaudeMultiError, OSError):
        return ()
    found: list[str] = []
    for name in names:
        try:
            document = runtime.profiles.load(name)
        except (errors.ClaudeMultiError, OSError, ValueError):
            continue
        specs = [document.get("lead"), *(document.get("agents") or {}).values()]
        for spec in specs:
            key = spec.get("model") if isinstance(spec, Mapping) else None
            if not isinstance(key, str):
                continue
            try:
                resolved = lcat.resolve_key(key).key
            except (catalog.CatalogError, errors.ClaudeMultiError, KeyError):
                resolved = key
            entry = lcat.lines.get(resolved) if resolved else None
            if isinstance(entry, Mapping) and entry.get("provider") == provider_id:
                found.append(name)
                break
    return tuple(sorted(found))


def _profiles_line(display: str, names: Sequence[str]) -> str:
    return (texts.PROFILES_LOSE.format(display=display, names=", ".join(names)) if names
            else texts.PROFILES_NONE.format(display=display))


def _live_ids(runtime: Any, aliases: Iterable[str]) -> tuple[str, ...]:
    cmd = _cmd()
    scan = cmd.record_scan(runtime)
    try:
        cmd.refuse_unknown_liveness(scan, "remove")
    except cmd.OperatorCommandError as exc:
        raise _refusal(exc) from exc
    return tuple(sorted(user[:8] for user in cmd.live_users(scan, aliases)))


def provider_aliases(runtime: Any, provider_id: str, ctx: Any | None = None) -> frozenset[str]:
    ctx = ctx if ctx is not None else _cmd().operator_context(runtime)
    bases: set[str] = set()
    provider = ctx.docs["providers"]["providers"].get(provider_id) or {}
    bases.update(route["name"] for route in provider.get("passthrough_routes", []))
    for entry in ctx.docs["models-v2"]["models"].values():
        if entry.get("provider") == provider_id:
            bases.update(operator_mod.line_aliases(entry))
    for line in ctx.layer.lines.values():
        if line.provider_id == provider_id:
            bases.update(operator_mod.line_aliases(line.core_entry))
    return frozenset(bases)


# ------------------------------------------------------------------ set or replace an API key


@dataclass(frozen=True)
class KeyPlan(model.Plan):
    provider_id: str
    display: str
    secret_name: str
    path_display: str
    replaces: bool
    current_length: int | None
    zero_lines: bool
    lines_count: int = 0
    key_file: tuple[str, str] = ("", "")  # (source, real path) the key goes to
    served: Any = field(default=None, compare=False, repr=False)
    # The other providers whose API key is ``secret_name`` (sorted ids): a
    # new value replaces their key too, so a surface names them and asks.
    shared_with: tuple[str, ...] = ()

    @property
    def key_users(self) -> str:
        """Every provider using the key, this one included (sorted, joined)."""

        return ", ".join(sorted((self.provider_id, *self.shared_with)))

    @property
    def replaces_shared(self) -> bool:
        """A saved key other providers use too: replacing it changes theirs."""

        return self.replaces and bool(self.shared_with)


def plan_set_key(runtime: Any, provider_id: str) -> KeyPlan:
    # The served sample comes first: every fact below (the provider and its
    # key name, the saved key, who else uses it) is read after it, so a
    # change that lands while the plan is made or shown refuses the apply.
    sample = served_sample(runtime)
    ctx = _cmd().operator_context(runtime)
    keyed = keyed_provider(runtime, provider_id, ctx)
    location = key_file_identity(runtime)
    store = store_of(runtime)
    length = _secret_length(store, keyed.secret_name)
    shared = key_sharers(ctx, keyed.secret_name, exclude=provider_id)
    served = preflight(runtime, f"providers set-key {provider_id}", assume_present=(keyed.secret_name,),
                       sample=sample)
    shown = key_file_display(runtime)
    if length is not None and shared:
        users = ", ".join(sorted((provider_id, *shared)))
        lines: tuple[str, ...] = (texts.KEY_SHARED_REPLACE_PROMPT.format(name=keyed.secret_name, n=length,
                                                                           providers=users).rstrip(),)
    elif length is not None:
        lines = (texts.KEY_REPLACE_PROMPT.format(display=keyed.display, n=length).rstrip(),)
    else:
        lines = (texts.KEY_PROMPT.format(display=keyed.display).rstrip(),)
    digest = model.digest_of("set-key", provider_id, keyed.secret_name, length is not None, length, list(location),
                             served.sample, list(shared))
    return KeyPlan("set-key", provider_id, lines, "confirm", True, digest, (shown,), provider_id, keyed.display,
                   keyed.secret_name, shown, length is not None, length, keyed.lines == 0, keyed.lines, location,
                   served, shared)


def check_value(value: model.Secret) -> None:
    if not len(value):
        raise model.Refused(texts.KEY_EMPTY)
    if not secret_store.VALUE_SHAPE.fullmatch(value.reveal()):
        raise model.Refused(texts.KEY_SHAPE)


class _PlannedStore:
    """The store a transaction writes, opened under its locks by ``require``."""

    def __init__(self, runtime: Any, planned: tuple[str, str]) -> None:
        self.runtime, self.planned = runtime, planned
        self.store: secret_store.FileSecretStore | None = None

    def open(self) -> secret_store.FileSecretStore:
        self.store = open_planned_store(self.runtime, self.planned)
        return self.store

    def get(self) -> secret_store.FileSecretStore:
        assert self.store is not None, "opened by the transaction's require step"
        return self.store


def apply_set_key(runtime: Any, plan: KeyPlan, confirmation: model.Confirmation, value: model.Secret) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb=f"providers set-key {plan.provider_id}")
    check_value(value)
    opened = _PlannedStore(runtime, plan.key_file)

    def require(fresh: Any) -> None:
        # The provider the key was planned for is still there and still
        # takes exactly that key: a provider removed, switched to its
        # account or given another key name meanwhile writes nothing.
        try:
            target = keyed_provider(runtime, plan.provider_id, fresh)
        except model.SetupError:
            target = None
        if target is None or target.secret_name != plan.secret_name:
            raise model.Stale(texts.KEY_TARGET_WHAT.format(id=plan.provider_id, name=plan.secret_name))
        store = opened.open()
        if _secret_length(store, plan.secret_name) != plan.current_length:
            raise model.Stale(f"the saved {plan.display} API key")
        # The providers named when the replacement was confirmed are still
        # exactly the ones using the key.
        if key_sharers(fresh, plan.secret_name, exclude=plan.provider_id) != plan.shared_with:
            raise model.Stale(texts.KEY_USERS_WHAT.format(name=plan.secret_name))

    def write(_fresh: Any, undo: model.UndoStack) -> None:
        set_secret(undo, opened.get(), plan.secret_name, value.reveal())

    committed = served_transaction(runtime, verb=f"providers set-key {plan.provider_id}", preflight=plan.served,
                                   write=write, require=require)
    reload = reload_text(runtime, committed.reload, display=plan.display, lines=plan.lines_count)
    return model.Applied("set-key", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.KEY_SAVED.format(display=plan.display, n=len(value),
                                                                       reload=reload)))


# ------------------------------------------------------------------ remove an API key


@dataclass(frozen=True)
class RemoveKeyPlan(model.Plan):
    provider_id: str
    display: str
    secret_name: str
    path_display: str
    affected_profiles: tuple[str, ...]
    serving_users: tuple[str, ...]
    current_length: int = 0
    key_file: tuple[str, str] = ("", "")  # (source, real path) the key is removed from
    served: Any = field(default=None, compare=False, repr=False)


_KEY_NAME = re.compile(r"^[A-Z0-9_]{1,128}$")


def retained_key(runtime: Any, provider_id: str, name: str) -> tuple[str, str]:
    """``(display, name)`` of the API key ``name`` of ``provider_id``,
    given by name: the key a provider you removed left behind (it was kept).
    Never guessed; refused when the name is another provider's key, or when
    ``provider_id`` still names a provider whose key is a different one."""

    if not _KEY_NAME.fullmatch(name or ""):
        raise model.Refused(texts.KEY_NAME_SHAPE)
    owners = provider_key_names(runtime)
    ctx = _cmd().operator_context(runtime, metadata_only=True)
    current = provider_id in ctx.docs["providers"]["providers"] or provider_id in ctx.layer.providers
    if current:
        display, own = _key_target(runtime, provider_id, ctx)
        if own != name:
            raise model.Refused(texts.KEY_NAME_NOT_ITS.format(id=provider_id, key=own, name=name))
        return display, own
    owner = owners.get(name)
    if owner is not None:
        raise model.Refused(texts.KEY_NAME_IN_USE.format(name=name, other=owner[0]))
    return provider_id, name


def _key_target(runtime: Any, provider_id: str, ctx: Any) -> tuple[str, str]:
    """(display, secret name) of the key ``remove-key`` removes."""

    alternative = _key_alternative(ctx, provider_id)
    if alternative is not None:
        names = transport_names(ctx.docs, provider_id)
        choice = ctx.ledger.transport_choices.get(provider_id) if ctx.ledger is not None else None
        if choice == operator_mod.TRANSPORT_API_KEY:
            raise model.Refused(texts.KEY_IN_USE.format(**names),
                                line=texts.KEY_IN_USE_LINE.format(**names, id=provider_id),
                                fix=model.Fix(f"Enter on {names['display']}",
                                              f"claude-multi providers transport {provider_id} oauth-pool"))
        # Not in use (or not offered by this build): a saved key can go.
        return names["display"], alternative.secret_name
    keyed = keyed_provider(runtime, provider_id, ctx)
    return keyed.display, keyed.secret_name


def plan_remove_key(runtime: Any, provider_id: str, *, secret: tuple[str, str] | None = None) -> RemoveKeyPlan:
    """The removal of a provider's saved key; ``secret`` ``(display, name)``
    names it directly (a provider you just removed)."""

    ctx = _cmd().operator_context(runtime)
    display, name = secret if secret is not None else _key_target(runtime, provider_id, ctx)
    location = key_file_identity(runtime)
    store = store_of(runtime)
    length = _secret_length(store, name)
    if length is None:
        raise model.Refused(texts.KEY_NOT_SET.format(display=display))
    served = preflight(runtime, f"providers remove-key {provider_id}", assume_absent=(name,))
    profiles = profiles_using(runtime, provider_id)
    users = _live_ids(runtime, provider_aliases(runtime, provider_id, ctx))
    shown = key_file_display(runtime)
    body = texts.REMOVE_KEY_BODY.format(name=name, path=shown, profiles_line=_profiles_line(display, profiles),
                                        display=display)
    digest = model.digest_of("remove-key", provider_id, name, length, list(location), served.sample,
                             list(profiles), list(users))
    return RemoveKeyPlan("remove-key", provider_id, tuple(body.split("\n")), "destructive", True, digest, (shown,),
                         provider_id, display, name, shown, profiles, users, length, location, served)


def apply_remove_key(runtime: Any, plan: RemoveKeyPlan, confirmation: model.Confirmation) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb=f"providers remove-key {plan.provider_id}")
    opened = _PlannedStore(runtime, plan.key_file)

    def require(_fresh: Any) -> None:
        store = opened.open()
        if _secret_length(store, plan.secret_name) != plan.current_length:
            raise model.Stale(f"the saved {plan.display} API key")

    def write(_fresh: Any, undo: model.UndoStack) -> None:
        delete_secret(undo, opened.get(), plan.secret_name)

    committed = served_transaction(runtime, verb=f"providers remove-key {plan.provider_id}", preflight=plan.served,
                                   write=write, require=require)
    return model.Applied("remove-key", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.KEY_REMOVED.format(
                             display=plan.display, reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ account or API-key transport


@dataclass(frozen=True)
class TransportPlan(model.Plan):
    provider_id: str
    to: str  # api-key | oauth-pool
    selectors: int
    origin: str | None
    header: str | None
    secret_name: str | None
    key_present: bool
    live_users: tuple[str, ...]
    previous: str = operator_mod.TRANSPORT_POOL
    key_file: tuple[str, str] = ("", "")  # (source, real path) the key is read from (or saved to)
    served: Any = field(default=None, compare=False, repr=False)


def current_transport(runtime: Any, provider_id: str, ctx: Any | None = None) -> str:
    ctx = ctx if ctx is not None else _cmd().operator_context(runtime)
    choice = ctx.ledger.transport_choices.get(provider_id) if ctx.ledger is not None else None
    return choice or operator_mod.TRANSPORT_POOL


def moved_selectors(served: Any, selectors: Iterable[str]) -> int:
    """How many served selectors a transport switch moves: the retargeted
    rows its served-change preview lists (the claude-multi-* aliases
    included), so the summary and the list agree; the provider's selector
    bases when the published render is unknown."""

    plan = getattr(served, "plan", None)
    if plan is not None and plan.before_unknown is None:
        return len(plan.retargeted)
    return len(frozenset(selectors))


def plan_transport(runtime: Any, provider_id: str, to: str) -> TransportPlan:
    cmd = _cmd()
    ctx = cmd.operator_context(runtime)
    if to not in operator_mod.TRANSPORT_CHOICES:
        raise model.Refused(f"unknown transport {to!r} ({', '.join(operator_mod.TRANSPORT_CHOICES)})")
    catalog_provider = ctx.docs["providers"]["providers"].get(provider_id)
    if catalog_provider is None or catalog_provider["transport"]["kind"] != operator_mod.TRANSPORT_POOL:
        raise model.Refused(operator_mod.transport_problem(ctx.docs, provider_id, operator_mod.TRANSPORT_POOL)
                            or f"{provider_id} has no account transport")
    problem = operator_mod.transport_problem(ctx.docs, provider_id, to)
    if problem is not None:
        raise model.Refused(problem)
    if ctx.snapshot.ledger_error is not None:
        raise model.Refused(f"{ctx.snapshot.ledger_error} — nothing changed")
    current = current_transport(runtime, provider_id, ctx)
    selection = operator_mod.transport_selections(ctx.docs, ctx.ledger).get(provider_id)
    if to == current and (to == operator_mod.TRANSPORT_POOL or (selection and selection.approved)):
        raise model.Refused(texts.TRANSPORT_ALREADY.format(id=provider_id, choice=to))
    alternative = operator_mod.transport_alternative(provider_id, to)
    location = key_file_identity(runtime)
    store = store_of(runtime)
    present = bool(alternative is not None and _secret_length(store, alternative.secret_name) is not None)
    selectors = cmd._transport_selectors(ctx, provider_id)
    users = _live_ids(runtime, selectors)
    stamp = operator_mod.utc_stamp()

    def planned(document: dict[str, Any]) -> None:
        if alternative is None:
            document["transport_choices"].pop(provider_id, None)
            document["routes"].pop(provider_id, None)
        else:
            document["transport_choices"][provider_id] = to
            document["routes"][provider_id] = operator_mod.transport_route_record(alternative, at=stamp)

    served = preflight(runtime, f"providers transport {provider_id} {to}", ledger_edit=planned,
                       assume_present=(alternative.secret_name,) if alternative is not None else ())
    live_line = texts.LIVE_LINE.format(ids=", ".join(users)) if users else ""
    names = transport_names(ctx.docs, provider_id)
    moving = moved_selectors(served, selectors)
    if alternative is not None:
        body = texts.TRANSPORT_TO_KEY_BODY.format(n=moving, origin=alternative.origin,
                                                  header=alternative.header or "Authorization", live_line=live_line,
                                                  **names)
        if alternative.explicit_models:
            reviewed = operator_mod.key_route_lines(ctx.docs, provider_id)
            body += "\n" + texts.TRANSPORT_KEY_REVIEWED.format(
                names=", ".join(reviewed), n=len(reviewed), total=line_count(runtime, provider_id, ctx), **names)
    else:
        body = texts.TRANSPORT_TO_ACCOUNT_BODY.format(n=moving, live_line=live_line, **names)
    lines = tuple(line for line in body.split("\n") if line)
    digest = model.digest_of("transport", provider_id, to, current, present, list(location), served.sample,
                             list(users))
    return TransportPlan("transport", provider_id, lines, "approve-route", True, digest, ("operator-ledger.json",),
                         provider_id, to, moving, alternative.origin if alternative else None,
                         alternative.header if alternative else None,
                         alternative.secret_name if alternative else None, present, users, current, location,
                         served)


def apply_transport(runtime: Any, plan: TransportPlan, confirmation: model.Confirmation,
                    value: model.Secret | None = None) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb=f"providers transport {plan.provider_id} {plan.to}")
    alternative = operator_mod.transport_alternative(plan.provider_id, plan.to)
    if alternative is not None and not plan.key_present:
        if value is None:
            raise model.Refused(texts.KEY_EMPTY)
        check_value(value)
    elif value is not None:
        check_value(value)
    opened = _PlannedStore(runtime, plan.key_file)

    def require(fresh: Any) -> None:
        if current_transport(runtime, plan.provider_id, fresh) != plan.previous:
            raise model.Stale(f"the {plan.provider_id} transport")
        if alternative is None:
            return  # back to the account: no key is read or written
        store = opened.open()
        if value is None and _secret_length(store, alternative.secret_name) is None:
            # The key the plan found is gone: never select a transport
            # without its key (the account route would stop serving).
            provider = fresh.docs["providers"]["providers"].get(plan.provider_id) or {}
            raise model.Stale(f"the saved {provider.get('display') or plan.provider_id} API key")

    def write(fresh: Any, undo: model.UndoStack) -> None:
        pid = plan.provider_id
        previous_choice = fresh.ledger.transport_choices.get(pid) if fresh.ledger is not None else None
        previous_route = fresh.ledger.routes.get(pid) if fresh.ledger is not None else None
        record = (operator_mod.transport_route_record(alternative, at=operator_mod.utc_stamp())
                  if alternative is not None else None)
        if value is not None and alternative is not None:
            # The key first: a selected transport never lacks its key.
            set_secret(undo, opened.get(), alternative.secret_name, value.reveal())

        def select(document: dict[str, Any]) -> None:
            if alternative is None:
                document["transport_choices"].pop(pid, None)
                document["routes"].pop(pid, None)
            else:
                document["transport_choices"][pid] = plan.to
                document["routes"][pid] = record

        def selected(document: Mapping[str, Any]) -> bool:
            if alternative is None:
                return pid not in document["transport_choices"] and pid not in document["routes"]
            return document["transport_choices"].get(pid) == plan.to and document["routes"].get(pid) == record

        def revert(document: dict[str, Any]) -> None:
            for name, item in (("transport_choices", previous_choice), ("routes", previous_route)):
                if item is None:
                    document[name].pop(pid, None)
                else:
                    document[name][pid] = item

        ledger_write(undo, fresh, select, selected, revert)

    committed = served_transaction(runtime, verb=f"providers transport {plan.provider_id} {plan.to}",
                                   preflight=plan.served, write=write, require=require)
    reload = reload_text(runtime, committed.reload)
    text = (texts.TRANSPORT_SWITCHED_KEY if alternative is not None else texts.TRANSPORT_SWITCHED_ACCOUNT)
    names = transport_names(runtime.catalog.docs, plan.provider_id)
    return model.Applied("transport", plan.provider_id, committed.reload,
                         _verb_lines(committed, text.format(reload=reload, **names)))


# ------------------------------------------------------------------ approve a route


@dataclass(frozen=True)
class ApprovePlan(model.Plan):
    provider_id: str
    origin: str
    auth_text: str
    secret_name: str | None
    key_present: bool
    listing: str | None
    changed: bool
    previous_origin: str | None
    route_digest: str = ""
    served: Any = field(default=None, compare=False, repr=False)


def plan_approve(runtime: Any, provider_id: str) -> ApprovePlan:
    cmd = _cmd()
    ctx = cmd.operator_context(runtime)
    if ctx.snapshot.ledger_error is not None:
        raise model.Refused(f"{ctx.snapshot.ledger_error} — nothing changed")
    provider = ctx.layer.providers.get(provider_id)
    if provider is None:
        found = ctx.layer.problems_by_file.get(operator_mod.file_label(provider_id), ())
        detail = ("\n  " + "\n  ".join(p.text() for p in found)) if found else ""
        raise model.Refused(f"no valid provider declaration {operator_mod.file_label(provider_id)}{detail}")
    if provider.auth_kind == "none":
        raise model.Refused(texts.APPROVE_KEYLESS.format(id=provider_id))
    status = ctx.layer.route_status.get(provider_id)
    if status == "approved":
        raise model.Refused(texts.APPROVE_ALREADY.format(id=provider_id, origin=provider.origin))
    previous = ctx.ledger.routes.get(provider_id) if ctx.ledger is not None else None
    previous_origin = previous.get("origin") if isinstance(previous, Mapping) else None
    present = _secret_length(store_of(runtime), provider.secret_name or "") is not None
    served = preflight(runtime, f"providers approve {provider_id}", ledger_edit=cmd._route_edit(provider))
    auth_text = texts.AUTH_TEXT.get(provider.auth_kind, provider.auth_kind)
    listing = provider.listing_origin
    body = texts.APPROVE_BODY.format(name=provider.secret_name, present=texts.APPROVE_PRESENT[present],
                                     origin=provider.origin, auth_text=auth_text, listing=listing or "none")
    lines = body.split("\n")
    changed = status == "changed"
    if changed and previous_origin:
        lines.append(texts.APPROVE_PREVIOUS.format(previous_origin=previous_origin))
    digest = model.digest_of("approve", provider_id, provider.route_digest, present, served.sample)
    return ApprovePlan("approve", provider_id, tuple(lines), "approve-route", True, digest,
                       ("operator-ledger.json",), provider_id, provider.origin, auth_text, provider.secret_name,
                       present, listing, changed, previous_origin, provider.route_digest, served)


def apply_approve(runtime: Any, plan: ApprovePlan, confirmation: model.Confirmation) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb=f"providers approve {plan.provider_id}")
    cmd = _cmd()

    def require(fresh: Any) -> None:
        now = fresh.layer.providers.get(plan.provider_id)
        if fresh.snapshot.ledger_error is not None or now is None or now.route_digest != plan.route_digest:
            raise model.Stale(f"the declaration of {plan.provider_id}")

    def write(fresh: Any, undo: model.UndoStack) -> None:
        grant_route(undo, fresh, fresh.layer.providers[plan.provider_id])

    committed = served_transaction(runtime, verb=f"providers approve {plan.provider_id}", preflight=plan.served,
                                   write=write, require=require)
    return model.Applied("approve", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.APPROVED.format(
                             id=plan.provider_id, origin=plan.origin, reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ apply


@dataclass(frozen=True)
class ApplyPlan(model.Plan):
    preview: tuple[str, ...]
    changes: bool
    served: Any = field(default=None, compare=False, repr=False)


def plan_apply(runtime: Any, *, candidate_environ: Mapping[str, str] | None = None,
               verb: str = "providers apply") -> ApplyPlan:
    """Render what is declared now; with ``candidate_environ``, what the
    render reads once that environment applies (another key file)."""

    served = preflight(runtime, verb, candidate_environ=candidate_environ)
    preview = tuple(served.plan.lines())
    lines = (*preview, texts.APPLY_BODY_TAIL)
    digest = model.digest_of("apply", served.sample, served.plan.digest)
    return ApplyPlan("apply", "gateway", lines, "confirm", False, digest, ("config.yaml",), preview,
                     served.plan.changed, served)


def apply_apply(runtime: Any, plan: ApplyPlan, confirmation: model.Confirmation) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb="providers apply")
    committed = served_transaction(runtime, verb="providers apply", preflight=plan.served,
                                   write=lambda _fresh, _undo: None)
    return model.Applied("apply", "gateway", committed.reload,
                         _verb_lines(committed, texts.APPLIED.format(reload=reload_text(runtime, committed.reload))))


def rerender(runtime: Any, verb: str) -> model.Applied:
    """Render and verify what is declared now (no plan shown): the reload
    after a change outside the served inputs, such as a sign-out."""

    plan = plan_apply(runtime)
    committed = served_transaction(runtime, verb=verb, preflight=plan.served, write=lambda _fresh, _undo: None)
    return model.Applied("apply", "gateway", committed.reload,
                         _verb_lines(committed, texts.APPLIED.format(reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ the API-key file


def _restore_pointer(env: Mapping[str, str], pointer: Any, previous: bytes | None) -> None:
    from claude_multi import state

    if previous is None:
        state.remove_private(pointer)
    else:
        state.atomic_write(pointer, previous)


def selected_environ(env: Mapping[str, str], target: Any) -> dict[str, str]:
    """The environment a render reads once ``target`` is the selected key
    file: an environment override still wins over the pointer; otherwise
    every key comes from ``target``."""

    if env.get(secret_store.SECRET_ENV_OVERRIDE):
        return dict(env)
    return {**env, secret_store.SECRET_ENV_OVERRIDE: str(target)}


def apply_key_file(runtime: Any, target: Any, *, verb: str = "setup --keys-file") -> model.Applied:
    """Select an existing key file (``secret-file.json``) and render with it,
    in one served change. It holds the same locks as every key save, so a
    key confirmed for one file is never written to another, and the pointer
    is put back exactly when the render refuses or is interrupted.

    The served change is planned against the selected file's keys (what
    the render will read), so a selection that would stop serving a model
    line is a removal like any other: refused while its live impact is
    unknown. It is planned again under the transaction's locks, before the
    pointer changes; a plan that moved refuses with nothing written."""

    env = runtime.gateway_environ()
    pointer = paths.secret_pointer_path(env)
    document = secret_store.pointer_document(target, env)
    try:
        secret_store.parse_pointer(document, env)
    except ValueError as exc:
        raise model.Refused(str(exc)) from exc
    wanted = strict_json.pretty_file_bytes(document)
    candidate = selected_environ(env, target)
    plan = plan_apply(runtime, candidate_environ=candidate, verb=verb)

    def require(_fresh: Any) -> None:
        again = preflight(runtime, verb, candidate_environ=candidate)
        if again.plan.digest != plan.served.plan.digest:
            raise model.Stale(texts.KEY_FILE_SELECTION_WHAT)

    def write(_fresh: Any, undo: model.UndoStack) -> None:
        previous = _file_bytes(pointer)  # a path, never a key
        guarded_write(undo, lambda: secret_store.write_pointer(env, target),
                      landed=lambda: _file_bytes(pointer) == wanted,
                      restore=lambda: _restore_pointer(env, pointer, previous))

    committed = served_transaction(runtime, verb=verb, preflight=plan.served, write=write, require=require)
    return model.Applied("keys-file", "gateway", committed.reload,
                         _verb_lines(committed, texts.APPLIED.format(reload=reload_text(runtime, committed.reload))))


def select_key_file(runtime: Any, target: Any, *, verb: str = "setup --keys-file") -> model.Applied:
    """Use an existing private key file for API keys: the one path that
    ``setup --keys-file`` and an answers file's ``keys_file`` share.

    It needs a person at the terminal. The file is checked, then selected
    with the gateway re-rendered and its reload verified
    (:func:`apply_key_file`); the lines say which file each reader now
    uses: an environment override still wins in this shell, and an
    installed gateway service reads the selected file."""

    from claude_multi.cli import consent
    from claude_multi.setup import external

    env = runtime.gateway_environ()
    consent.require_human(verb, env)
    try:
        names = secret_store.check_key_file(target, env)
    except (secret_store.SecretStoreError, OSError) as exc:
        raise _refusal(exc) from exc
    applied = apply_key_file(runtime, target, verb=verb)
    shown = paths.display(target, runtime.environ)
    lines = [texts.KEYS_FILE_SET.format(path=shown, n=len(names))]
    try:
        overridden = secret_store.key_file_location(env).source == "environment"
    except secret_store.SecretStoreError:
        overridden = False
    if overridden:
        lines.append(texts.KEYS_FILE_OVERRIDDEN)
    if external.service_installed(runtime):
        lines.append(texts.KEYS_FILE_SERVICE.format(path=shown))
    return model.Applied("keys-file", "gateway", applied.reload, (*lines, *applied.lines))


# ------------------------------------------------------------------ the gateway's outbound proxy

PROXY_VERB = "setup --step gateway"


@dataclass(frozen=True)
class ProxyPlan(model.Plan):
    proxy_url: str | None  # None: connect directly
    running: bool  # a running gateway re-renders with it
    served: Any = field(default=None, compare=False, repr=False)


def plan_proxy(runtime: Any, proxy_url: str | None, *, running: bool | None = None) -> ProxyPlan:
    """Set (with None: clear) the gateway's outbound proxy. A running
    gateway re-renders with it in one served change, so the preview's
    checks run before anything is written. ``running`` is the caller's own
    observation of the gateway, when it has one."""

    from claude_multi import endpoint
    from claude_multi.setup import external

    try:
        endpoint.check_proxy(proxy_url)
    except endpoint.EndpointError as exc:
        raise _refusal(exc) from exc
    if running is None:
        running = external.gateway_status(runtime).state == "running"
    served = preflight(runtime, PROXY_VERB) if running else None
    lines = (texts.PROXY_PLAN_SET.format(proxy=proxy_url) if proxy_url else texts.PROXY_PLAN_DIRECT,
             texts.PROXY_PLAN_RELOADS if running else texts.PROXY_PLAN_NEXT_START)
    facts = (served.sample, served.plan.digest) if served is not None else None
    digest = model.digest_of("proxy", proxy_url, running, facts)
    return ProxyPlan("proxy", "gateway", lines, "confirm", False, digest, ("endpoint.json",),
                     proxy_url, running, served)


PROXY_NOT_CHANGED = "the gateway's outbound proxy was not changed"


def apply_proxy(runtime: Any, plan: ProxyPlan, confirmation: model.Confirmation) -> model.Applied:
    """Record the proxy, then (for a running gateway) render and verify the
    reload. A refused render or an interrupt before the new configuration
    is published puts ``endpoint.json`` back exactly as it was and
    refreshes the runtime's view of it; a published configuration keeps
    the change (and says so).

    ``endpoint.json`` changes inside the writer fence of every state root
    that may own this home's gateway (the gateway inhibition is checked
    there), and so does putting it back. The undo is registered before the
    new document is written and restores only while the document still
    shows this change."""

    from claude_multi import endpoint, gateway_inhibition

    model.check_apply(runtime, plan, confirmation, verb=PROXY_VERB)
    try:
        packaged = endpoint.port_of(runtime.catalog.docs["gateway"]["gateway"]["base_url"])
    except (endpoint.EndpointError, KeyError, TypeError) as exc:
        raise _refusal(exc) from exc
    token = runtime.environ.get(gateway_inhibition.TOKEN_ENV) or None
    root = runtime.session_store.root

    def fence() -> Any:
        roots = gateway_inhibition.home_roots(runtime.home, root, what=PROXY_NOT_CHANGED)
        return gateway_inhibition.fenced(roots, token=token, what=PROXY_NOT_CHANGED, home=runtime.home)

    def write(_fresh: Any, undo: model.UndoStack) -> None:
        undo.push(runtime.reload_catalog)  # runs last: the view of the restored document

        def register(previous: bytes | None, updated: Any) -> None:
            def restore() -> None:
                with fence():
                    if endpoint.read_config(runtime.home) == updated:
                        endpoint.restore_bytes(runtime.home, previous)

            undo.push(restore)

        with fence():
            try:
                endpoint.swap_proxy(runtime.home, plan.proxy_url, packaged_port=packaged, before_write=register)
            except endpoint.EndpointError as exc:
                raise _refusal(exc) from exc
        runtime.reload_catalog()

    if not plan.running:
        # Nothing serves it yet: the next start renders it.
        undo = model.UndoStack()
        try:
            write(None, undo)
        except BaseException as exc:
            _undo_then_raise(undo, exc)
            raise
        return model.Applied("proxy", "gateway", "not-needed", ())
    committed = served_transaction(runtime, verb=PROXY_VERB, preflight=plan.served, write=write)
    return model.Applied("proxy", "gateway", committed.reload,
                         _verb_lines(committed, texts.APPLIED.format(reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ remove a provider you added


@dataclass(frozen=True)
class Blocker:
    kind: str  # admitted | bound-profile | bound-binding | live | unknown-liveness
    subject: str
    detail: str
    fix_tui: str
    fix_cli: str


def _blocker(kind: str, subject: str, detail: str = "", **fields: str) -> Blocker:
    tui, cli = texts.BLOCKER[kind]
    return Blocker(kind, subject, detail, tui.format(**fields), cli.format(**fields))


@dataclass(frozen=True)
class RemoveProviderPlan(model.Plan):
    provider_id: str
    file_display: str
    route_withdrawn: bool
    blockers: tuple[Blocker, ...]
    secret_name: str | None
    key_present: bool
    previous: bytes = field(default=b"", repr=False)
    served: Any = field(default=None, compare=False, repr=False)


def _provider_keys(ctx: Any, provider_id: str) -> set[str]:
    keys = {key for key, fid in operator_mod.line_file_ids(ctx.read).items() if fid == provider_id}
    keys |= {key for key, line in ctx.layer.lines.items() if line.provider_id == provider_id}
    return keys


def plan_remove_provider(runtime: Any, provider_id: str) -> RemoveProviderPlan:
    cmd = _cmd()
    ctx = cmd.operator_context(runtime)
    if ctx.snapshot.ledger_error is not None:
        raise model.Refused(f"{ctx.snapshot.ledger_error} — nothing changed")
    if provider_id in ctx.docs["providers"]["providers"]:
        raise model.Refused(texts.NOT_YOUR_PROVIDER.format(id=provider_id))
    previous = ctx.read.files.get(provider_id)
    target = operator_mod.providers_dir(ctx.env) / f"{provider_id}.json"
    if previous is None and not os.path.lexists(target):
        raise model.Refused(texts.NO_DECLARATION.format(id=provider_id))
    keys = _provider_keys(ctx, provider_id)
    admitted = set(cmd.admitted_keys(runtime)) | (set(ctx.ledger.admissions) if ctx.ledger is not None else set())
    blockers: list[Blocker] = [_blocker("admitted", key, key=key) for key in sorted(keys & admitted)]
    references = cmd.key_references(runtime)
    for key in sorted(keys):
        by_profile: dict[str, list[str]] = {}
        for user in references.get(key, ()):
            kind, _space, rest = user.partition(" ")
            if kind == "profile":
                name, _space, slot = rest.partition(" ")
                by_profile.setdefault(name, []).append(slot)
            else:
                blockers.append(_blocker("bound-binding", key, b=rest, key=key))
        for name, slots in sorted(by_profile.items()):
            blockers.append(_blocker("bound-profile", key, p=name, key=key, slots=", ".join(slots)))
    scan = cmd.record_scan(runtime)
    if scan.directory_error is not None or scan.unreadable:
        for stem in sorted(scan.unreadable) or ["records"]:
            blockers.append(_blocker("unknown-liveness", stem, id8=stem[:8]))
    aliases = {alias for key in keys if key in ctx.layer.lines
               for alias in operator_mod.line_aliases(ctx.layer.lines[key].core_entry)}
    if ctx.ledger is not None:
        aliases |= {alias for alias, capture in ctx.ledger.aliases.items() if capture["provider"] == provider_id}
    for user in sorted(cmd.live_users(scan, aliases)):
        blockers.append(_blocker("live", user, id8=user[:8]))
    provider = ctx.layer.providers.get(provider_id)
    secret_name = provider.secret_name if provider is not None else None
    present = bool(secret_name) and _secret_length(store_of(runtime), secret_name or "") is not None
    route = ctx.ledger.routes.get(provider_id) if ctx.ledger is not None else None
    served = None
    if not blockers:
        served = preflight(runtime, f"providers rm {provider_id}", changes={provider_id: None},
                           ledger_edit=lambda doc: doc["routes"].pop(provider_id, None))
    file_display = operator_mod.file_label(provider_id)
    lines = ((texts.REMOVE_PROVIDER_BODY.format(file=file_display),) if not blockers
             else tuple(blocker.fix_tui for blocker in blockers))
    digest = model.digest_of("remove-provider", provider_id, strict_json.sha256_hex(previous or b""),
                             route is not None, [(b.kind, b.subject) for b in blockers],
                             served.sample if served is not None else None)
    return RemoveProviderPlan("remove-provider", provider_id, lines, "destructive", False, digest, (file_display,),
                              provider_id, file_display, route is not None, tuple(blockers), secret_name, present,
                              previous or b"", served)


def apply_remove_provider(runtime: Any, plan: RemoveProviderPlan, confirmation: model.Confirmation) -> model.Applied:
    if plan.blockers:
        raise model.Refused(texts.REMOVE_PROVIDER_REFUSED.format(
            fixes="; ".join(blocker.fix_cli for blocker in plan.blockers)))
    model.check_apply(runtime, plan, confirmation, verb=f"providers rm {plan.provider_id}")
    cmd = _cmd()

    def require(fresh: Any) -> None:
        if fresh.read.files.get(plan.provider_id, b"") != plan.previous:
            raise model.Stale(operator_mod.file_label(plan.provider_id))

    def write(fresh: Any, undo: model.UndoStack) -> None:
        pid = plan.provider_id
        previous = fresh.read.files.get(pid)
        if previous is not None:
            remove_declaration(undo, fresh, pid, previous)
        else:
            operator_mod.remove_provider_file(fresh.env, pid)  # an unreadable leftover: nothing to put back
        route = fresh.ledger.routes.get(pid) if fresh.ledger is not None else None
        if route is not None:
            ledger_write(undo, fresh, lambda doc: doc["routes"].pop(pid, None),
                         lambda doc: pid not in doc["routes"], lambda doc: doc["routes"].__setitem__(pid, route))

    committed = served_transaction(runtime, verb=f"providers rm {plan.provider_id}", preflight=plan.served,
                                   write=write, require=require)
    return model.Applied("remove-provider", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.PROVIDER_REMOVED.format(
                             id=plan.provider_id, reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ your own endpoint


_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def derived_secret_name(provider_id: str) -> str:
    """``<ID>_API_KEY``, or ``USER_<ID>_API_KEY`` when that name is reserved."""

    base = provider_id.upper().replace("-", "_")
    name = f"{base}_API_KEY"
    if secret_store.secret_name_problem(name) is None:
        return name
    return f"USER_{base}_API_KEY"


# How a new provider made from a preset takes its API key when another
# provider already uses the preset's key name: its own key under its own
# name (the default), the saved key shared, or the shared key replaced.
KEY_OWN, KEY_REUSE, KEY_REPLACE = "own", "reuse", "replace"
KEY_USES = (KEY_OWN, KEY_REUSE, KEY_REPLACE)


def key_sharers(ctx: Any, name: str, *, exclude: str = "") -> tuple[str, ...]:
    """Every provider whose API key is ``name`` (sorted ids): a shipped
    provider's key, an account provider's API key (reserved whether or not
    this build offers it), the providers you added and every provider of
    custom.json still in effect (the merged view decides which: none once
    custom.json was migrated, never one the operator layer dropped);
    ``exclude`` is the provider being added."""

    found: set[str] = set()
    merged = operator_mod.merge_docs(ctx.docs, ctx.layer, legacy=ctx.legacy)
    for pid, provider in merged["providers"]["providers"].items():
        if ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref") == f"env:{name}":
            found.add(pid)
        alternative = operator_mod.transport_alternative(pid, operator_mod.TRANSPORT_API_KEY)
        if alternative is not None and alternative.secret_name == name:
            found.add(pid)
    found.update(pid for pid, provider in ctx.layer.providers.items() if provider.secret_name == name)
    found.discard(exclude)
    return tuple(sorted(found))


def instance_secret_name(preset_secret: str, provider_id: str) -> str:
    """The key name of one more provider made from a preset whose key name
    is in use: the preset's name with the provider's own suffix
    (``DASHSCOPE_API_KEY`` and ``studio-2`` → ``DASHSCOPE_API_KEY_STUDIO_2``)."""

    name = f"{preset_secret}_{provider_id.upper().replace('-', '_')}"
    problem = secret_store.secret_name_problem(name)
    if problem is not None:
        raise model.Refused(texts.PRESET_KEY_NAME_PROBLEM.format(id=provider_id, problem=problem))
    return name


@dataclass(frozen=True)
class EndpointPlan(model.Plan):
    provider_id: str
    kind: str
    base_url: str
    origin: str
    auth: str
    family: str
    secret_name: str
    listing: str | None
    document_bytes: bytes
    route_digest: str = ""
    key_file: tuple[str, str] = ("", "")  # (source, real path) a key typed with it goes to
    served: Any = field(default=None, compare=False, repr=False)
    preset: str = ""  # the reviewed preset it was made from, if any
    # The key saved as ``secret_name`` now (presence and length only).
    key_present: bool = False
    key_length: int | None = None
    # How the key is taken (KEY_OWN, KEY_REUSE, KEY_REPLACE) and the other
    # providers whose key is ``secret_name`` (only when it is shared).
    key_use: str = KEY_OWN
    shared_with: tuple[str, ...] = ()
    # The preset's own key name and the providers already using it (what a
    # surface offers: reusing or replacing that key instead of a new one).
    preset_secret: str = ""
    preset_sharers: tuple[str, ...] = ()


def _new_declaration_checks(runtime: Any, ctx: Any, provider_id: str) -> None:
    if ctx.snapshot.ledger_error is not None:
        raise model.Refused(f"{ctx.snapshot.ledger_error} — nothing changed")
    if not _PROVIDER_ID.fullmatch(provider_id or ""):
        raise model.Refused("a provider name uses a–z, 0–9 and -, starting with a letter")
    if provider_id in ctx.docs["providers"]["providers"]:
        raise model.Refused(f"{provider_id} ships with claude-multi — choose another name")
    target = operator_mod.providers_dir(ctx.env) / f"{provider_id}.json"
    if provider_id in ctx.read.files or os.path.lexists(target):
        raise model.Refused(f"{operator_mod.file_label(provider_id)} exists — choose another name, or edit it: "
                            f"claude-multi providers edit {provider_id}")


def _layer_problems(cmd: Any, ctx: Any, provider_id: str, raw: bytes) -> Any:
    layer = cmd.proposed(ctx, {provider_id: raw})
    own = operator_mod.file_problems(layer, provider_id)
    if own:
        raise model.Refused("not declared:\n  " + "\n  ".join(p.text() for p in own))
    blocking = operator_mod.blocking_problems(layer)
    if blocking:
        raise model.Refused("the providers you added would be invalid — nothing written:\n  "
                            + "\n  ".join(problem.text() for problem in blocking[:8]))
    return layer


def plan_add_endpoint(runtime: Any, values: Mapping[str, str]) -> EndpointPlan:
    cmd = _cmd()
    provider_id = str(values.get("id") or "").strip()
    kind = str(values.get("kind") or "anthropic-compatible")
    if kind not in ("anthropic-compatible", "openai-compatible"):
        raise model.Refused(f"unknown endpoint kind {kind!r}")
    if kind == "openai-compatible" and not catalog.keyed_compat_audited(runtime.catalog.docs.get("gateway")):
        raise model.Refused(texts.OPENAI_COMPAT_CLOSED)
    auth = str(values.get("auth") or ("header" if kind == "anthropic-compatible" else "bearer"))
    if auth not in ("header", "bearer"):
        raise model.Refused("the key is sent as the x-api-key header or as Authorization: Bearer")
    base_url = str(values.get("url") or "").strip()
    family = str(values.get("family") or "unknown").strip() or "unknown"
    listing = str(values.get("listing") or "").strip() or None
    ctx = cmd.operator_context(runtime)
    _new_declaration_checks(runtime, ctx, provider_id)
    secret_name = derived_secret_name(provider_id)
    auth_block: dict[str, Any] = {"kind": auth, "secret_ref": f"env:{secret_name}"}
    if auth == "header":
        auth_block["header"] = operator_mod.MIGRATION_HEADER
    block: dict[str, Any] = {"display": provider_id, "kind": kind, "base_url": base_url, "auth": auth_block,
                             "independence_family": family}
    if listing is not None:
        block["listing"] = {"url": listing, "shape": "anthropic" if kind == "anthropic-compatible" else "openai",
                            "auth": "provider"}
    raw = operator_mod.document_bytes({"version": operator_mod.DATA_VERSION, "provider": block, "lines": {}})
    return _keyed_declaration_plan(runtime, cmd, ctx, provider_id, raw, kind=kind, base_url=base_url, auth=auth,
                                   family=family, secret_name=secret_name, listing=listing)


def _keyed_declaration_plan(runtime: Any, cmd: Any, ctx: Any, provider_id: str, raw: bytes, *, kind: str,
                            base_url: str, auth: str, family: str, secret_name: str, listing: str | None,
                            preset: str = "", head: str = "", key_use: str = KEY_OWN, preset_secret: str = "",
                            preset_sharers: tuple[str, ...] = ()) -> EndpointPlan:
    """The plan of a new keyed declaration (your own endpoint or a preset):
    the layer checks, the served-change preview with its route approval,
    the key saved under its name now (presence and length) and who else
    uses that key, and the preview text (``head`` first when given). A key
    name another provider uses is refused unless the plan shares it on
    purpose (``key_use`` reuse or replace)."""

    shared = key_sharers(ctx, secret_name, exclude=provider_id)
    if shared and key_use == KEY_OWN:
        raise model.Refused(texts.KEY_NAME_TAKEN.format(name=secret_name, providers=", ".join(shared)))
    layer = _layer_problems(cmd, ctx, provider_id, raw)
    resolved = layer.providers[provider_id]
    length = _secret_length(store_of(runtime), secret_name)
    if key_use == KEY_REUSE and length is None:
        raise model.Refused(texts.PRESET_KEY_NOTHING_TO_REUSE.format(name=secret_name, id=provider_id))
    served = preflight(runtime, f"providers add {provider_id}", changes={provider_id: raw},
                       ledger_edit=cmd._route_edit(resolved), assume_present=(secret_name,))
    host = resolved.origin.split("://", 1)[-1]
    location = key_file_identity(runtime)
    preview = texts.ENDPOINT_PREVIEW.format(id=provider_id, kind_label=texts.KIND_LABELS[kind], base_url=base_url,
                                            secret_name=secret_name, auth_text=texts.AUTH_TEXT[auth],
                                            origin=resolved.origin, family=family, host=host)
    lines = ((head,) if head else ()) + tuple(preview.split("\n")) + _key_use_lines(
        provider_id, secret_name, key_use, shared, preset_secret, preset_sharers)
    digest = model.digest_of("add-endpoint", provider_id, strict_json.sha256_hex(raw), resolved.route_digest,
                             list(location), served.sample, preset, key_use, secret_name, length, list(shared),
                             preset_secret, list(preset_sharers))
    return EndpointPlan("add-endpoint", provider_id, lines, "approve-route", True, digest,
                        (operator_mod.file_label(provider_id), "operator-ledger.json"), provider_id, kind,
                        resolved.base_url, resolved.origin, auth, family, secret_name, listing, raw,
                        resolved.route_digest, location, served=served, preset=preset,
                        key_present=length is not None, key_length=length, key_use=key_use, shared_with=shared,
                        preset_secret=preset_secret, preset_sharers=preset_sharers)


def _key_use_lines(provider_id: str, secret_name: str, key_use: str, shared: Sequence[str], preset_secret: str,
                   preset_sharers: Sequence[str]) -> tuple[str, ...]:
    """What the preview says about a preset key another provider uses."""

    if key_use == KEY_REUSE:
        return (texts.PRESET_KEY_REUSE.format(id=provider_id, name=secret_name, providers=", ".join(shared)),)
    if key_use == KEY_REPLACE:
        return (texts.PRESET_KEY_REPLACE.format(name=secret_name, providers=", ".join(shared)),)
    if preset_sharers and secret_name != preset_secret:
        return (texts.PRESET_KEY_OWN.format(id=provider_id, name=secret_name, preset_name=preset_secret,
                                            providers=", ".join(preset_sharers)),)
    return ()


def apply_add_endpoint(runtime: Any, plan: EndpointPlan, confirmation: model.Confirmation,
                       value: model.Secret | None) -> model.Applied:
    model.check_apply(runtime, plan, confirmation, verb=f"providers add {plan.provider_id}")
    if value is not None:
        check_value(value)
    if plan.key_use == KEY_REUSE and value is not None:
        raise model.Refused(texts.PRESET_KEY_REUSE_TAKES_NO_KEY)
    if plan.key_use == KEY_REPLACE and value is None:
        raise model.Refused(texts.PRESET_KEY_REPLACE_NEEDS_KEY)
    cmd = _cmd()
    opened = _PlannedStore(runtime, plan.key_file)
    target = operator_mod.providers_dir(runtime.gateway_environ()) / f"{plan.provider_id}.json"

    def require(fresh: Any) -> None:
        if plan.provider_id in fresh.read.files or os.path.lexists(target):
            raise model.Stale(operator_mod.file_label(plan.provider_id))
        if key_sharers(fresh, plan.secret_name, exclude=plan.provider_id) != plan.shared_with:
            raise model.Stale(texts.KEY_USERS_WHAT.format(name=plan.secret_name))
        if value is not None or plan.key_use == KEY_REUSE:
            # The key a new one replaces (or the one it shares) is still the one planned with.
            if _secret_length(opened.open(), plan.secret_name) != plan.key_length:
                raise model.Stale(texts.KEY_SAVED_WHAT.format(name=plan.secret_name))

    def write(fresh: Any, undo: model.UndoStack) -> None:
        write_declaration(undo, fresh, plan.provider_id, plan.document_bytes)
        now = cmd.proposed(fresh, {plan.provider_id: plan.document_bytes}).providers.get(plan.provider_id)
        if now is None or now.route_digest != plan.route_digest:
            raise model.Stale(f"the declaration of {plan.provider_id}")
        grant_route(undo, fresh, now)
        if value is not None:
            set_secret(undo, opened.get(), plan.secret_name, value.reveal())

    committed = served_transaction(runtime, verb=f"providers add {plan.provider_id}", preflight=plan.served,
                                   write=write, require=require)
    return model.Applied("add-endpoint", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.ENDPOINT_ADDED.format(
                             id=plan.provider_id, reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ a server on your network


@dataclass(frozen=True)
class PresetPlan(model.Plan):
    preset: str
    provider_id: str
    base_url: str
    document_bytes: bytes
    served: Any = field(default=None, compare=False, repr=False)


def plan_add_preset(runtime: Any, preset: str, provider_id: str, base_url: str | None, *,
                    key: str = KEY_OWN) -> PresetPlan | EndpointPlan:
    """A reviewed preset as a new provider named ``provider_id``: a keyless
    server is a :class:`PresetPlan` (declare only); a preset with an API key
    is the :class:`EndpointPlan` of your own endpoint with the preset's
    kind, address, key name, family and model list (its route is approved
    and its key typed with it). An OpenAI-compatible preset is refused
    while that route is closed, like the endpoint form.

    When another provider already uses the preset's key name, ``key``
    decides: ``own`` (the default) gives the new provider its own key under
    :func:`instance_secret_name`, so the saved key is never touched;
    ``reuse`` shares the saved key (no key is typed); ``replace`` shares the
    name and the typed key replaces the saved one for every provider in
    ``shared_with`` (a surface confirms that first, naming them)."""

    cmd = _cmd()
    try:
        raw_preset = operator_mod.load_preset(preset, runtime.asset_root)
        document = operator_mod.preset_document(raw_preset, base_url=base_url or None)
    except operator_mod.OperatorError as exc:
        raise _refusal(exc) from exc
    block = document.get("provider")
    auth = block.get("auth") if isinstance(block, Mapping) else None
    if not isinstance(block, Mapping) or not isinstance(auth, Mapping):
        raise model.Refused(f"preset {preset} declares no provider")
    if key not in KEY_USES:
        raise model.Refused(f"unknown key choice {key!r}")
    if auth.get("kind") != "none":
        return _plan_keyed_preset(runtime, cmd, preset, provider_id, document, key=key)
    ctx = cmd.operator_context(runtime)
    _new_declaration_checks(runtime, ctx, provider_id)
    raw = operator_mod.document_bytes(document)
    _layer_problems(cmd, ctx, provider_id, raw)
    served = preflight(runtime, f"providers add --preset {preset}", changes={provider_id: raw})
    url = str(block.get("base_url"))
    digest = model.digest_of("add-preset", preset, provider_id, strict_json.sha256_hex(raw), served.sample)
    return PresetPlan("add-preset", provider_id, (texts.LAN_PREVIEW.format(id=provider_id, base_url=url),),
                      "confirm", False, digest, (operator_mod.file_label(provider_id),), preset, provider_id, url,
                      raw, served)


def preset_key_name(ctx: Any, provider_id: str, preset_secret: str, key: str) -> tuple[str, tuple[str, ...]]:
    """``(the key name the new provider uses, the providers already using
    the preset's key name)``: the preset's name while no other provider
    uses it; then its own name (``own``) or the shared one (``reuse``,
    ``replace``). Asking to share a name nobody uses is refused."""

    sharers = key_sharers(ctx, preset_secret, exclude=provider_id)
    if key != KEY_OWN and not sharers:
        raise model.Refused(texts.PRESET_KEY_NOT_SHARED.format(name=preset_secret, id=provider_id))
    if key != KEY_OWN or not sharers:
        return preset_secret, sharers
    return instance_secret_name(preset_secret, provider_id), sharers


def with_secret_name(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    """``document`` (a preset's declaration) with its key named ``name``."""

    block = document["provider"]
    return {**document, "provider": {**block, "auth": {**block["auth"], "secret_ref": f"env:{name}"}}}


def _plan_keyed_preset(runtime: Any, cmd: Any, preset: str, provider_id: str,
                       document: Mapping[str, Any], *, key: str = KEY_OWN) -> EndpointPlan:
    block = document["provider"]
    kind = str(block.get("kind"))
    if kind == operator_mod.KEYED_KIND and not catalog.keyed_compat_audited(runtime.catalog.docs.get("gateway")):
        raise model.Refused(texts.OPENAI_COMPAT_CLOSED)
    if kind not in texts.KIND_LABELS or kind == operator_mod.LAN_KIND:
        raise model.Refused(f"preset {preset} has an unknown kind {kind!r}")
    auth = block["auth"]
    ctx = cmd.operator_context(runtime)
    _new_declaration_checks(runtime, ctx, provider_id)
    preset_secret = str(auth.get("secret_ref") or "").removeprefix("env:")
    secret_name, sharers = preset_key_name(ctx, provider_id, preset_secret, key)
    if secret_name != preset_secret:
        document = with_secret_name(document, secret_name)
    listing = block.get("listing")
    listing_url = listing.get("url") if isinstance(listing, Mapping) else None
    raw = operator_mod.document_bytes(document)
    head = texts.PRESET_PREVIEW_HEAD.format(display=block.get("display") or preset)
    return _keyed_declaration_plan(runtime, cmd, ctx, provider_id, raw, kind=kind,
                                   base_url=str(block.get("base_url")), auth=str(auth.get("kind")),
                                   family=str(block.get("independence_family") or operator_mod.UNKNOWN_FAMILY),
                                   secret_name=secret_name, listing=listing_url, preset=preset, head=head,
                                   key_use=key, preset_secret=preset_secret, preset_sharers=sharers)


def apply_add_preset(runtime: Any, plan: PresetPlan | EndpointPlan, confirmation: model.Confirmation,
                     value: model.Secret | None = None) -> model.Applied:
    """Declare what :func:`plan_add_preset` planned (a preset with an API key
    is added like your own endpoint: its route approved, ``value`` stored
    as its key when given)."""

    if isinstance(plan, EndpointPlan):
        return apply_add_endpoint(runtime, plan, confirmation, value)
    model.check_apply(runtime, plan, confirmation, verb=f"providers add --preset {plan.preset}")
    cmd = _cmd()
    target = operator_mod.providers_dir(runtime.gateway_environ()) / f"{plan.provider_id}.json"

    def require(fresh: Any) -> None:
        if plan.provider_id in fresh.read.files or os.path.lexists(target):
            raise model.Stale(operator_mod.file_label(plan.provider_id))

    def write(fresh: Any, undo: model.UndoStack) -> None:
        write_declaration(undo, fresh, plan.provider_id, plan.document_bytes)

    committed = served_transaction(runtime, verb=f"providers add --preset {plan.preset}", preflight=plan.served,
                                   write=write, require=require)
    return model.Applied("add-preset", plan.provider_id, committed.reload,
                         _verb_lines(committed, texts.ENDPOINT_ADDED.format(
                             id=plan.provider_id, reload=reload_text(runtime, committed.reload))))


# ------------------------------------------------------------------ on and off


@dataclass(frozen=True)
class TogglePlan(model.Plan):
    provider_id: str
    enabled: bool
    was: bool | None
    served: Any = field(default=None, compare=False, repr=False)


def plan_toggle(runtime: Any, provider_id: str, enabled: bool) -> TogglePlan:
    ctx = _cmd().operator_context(runtime)
    if provider_id not in ctx.docs["providers"]["providers"] and provider_id not in ctx.layer.providers:
        raise model.Refused(texts.KEY_UNKNOWN_PROVIDER.format(id=provider_id))
    try:
        eff = runtime.current_effective()
        was: bool | None = settings_mod.provider_enabled(eff, provider_id)
    except errors.ClaudeMultiError:
        was = None
    state = "enabled" if enabled else "disabled"
    if was is not None and was == enabled:
        raise model.Refused(texts.TOGGLE_ALREADY.format(id=provider_id, state=state))
    served = preflight(runtime, f"provider {provider_id} {'on' if enabled else 'off'}",
                       provider_enabled={provider_id: bool(enabled)})
    digest = model.digest_of("toggle", provider_id, enabled, was, served.sample)
    return TogglePlan("toggle", provider_id, (texts.TOGGLED.format(id=provider_id, state=state),), None, False,
                      digest, ("settings.json",), provider_id, enabled, was, served)


def apply_toggle(runtime: Any, plan: TogglePlan, confirmation: model.Confirmation) -> model.Applied:
    """The Settings write inside one served-change phase (nothing served
    changes: the provider's aliases stay rendered for running sessions)."""

    model.check_apply(runtime, plan, confirmation, verb=f"providers {'enable' if plan.enabled else 'disable'}")
    cmd = _cmd()
    from claude_multi import custom

    try:
        with cmd.operator_write(runtime, preflight=plan.served):
            runtime.settings_store.set_provider_enabled(
                plan.provider_id, bool(plan.enabled), catalog=runtime.lineup_catalog(),
                custom_registry=custom.load_registry(runtime.environ))
    except cmd.OperatorCommandError as exc:
        raise _refusal(exc) from exc
    return model.Applied("toggle", plan.provider_id, "not-needed", plan.lines)


# ------------------------------------------------------------------ the provider picker


@dataclass(frozen=True)
class PickerEntry:
    id: str
    group: str
    group_title: str
    family: str
    label: str
    kind: str  # account | api-key | preset | own | endpoint | lan
    provider_id: str | None
    state_text: str
    note: str
    available: bool
    current: bool


def picker_entries(runtime: Any, *, only: str | None = None) -> tuple[PickerEntry, ...]:
    """Every way to connect, in the picker's order: Anthropic, OpenAI and
    OpenRouter, the other shipped providers, the reviewed presets of vendors
    with an API key (kind ``preset``), the providers you added, then
    "Other" (your own endpoint and the servers on your network, the
    reviewed server presets included). ``only="other"`` is adding a new
    provider of your own (Providers N): the presets with an API key, then
    "Other"."""

    from claude_multi.setup import signin, status

    entries: list[PickerEntry] = []
    connections = {item.provider_id: item for item in status.connections(runtime)}
    docs = runtime.catalog.docs["providers"]["providers"]
    ctx = _cmd().operator_context(runtime)
    presets = operator_mod.presets(runtime.asset_root)
    order = [pid for pid in _FIRST if pid in docs] + [pid for pid in docs if pid not in _FIRST]
    if only is None:
        for pid in order:
            provider = docs[pid]
            transport = provider["transport"]
            display = str(provider.get("display") or pid)
            family = (texts.FAMILY_PER_MODEL if pid in catalog.AGGREGATOR_PROVIDERS
                      else str(provider.get("independence_family") or ""))
            conn = connections.get(pid)
            if transport["kind"] == operator_mod.TRANSPORT_POOL:
                pool = transport["pool"]
                using_key = conn is not None and conn.transport == "api-key"
                account_state = (texts.STATE_TEXT["signed-in"] if conn is not None and conn.accounts
                                 else texts.STATE_TEXT["not-signed-in"])
                offered = signin.pool_offered(pool)
                entries.append(PickerEntry(
                    f"{pid}:account", pid, display, family, texts.PICKER_LABELS[f"{pid}:account"]
                    if f"{pid}:account" in texts.PICKER_LABELS else f"{display} account",
                    "account", pid, account_state + ("" if using_key else f" · {texts.IN_USE}"),
                    "" if offered else texts.SIGNIN_CLOSED_NOTE, offered, not using_key))
                key_label = texts.PICKER_LABELS.get(f"{pid}:api-key", f"{display} API key")
                alternative = operator_mod.transport_alternative(pid, operator_mod.TRANSPORT_API_KEY)
                if alternative is None:
                    continue
                names = transport_names(runtime.catalog.docs, pid)
                problem = operator_mod.transport_problem(runtime.catalog.docs, pid, operator_mod.TRANSPORT_API_KEY)
                if problem is None or using_key:
                    present = _key_present(runtime, alternative.secret_name)
                    entries.append(PickerEntry(
                        f"{pid}:api-key", pid, display, family, key_label, "api-key", pid,
                        (texts.STATE_TEXT["key-set"] if present else texts.STATE_TEXT["key-missing"])
                        + (f" · {texts.IN_USE}" if using_key else ""), "", True, using_key))
                else:
                    note = (texts.KEY_CLOSED_NOTE.format(**names) if not alternative.available
                            else texts.KEY_ROUTE_NO_MODELS_NOTE)
                    entries.append(PickerEntry(f"{pid}:api-key", pid, display, family,
                                               texts.PICKER_KEY_CLOSED_LABEL.format(**names), "api-key", pid,
                                               note, note, False, False))
                continue
            ref = ((transport.get("auth") or {}).get("secret_ref"))
            if not isinstance(ref, str):
                continue  # a keyless shipped server needs no connection step
            lines = line_count(runtime, pid, ctx)
            label = texts.PICKER_LABELS.get(f"{pid}:api-key") or (
                texts.PICKER_LABELS["api-key"] if lines else texts.PICKER_LABELS["api-key-no-models"]).format(
                    display=display)
            state = texts.STATE_TEXT.get(conn.state, conn.state) if conn is not None else texts.STATE_TEXT["key-missing"]
            if conn is not None and conn.state == "connected":
                state = texts.STATE_TEXT["key-set"]
            entries.append(PickerEntry(f"{pid}:api-key", pid, display, family, label, "api-key", pid, state,
                                       "" if lines else texts.NO_MODELS_NOTE, True, False))
        entries.extend(_keyed_preset_entries(runtime, presets))
        for pid in sorted(ctx.layer.providers):
            provider = ctx.layer.providers[pid]
            display = str(provider.entry.get("display") or pid)
            conn = connections.get(pid)
            entries.append(PickerEntry(
                f"own:{pid}", f"own:{pid}", display, str(provider.entry.get("independence_family") or ""),
                texts.PICKER_LABELS["own"].format(display=display,
                                                  kind_label=texts.KIND_LABELS.get(provider.kind, provider.kind)),
                "own", pid, texts.STATE_TEXT.get(conn.state, conn.state) if conn is not None else "", "", True,
                False))
    else:
        # A new provider of your own: a reviewed preset fills in the same form.
        entries.extend(_keyed_preset_entries(runtime, presets))
    audited = catalog.keyed_compat_audited(runtime.catalog.docs.get("gateway"))
    entries.append(PickerEntry("other:anthropic-compatible", "other", texts.PICKER_OTHER, "",
                               texts.PICKER_LABELS["other:anthropic-compatible"], "endpoint", None, "", "", True,
                               False))
    if audited:
        entries.append(PickerEntry("other:openai-compatible", "other", texts.PICKER_OTHER, "",
                                   texts.PICKER_LABELS["other:openai-compatible"], "endpoint", None, "", "", True,
                                   False))
    else:
        entries.append(PickerEntry("other:openai-compatible", "other", texts.PICKER_OTHER, "",
                                   texts.OPENAI_COMPAT_CLOSED_LABEL, "endpoint", None, "",
                                   texts.OPENAI_COMPAT_CLOSED_NOTE, False, False))
    lan = [info for info in presets.values() if not info.keyed]
    for info in sorted(lan, key=lambda item: (not item.generic, item.display.lower(), item.name)):
        label = (texts.PICKER_LABELS["other:lan"] if info.generic
                 else texts.PICKER_LABELS["preset:lan"].format(display=info.display))
        entries.append(PickerEntry(f"other:lan:{info.name}", "other", texts.PICKER_OTHER, "", label, "lan", None,
                                   "" if info.generic else texts.PRESET_STATE, "", True, False))
    return tuple(entries)


def _keyed_preset_entries(runtime: Any, presets: Mapping[str, Any]) -> list[PickerEntry]:
    """One entry per reviewed preset with an API key, each its own group:
    Anthropic-compatible first, then OpenAI-compatible, which stays
    unavailable (with the closed route's note) until that route is open."""

    audited = catalog.keyed_compat_audited(runtime.catalog.docs.get("gateway"))
    order = {"anthropic-compatible": 0, operator_mod.KEYED_KIND: 1}
    keyed = [info for info in presets.values() if info.keyed and info.kind in order]
    found: list[PickerEntry] = []
    for info in sorted(keyed, key=lambda item: (order[item.kind], item.display.lower(), item.name)):
        family = "" if info.family == operator_mod.UNKNOWN_FAMILY else info.family
        kind_text = texts.PRESET_KIND_TEXT[info.kind]
        if info.kind == operator_mod.KEYED_KIND and not audited:
            found.append(PickerEntry(
                f"preset:{info.name}", f"preset:{info.name}", info.display, family,
                texts.PICKER_LABELS["preset-closed"].format(display=info.display, kind=kind_text), "preset", None,
                "", texts.OPENAI_COMPAT_CLOSED_NOTE, False, False))
            continue
        found.append(PickerEntry(
            f"preset:{info.name}", f"preset:{info.name}", info.display, family,
            texts.PICKER_LABELS["preset"].format(display=info.display, kind=kind_text), "preset", None,
            texts.PRESET_STATE, "", True, False))
    return found


def _key_present(runtime: Any, name: str) -> bool:
    try:
        return secret_store.default_store(runtime.gateway_environ()).is_set(name)
    except (secret_store.SecretStoreError, OSError):
        return False


# ------------------------------------------------------------------ moving to another computer


def provider_key_names(runtime: Any) -> dict[str, tuple[str, str]]:
    """API-key name -> (provider id, display name) of every keyed provider
    here, the API keys of the account providers included (names only)."""

    found: dict[str, tuple[str, str]] = {}
    try:
        ctx = _cmd().operator_context(runtime, metadata_only=True)
    except (errors.ClaudeMultiError, OSError):
        return found
    for pid, provider in ctx.docs["providers"]["providers"].items():
        ref = ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref")
        if isinstance(ref, str):
            found[ref.removeprefix("env:")] = (pid, str(provider.get("display") or pid))
        alternative = operator_mod.transport_alternative(pid, operator_mod.TRANSPORT_API_KEY)
        if alternative is not None:
            # Reserved for that provider whether or not this build offers it.
            found[alternative.secret_name] = (pid, str(provider.get("display") or pid))
    for pid, provider in ctx.layer.providers.items():
        if provider.secret_name:
            found[provider.secret_name] = (pid, str(provider.entry.get("display") or pid))
    return found


def reconnect_facts(runtime: Any) -> dict[str, list[str]]:
    """What another computer needs connected again, by name only and without
    reading the key file: the API keys this setup uses (providers you added,
    an account provider's API key when it is selected, shipped providers your
    profiles bind) and the accounts signed in here (record names only)."""

    from claude_multi.setup import signin

    names = provider_key_names(runtime)
    wanted: set[str] = set()
    try:
        ctx = _cmd().operator_context(runtime, metadata_only=True)
    except (errors.ClaudeMultiError, OSError):
        ctx = None
    if ctx is not None:
        wanted |= {provider.secret_name for provider in ctx.layer.providers.values() if provider.secret_name}
        for pid, choice in (ctx.ledger.transport_choices.items() if ctx.ledger is not None else ()):
            alternative = operator_mod.transport_alternative(pid, choice)
            if alternative is not None:
                wanted.add(alternative.secret_name)
    by_provider = {pid: name for name, (pid, _display) in names.items()
                   if operator_mod.transport_alternative(pid, operator_mod.TRANSPORT_API_KEY) is None
                   or name != operator_mod.transport_alternative(pid, operator_mod.TRANSPORT_API_KEY).secret_name}
    lines: Mapping[str, Any] = {}
    if ctx is not None:
        lines = operator_mod.merge_docs(ctx.docs, ctx.layer)["models-v2"]["models"]
    try:
        profile_names = [name for name in runtime.profiles.names() if runtime.profiles.has_user(name)]
    except (errors.ClaudeMultiError, OSError):
        profile_names = []
    for name in profile_names:
        try:
            document = runtime.profiles.load(name)
        except (errors.ClaudeMultiError, OSError, ValueError):
            continue
        for spec in [document.get("lead"), *(document.get("agents") or {}).values()]:
            key = spec.get("model") if isinstance(spec, Mapping) else None
            entry = lines.get(key) if isinstance(key, str) else None
            provider = entry.get("provider") if isinstance(entry, Mapping) else None
            if provider in by_provider:
                wanted.add(by_provider[provider])
    sign_ins = []
    for provider_id in signin.ACCOUNT_POOLS:
        try:
            if signin.accounts(runtime, provider_id):
                sign_ins.append(provider_id)
        except errors.ClaudeMultiError:
            continue
    return {"api_keys": sorted(wanted), "sign_ins": sign_ins}
