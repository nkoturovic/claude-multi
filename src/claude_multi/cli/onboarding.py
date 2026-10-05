"""Presentation adapters over the canonical operator transactions.

No curses, subprocess, lock, or alternative authority lives here. The CLI
owners still perform their human guard, consent, revalidation and commit;
forms supply draft values and a confirmation callback, never approval flags.
"""
from __future__ import annotations

import contextlib
import io
from typing import Callable

from claude_multi import discovery, operator, secret_store, strict_json
import claude_multi.cli.consent as consent
import claude_multi.cli.parser as parser
from claude_multi.cli.commands import discover, models, providers


class Answers(io.StringIO):
    def __init__(self, confirm: Callable[[str], bool]):
        super().__init__()
        self.confirm = confirm


def invoke(runtime, argv, *, confirm, output):
    """Execute an already previewed intent; never shell out to the CLI."""
    if not runtime.allow_state_writes:
        raise providers.OperatorCommandError("read-only: onboarding cannot change operator state")
    consent.require_human(" ".join(argv[:2]), runtime.gateway_environ())
    # Form text is data, not a request to run argparse's help/exit path.
    # Keep parser diagnostics off the curses terminal (they can echo inputs).
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            args = parser.build_parser().parse_args(argv)
        except SystemExit:
            raise providers.OperatorUsageError(
                "invalid onboarding fields — check the provider id, URL and choices; nothing changed"
            ) from None
    with contextlib.redirect_stderr(output):
        command = providers._providers_command if args.command == "providers" else models._models_command
        return command(runtime, args, input_stream=Answers(confirm), output_stream=output, interactive=True)


def toggle_provider(runtime, provider_id, enabled, *, catalog=None, custom_registry=None):
    """The Providers pane on/off toggle, through the setup layer: the shared
    authority preview (no served selector changes: aliases stay rendered)
    and one served-change phase around the Settings write. Returns the
    served-change plan."""
    from claude_multi.setup import model, providers as setup_providers

    plan = setup_providers.plan_toggle(runtime, provider_id, bool(enabled))
    setup_providers.apply_toggle(runtime, plan, model.Confirmation.given(plan))
    return plan.served.plan


def key_status(runtime, provider_id):
    """(secret name, is set) of a keyed operator provider, else None.
    Names and presence only; the value is never read here."""
    ctx = providers.operator_context(runtime)
    provider = ctx.layer.providers.get(provider_id)
    if provider is None or provider.secret_name is None:
        return None
    return provider.secret_name, secret_store.default_store(ctx.env).is_set(provider.secret_name)


def listing(runtime, provider_id, *, confirm):
    """An explicit listing only; moving through forms never reaches this."""
    consent.require_human(f"discover {provider_id}", runtime.gateway_environ())
    call = discover._plan_one(runtime, provider_id)
    if not confirm(discovery.consent_text([call])):
        return None
    result = discover._execute(runtime, call)
    return call, result


def line_draft(runtime, provider_id, entry, call=None, *, registry=None):
    """Use the same listing/registry prefill rules as discover --add.

    ``entry`` carries only what a listing stated (its context is listing
    evidence); ``registry`` = ``(RegistryModel, section)`` supplies a
    candidate's pinned-registry facts, attributed to the registry."""
    _ctx, docs = discover._view(runtime)
    provider = docs["providers"]["providers"][provider_id]
    if registry is not None:
        model, section = registry
        registry_ref = f"pinned registry {section} {discover._today()}"
    else:
        model, registry_ref = discover._registry_model(runtime, provider, entry["id"])
    return discovery.declaration_line(
        entry, provider=provider, agent_efforts=runtime.catalog.agent_efforts,
        source_ref=f"{call.url} {discover._today()}"[:256] if call else "manual declaration",
        registry_model=model, registry_ref=registry_ref,
    )


def declare(runtime, provider_id, key, line, *, output):
    if not runtime.allow_state_writes:
        raise providers.OperatorCommandError("read-only: onboarding cannot declare a line")
    consent.require_human("models add", runtime.gateway_environ())
    with contextlib.redirect_stderr(output):
        return models.declare_line(runtime, provider_id, key, line,
                                   verb="models add", output_stream=output)


