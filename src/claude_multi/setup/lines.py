"""Removing a model line you added, with a successor where profiles or
named bindings use it.

The plan names every reference, the lines that can take its place (best
first) and the sessions running on it; with a successor it lists the exact
rewrites. The apply proves liveness, the references and the successor again
under the locks, rewrites only the slots still equal to the reviewed ones,
then removes the declaration and re-renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from claude_multi import catalog, errors, operator as operator_mod, profile as profile_mod
from claude_multi import settings as settings_mod, strict_json
from claude_multi.setup import model, texts


def _models() -> Any:
    import claude_multi.cli.commands.models as models_cmd

    return models_cmd


def _cmd() -> Any:
    import claude_multi.cli.commands.providers as providers_cmd

    return providers_cmd


@dataclass(frozen=True)
class LineRemovePlan(model.Plan):
    key: str
    file_display: str
    references: tuple[tuple[str, str, str], ...]  # (kind, name, slot)
    candidates: tuple[str, ...]
    live_users: tuple[str, ...]
    rewrites: tuple[str, ...]
    successor: str | None = None
    file_id: str = ""
    previous: bytes = field(default=b"", repr=False)
    reviewed: tuple[tuple[str, str, str, Any], ...] = field(default=(), repr=False)
    served: Any = field(default=None, compare=False, repr=False)


def _references(runtime: Any, key: str) -> tuple[tuple[str, str, str], ...]:
    found = []
    for user in _cmd().key_references(runtime).get(key, []):
        kind, _space, rest = user.partition(" ")
        if kind == "profile":
            name, _space, slot = rest.partition(" ")
            found.append(("profile", name, slot))
        else:
            found.append(("binding", rest, ""))
    return tuple(found)


def _slot_spec(runtime: Any, kind: str, name: str, slot: str) -> Mapping[str, Any] | None:
    try:
        if kind == "profile":
            document = runtime.profiles.load(name)
            return document.get("lead") if slot == "lead" else (document.get("agents") or {}).get(slot)
        return runtime.bindings.bindings().get(name)
    except (errors.ClaudeMultiError, OSError, ValueError):
        return None


def candidates(runtime: Any, key: str, references: tuple[tuple[str, str, str], ...]) -> tuple[str, ...]:
    """Offered lines that can take every referencing slot (lead slots need
    ``lead``; agent slots ``agents`` and the role; every slot its effort),
    same provider first, then same family, then by display name."""

    lcat = runtime.lineup_catalog()
    eff = runtime.current_effective()
    removed = lcat.lines.get(key) or {}
    found: list[tuple[int, int, str, str]] = []
    for other, entry in lcat.lines.items():
        if other == key or not settings_mod.line_offered(other, entry, eff):
            continue
        capabilities = set(entry.get("capabilities") or ())
        efforts = entry.get("efforts") or ()
        levels = set(efforts) if isinstance(efforts, list) else set(efforts)
        roles = entry.get("roles")
        fits = True
        for kind, name, slot in references:
            spec = _slot_spec(runtime, kind, name, slot)
            effort = spec.get("effort") if isinstance(spec, Mapping) else None
            if slot == "lead":
                fits = "lead" in capabilities and (effort in levels or effort == "ultracode")
            else:
                role = slot if kind == "profile" else None
                fits = "agents" in capabilities and effort in levels and (
                    role is None or roles == "all" or (isinstance(roles, list) and role in roles))
            if not fits:
                break
        if not fits:
            continue
        found.append((0 if entry.get("provider") == removed.get("provider") else 1,
                      0 if entry.get("family") and entry.get("family") == removed.get("family") else 1,
                      str(entry.get("display") or other), other))
    return tuple(item[3] for item in sorted(found))


def plan_line_remove(runtime: Any, key: str, *, successor: str | None = None) -> LineRemovePlan:
    cmd, models_cmd = _cmd(), _models()
    ctx = cmd.operator_context(runtime)
    if ctx.snapshot.ledger_error is not None:
        raise model.Refused(f"{ctx.snapshot.ledger_error} — nothing changed")
    file_id = operator_mod.line_file_ids(ctx.read).get(key)
    if file_id is None:
        if key in runtime.catalog.docs["models-v2"]["models"]:
            raise model.Refused(texts.LINE_NOT_YOURS.format(key=key))
        raise model.Refused(texts.LINE_UNKNOWN.format(key=key))
    scan = cmd.record_scan(runtime)
    try:
        cmd.refuse_unknown_liveness(scan, f"models rm {key}")
    except cmd.OperatorCommandError as exc:
        raise model.Refused(str(exc)) from exc
    users = tuple(sorted(models_cmd._line_users(ctx, scan, key)))
    references = _references(runtime, key)
    found = candidates(runtime, key, references) if references else ()
    if references and not found:
        places = ", ".join(f"{kind} {name}{(' ' + slot) if slot else ''}" for kind, name, slot in references)
        raise model.Refused(texts.LINE_NO_SUCCESSOR.format(key=key, places=places))
    reviewed: list[tuple[str, str, str, Any]] = []
    rewrites: tuple[str, ...] = ()
    if successor is not None:
        try:
            reviewed = list(models_cmd._successor_rewrites(runtime, key, successor))
        except cmd.OperatorCommandError as exc:
            raise model.Refused(str(exc).removeprefix(f"models rm {key}: ")) from exc
        rewrites = tuple(texts.REWRITE_LINE.format(kind=kind, name=name, slot=slot or "binding", key=key,
                                                   successor=successor) for kind, name, slot, _spec in reviewed)
    previous = ctx.read.files[file_id]
    document = strict_json.loads(previous)
    remaining = {k: v for k, v in document["lines"].items() if k != key}
    raw = None if not remaining and "provider" not in document else operator_mod.document_bytes(
        {**document, "lines": remaining})
    served = None
    if not users and (not references or successor is not None):
        try:
            served = cmd.served_preflight(runtime, f"models rm {key}", changes={file_id: raw}, revoke=(key,),
                                          ledger_edit=lambda doc: doc["admissions"].pop(key, None), show=False)
        except cmd.OperatorCommandError as exc:
            raise model.Refused(str(exc)) from exc
    file_display = operator_mod.file_label(file_id)
    lines = (*rewrites, texts.REWRITE_TAIL.format(key=key)) if rewrites else (
        texts.REMOVE_LINE_BODY.format(file=file_display),)
    digest = model.digest_of("remove-line", key, strict_json.sha256_hex(previous), list(references), list(rewrites),
                             list(users), served.sample if served is not None else None)
    return LineRemovePlan("remove-line", key, tuple(lines), "destructive", False, digest, (file_display,), key,
                          file_display, references, found, users, rewrites, successor, file_id, previous,
                          tuple(reviewed), served)


class _StaleReference(model.Stale):
    pass


def live_refusal(users: Any, *, changed: bool = False) -> str:
    """Sessions run on the line: end them (or record the end of one that
    is not running) first."""

    from claude_multi import sessions

    ids = ", ".join(sorted(user[:8] for user in users))
    middle = " — nothing changed;" if changed else " —"
    return f"live sessions use it ({ids}){middle} end them first; {sessions.mark_ended_remedy(users)}"


def apply_line_remove(runtime: Any, plan: LineRemovePlan, confirmation: model.Confirmation) -> model.Applied:
    """Rewrite the reviewed references, remove the line, render and verify."""

    from claude_multi.setup import providers as setup_providers

    if plan.live_users:
        raise model.Refused(live_refusal(plan.live_users))
    if plan.references and plan.successor is None:
        places = ", ".join(f"{kind} {name}{(' ' + slot) if slot else ''}" for kind, name, slot in plan.references)
        raise model.Refused(texts.LINE_NEEDS_SUCCESSOR.format(key=plan.key, places=places,
                                                              candidates=", ".join(plan.candidates) or "none"))
    model.check_apply(runtime, plan, confirmation, verb=f"models rm {plan.key}")
    cmd, models_cmd = _cmd(), _models()
    key, successor = plan.key, plan.successor
    document = strict_json.loads(plan.previous)
    last_wire = document["lines"][key]["wire_model"]
    remaining = {k: v for k, v in document["lines"].items() if k != key}
    raw = None if not remaining and "provider" not in document else operator_mod.document_bytes(
        {**document, "lines": remaining})

    def require(fresh: Any) -> None:
        if fresh.read.files.get(plan.file_id) != plan.previous:
            raise model.Stale(plan.file_display)
        scan = cmd.record_scan(runtime)
        try:
            cmd.refuse_unknown_liveness(scan, f"models rm {key}")
        except cmd.OperatorCommandError as exc:
            raise model.Refused(str(exc)) from exc
        users = models_cmd._line_users(fresh, scan, key)
        if users:
            raise model.Refused(live_refusal(sorted(users), changed=True))
        current = models_cmd._successor_rewrites(runtime, key, successor) if successor is not None else []
        if [tuple(item) for item in current] != [tuple(item) for item in plan.reviewed] \
                or (_references(runtime, key) and successor is None):
            raise model.Stale(f"the references to {key}")

    def write(fresh: Any, undo: model.UndoStack) -> None:
        lcat = runtime.lineup_catalog()
        for kind, name, slot, reviewed in plan.reviewed:
            label = f"{kind} {name}{(' ' + slot) if slot else ''}"
            if kind == "profile":
                def mutate(doc: dict[str, Any], slot: str = slot, reviewed: Any = reviewed,
                           label: str = label) -> None:
                    target = doc.get("lead") if slot == "lead" else (doc.get("agents") or {}).get(slot)
                    if not isinstance(target, dict) or dict(target) != reviewed:
                        raise _StaleReference(label)
                    target["model"] = successor

                def restore(slot: str = slot, reviewed: Any = reviewed, name: str = name) -> None:
                    def back(doc: dict[str, Any]) -> None:
                        target = doc.get("lead") if slot == "lead" else (doc.get("agents") or {}).get(slot)
                        if isinstance(target, dict):
                            target["model"] = reviewed.get("model")
                    runtime.profiles.update(name, back)

                runtime.profiles.update(name, mutate)
                undo.push(restore)
            else:
                def rebind(doc: dict[str, Any], name: str = name, reviewed: Any = reviewed,
                           label: str = label) -> None:
                    spec = doc["bindings"].get(name)
                    if not isinstance(spec, dict) or dict(spec) != reviewed:
                        raise _StaleReference(label)
                    doc["bindings"][name] = {**spec, "model": successor}

                def unbind(name: str = name, reviewed: Any = reviewed) -> None:
                    def back(doc: dict[str, Any]) -> None:
                        doc["bindings"][name] = dict(reviewed)
                    runtime.bindings.update(back, cat=runtime.lineup_catalog(), profiles=runtime.profiles)

                runtime.bindings.update(rebind, cat=lcat, profiles=runtime.profiles)
                undo.push(unbind)
        if raw is None:
            operator_mod.remove_provider_file(fresh.env, plan.file_id)
        else:
            operator_mod.write_provider_bytes(fresh.env, plan.file_id, raw)
        undo.push(lambda: cmd.restore_file(fresh.env, plan.file_id, plan.previous))

    committed = setup_providers.served_transaction(runtime, verb=f"models rm {key}", preflight=plan.served,
                                                   write=write, require=require)
    # After the render published: the grant goes first, then Settings.
    env = runtime.gateway_environ()
    schemas = operator_mod.load_schemas(runtime.asset_root)

    def removed(doc: dict[str, Any]) -> None:
        doc["removed"][key] = {"successor": successor, "last_wire": last_wire, "at": operator_mod.utc_stamp()}
        doc["admissions"].pop(key, None)

    operator_mod.update_ledger(env, schemas, removed)
    runtime.settings_store.revoke_line(key, catalog=models_cmd._catalog_view(runtime))
    note = texts.LINE_REWRITES_NOTE.format(n=len(plan.reviewed), successor=successor) if plan.reviewed else ""
    return model.Applied("remove-line", key, committed.reload, (
        texts.LINE_REMOVED.format(key=key, rewrites_note=note,
                                  reload=setup_providers.reload_text(runtime, committed.reload)),
        *committed.notes))