def provider_edit_snapshot(runtime, provider_id):
    """The edit form's opening snapshot of a provider you added:
    ``(bytes, provider block)``. The bytes pass the ``providers edit``
    target checks (the ledger, an owned writable file), the keyed preflight,
    a strict parse and the secret screen (stored keys and key shapes, in
    keys and values) before anything of them is drawn; every refusal is
    value-free. The block gives the form's defaults and the bytes are the
    baseline :func:`edit_provider` builds the edit from and the locked write
    compares, so a change made while the form is open is never reverted."""

    from claude_multi import proxy

    label = operator.file_label(provider_id)
    refusal = io.StringIO()
    with contextlib.redirect_stderr(refusal):
        previous = providers.edit_target(runtime, provider_id)
    if isinstance(previous, int):
        text = refusal.getvalue().strip().removeprefix("claude-multi: ")
        raise providers.OperatorCommandError(text or f"providers edit {provider_id}: {label} cannot be edited")
    gated = operator.keyed_preflight(runtime.catalog.docs, provider_id, previous)
    if gated is not None:
        raise providers.OperatorCommandError(f"{label} cannot be edited here: {gated.text()}")
    try:
        document = strict_json.loads(previous, strict_json.JSONLimits(max_bytes=operator.READ_LIMIT_BYTES,
                                                                      max_string=4096))
    except strict_json.StrictJSONError as exc:
        raise providers.OperatorCommandError(
            f"{label} is not strict JSON ({operator._json_failure(exc)}); "
            f"fix it in an editor: claude-multi providers edit {provider_id}") from None
    stored = proxy._stored_secret_values(runtime.gateway_environ())
    hits = operator._secret_hits(document, "$", stored)
    if not hits and operator._raw_secret_hit(previous, stored):
        hits = ["$"]
    if hits:
        raise providers.OperatorCommandError(
            f"{label} holds a value that looks like a secret at {', '.join(hits)} (not shown); remove it and "
            f"reference the key as env:NAME: claude-multi providers edit {provider_id}")
    block = document.get("provider") if isinstance(document, dict) else None
    if not isinstance(block, dict):
        raise providers.OperatorCommandError(
            f"{label} has no provider block; fix it in an editor: claude-multi providers edit {provider_id}")
    return previous, block


def edit_provider(runtime, provider_id, values, *, previous, confirm, output):
    """The edit form's answers through the providers edit transaction (the
    same checks, preview, confirmation, locked write and verified reload),
    built from ``previous``, the form's opening snapshot
    (:func:`provider_edit_snapshot`): a file changed since is refused here
    and again under the write lock, never overwritten with the form's
    stale defaults."""
    if not runtime.allow_state_writes:
        raise providers.OperatorCommandError("read-only: onboarding cannot edit a provider")
    consent.require_human("providers edit", runtime.gateway_environ())
    with contextlib.redirect_stderr(output):
        current = providers.edit_target(runtime, provider_id)
        if isinstance(current, int):
            return current
        if current != previous:
            return providers.fail(f"providers edit {provider_id}: the file changed while you edited it — "
                                  "nothing written; open the form again")
        edited = providers.edited_declaration(previous, values)
        return providers.edit_declaration(runtime, provider_id, previous, edited,
                                          input_stream=Answers(confirm), output_stream=output)


def declaration(runtime, key):
    ctx = providers.operator_context(runtime)
    file_id = operator.line_file_ids(ctx.read).get(key)
    if file_id is None:
        raise providers.OperatorCommandError(f"{key}: catalog lines use the developer draft workflow")
    return file_id, strict_json.loads(ctx.read.files[file_id])["lines"][key]


def edit_line(runtime, key, line, *, confirm, output):
    if not runtime.allow_state_writes:
        raise providers.OperatorCommandError("read-only: onboarding cannot edit a line")
    consent.require_human("models edit", runtime.gateway_environ())
    with contextlib.redirect_stderr(output):
        return models._models_edit(runtime, key, input_stream=Answers(confirm),
                                    output_stream=output, interactive=True, declaration=line)


def candidates(runtime, *, output):
    return models._models_candidates(runtime, False, output)


def details(runtime, key, *, output):
    return models._models_show(runtime, key, True, True, output)


def statuses(runtime):
    ctx = providers.operator_context(runtime)
    admitted = providers.admitted_keys(runtime)
    return {key: ("route unapproved" if ctx.layer.route_status.get(line.provider_id, "catalog") not in operator.ROUTE_USABLE
                  else {"New · Off": "off", "changed since admission (re-admit)": "changed — re-admit"}.get(
                      providers.line_status(key, ctx, admitted), providers.line_status(key, ctx, admitted)))
            for key, line in ctx.layer.lines.items()}
