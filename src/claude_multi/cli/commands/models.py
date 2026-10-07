"""The ``models`` listing and optional admission and qualification badges.

``add`` declares a model without approving its credential route. ``admit``
and ``revoke`` change local metadata only, never inference or render state.
``qualify`` records verdict-only evidence after explicit request-plan consent;
its render/route identity is revalidated before sends and evidence commits.
No lock is held across a prompt or a network call.
Exit statuses: 0 ok, 1 refused, 2 usage, 3 declined, 130 cancelled.
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Mapping, TextIO, TYPE_CHECKING

import claude_multi.cli.consent as consent
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.commands.providers as providers_cmd
from claude_multi import catalog
from claude_multi import discovery
from claude_multi import errors as cli_errors
from claude_multi import launch
from claude_multi import operator as operator_mod
from claude_multi import qualify as qualify_mod
from claude_multi import render as render_mod
from claude_multi import secret_store
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import strict_json

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod
    from claude_multi import continuity as continuity_mod


MODELS_LIST_SCHEMA = 1


def _models_listing(runtime: runtime_mod.Runtime, output_stream: Any) -> int:
    """``claude-multi models`` and ``models list``: the lines, then retired keys.

    One row per line of the merged (shipped and your own) view: key,
    display, generation, provider, class (``context.ordinary_profile``;
    lines for agents only have none), efforts, status and the wire id, with
    the typed ``/model`` selectors below it. Retired keys follow with their
    successors. Nothing is written.
    """

    lcat = runtime.lineup_catalog()
    for key in sorted(lcat.lines):
        line = lcat.lines[key]
        provider_id = line.get("provider", "?")
        provider = (lcat.providers.get(provider_id) or {}).get("display") or provider_id
        context = line.get("context") or {}
        line_class = context.get("ordinary_profile") or "agents-only"
        efforts = line.get("efforts") or ()
        effort_text = ",".join(str(effort) for effort in efforts) or "-"
        output_stream.write(
            f"{key}\t{line.get('display', key)}\tgeneration {line.get('generation', '?')}\t"
            f"{provider}\tclass {line_class}\tefforts {effort_text}\t"
            f"{line.get('status', '?')}\twire={line.get('wire_model', '?')}\n"
        )
        selectors = gateway_facts._line_selectors(line)
        if selectors:
            output_stream.write(
                "  in-session: " + " · ".join(f"/model {s}" for s in selectors) + "\n"
            )
    for key in sorted(lcat.retired):
        entry = lcat.retired[key]
        successor = entry.get("successor")
        output_stream.write(
            f"{key}\t{entry.get('display', key)}\tretired (catalog {entry.get('since_catalog', '?')})"
            f"\t{'successor ' + str(successor) if successor else 'no successor'}\n"
        )
    return 0


def models_list_document(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """The ``models list --json`` document: lines and retired keys, each
    sorted by key. A field the line does not state is ``null``; ``class`` is
    ``null`` for a line that serves agents only."""

    lcat = runtime.lineup_catalog()
    lines = []
    for key in sorted(lcat.lines):
        line = lcat.lines[key]
        context = line.get("context") or {}
        efforts = line.get("efforts") or ()
        lines.append({
            "key": key,
            "display": line.get("display", key),
            "generation": line.get("generation"),
            "provider": line.get("provider"),
            "class": context.get("ordinary_profile"),
            "efforts": [str(effort) for effort in efforts],
            "status": line.get("status"),
            "wire": line.get("wire_model"),
            "selectors": list(gateway_facts._line_selectors(line)),
        })
    retired = [
        {
            "key": key,
            "display": lcat.retired[key].get("display", key),
            "since_catalog": lcat.retired[key].get("since_catalog"),
            "successor": lcat.retired[key].get("successor"),
        }
        for key in sorted(lcat.retired)
    ]
    return {"schema_version": MODELS_LIST_SCHEMA, "list": "models", "lines": lines, "retired": retired}


def _models_list(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    if getattr(args, "json_output", False):
        output_stream.write(strict_json.pretty_file_bytes(models_list_document(runtime)).decode("utf-8"))
        return 0
    return _models_listing(runtime, output_stream)


fail = providers_cmd.fail
report = providers_cmd.report
OperatorCommandError = providers_cmd.OperatorCommandError

CANDIDATES_HEADER = "Candidates — advisory only; nothing declared or admitted"
CANDIDATES_NEXT = "next: claude-multi models add PROVIDER WIRE --context N --source registry"
REGISTRY_UNAVAILABLE = "Pinned registry unavailable; showing served candidates only."
GATEWAY_UNAVAILABLE = "Gateway observation unavailable; showing pinned-registry candidates only."


def _tokens(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _models_candidates(runtime: runtime_mod.Runtime, include_all: bool, output_stream: TextIO) -> int:
    """``claude-multi models --candidates [--all]``.

    The offline pinned registry plus one loopback /v1/models snapshot; no
    provider request, declaration prompt or write. Registry figures are
    registry-stated, never validation.
    """

    registry = gateway_facts.pinned_registry(runtime)
    served = None
    try:
        entries, _status = runtime.served_snapshot(runtime.gateway_token())
        served = entries
    except launch.LaunchError:
        served = None
    known = gateway_facts.discovery_known(runtime)
    result = discovery.candidates(registry, served, known, include_all=include_all)
    report(output_stream, CANDIDATES_HEADER)
    if registry is None:
        report(output_stream, REGISTRY_UNAVAILABLE)
    if served is None:
        report(output_stream, GATEWAY_UNAVAILABLE)
    rows = [*result.rows, *result.unattributed]
    for row in rows:
        meta = row.metadata
        context = _tokens(meta.context_length if meta is not None else None)
        output = _tokens(meta.max_completion_tokens if meta is not None else None)
        channel = row.channel or "unattributed"
        report(output_stream, f"{channel}  {row.wire}  registry-stated context {context}  max output {output}")
        if row.channel is None:
            detail = f"owned_by {row.owned_by} (advisory)" if row.owned_by else "attribution unknown"
        else:
            detail = f"visibility: {row.visibility}" if row.visibility else "visibility: n/a"
        report(output_stream, f"  {row.mark} · {detail} · {row.created_text}")
    for channel in sorted(result.hidden_older):
        report(output_stream, f"{channel}: {result.hidden_older[channel]} older candidate(s) hidden "
                              "(created before the newest onboarded wire) — --all shows them")
    if result.hidden_channels:
        report(output_stream, f"other registry channels hidden: {', '.join(result.hidden_channels)} — "
                              "--all shows them")
    if not rows:
        report(output_stream, "No candidates.")
    else:
        report(output_stream, CANDIDATES_NEXT)
    return 0


# ------------------------------------------------------------ render identity (current-sentinel proof)
def render_identity(runtime: runtime_mod.Runtime) -> tuple[str | None, frozenset[str] | None, str]:
    """(expected sentinel, served ids, problem) for the currently loaded
    render: the fresh render equals the on-disk config and its sentinel is
    served. ``problem`` is empty when the render is current."""

    try:
        token = runtime.gateway_token()
    except launch.LaunchError as exc:
        return None, None, f"the gateway key is unreadable ({exc})"
    snap = gateway_facts._gateway_snapshot(runtime, token)
    if snap.render_error is not None:
        return None, snap.served, f"the expected render failed ({snap.render_error})"
    if snap.served is None:
        return snap.sentinel, None, "the gateway is not reachable (claude-multi-proxy status)"
    if snap.config_drift is not False:
        return snap.sentinel, snap.served, "the on-disk gateway config is not the current render"
    if snap.sentinel not in snap.served:
        return snap.sentinel, snap.served, f"the gateway does not serve the current render (sentinel {snap.sentinel})"
    return snap.sentinel, snap.served, ""


def _served_check(key: str, line: operator_mod.ResolvedOperatorLine, served: frozenset[str] | None) -> str:
    aliases = operator_mod.line_aliases(line.core_entry)
    count = len(set(aliases) & set(served or ()))
    if count < len(aliases):
        return f"{count}/{len(aliases)} aliases served — claude-multi providers apply"
    return ""


def _qualification_context(runtime: runtime_mod.Runtime, key: str, verb: str) -> providers_cmd.OperatorContext:
    """Audit/route preflight before even the defensive secret-store scan.
    This snapshot conveys no grant: normal validation still follows it.
    """

    ctx = providers_cmd.operator_context(runtime, metadata_only=True)
    providers_cmd.require_ledger(ctx, verb)
    line = _operator_line(ctx, key, verb)
    provider = (ctx.layer.providers[line.provider_id].entry if line.provider_id in ctx.layer.providers
                else ctx.docs["providers"]["providers"][line.provider_id])
    if catalog.is_keyed_compat(provider):
        problem = catalog.keyed_compat_problem(provider, runtime.catalog.docs["gateway"])
        if problem:
            raise OperatorCommandError(f"{verb}: {problem}")
        route = ctx.layer.route_status.get(line.provider_id, "catalog")
        if route not in operator_mod.ROUTE_USABLE:
            raise OperatorCommandError(f"{verb}: " + operator_mod.route_status_text(line.provider_id, route))
    return providers_cmd.operator_context(runtime)


def _credential_present(ctx: providers_cmd.OperatorContext, provider: Mapping[str, Any], name: str) -> bool:
    store = secret_store.default_store(ctx.env)
    return render_mod.usable_secret(store.get(name)) if catalog.is_keyed_compat(provider) else store.is_set(name)


def _run_smoke(
    runtime: runtime_mod.Runtime, ctx: providers_cmd.OperatorContext, key: str, verb: str, *,
    input_stream: TextIO, output_stream: TextIO,
) -> operator_mod.SmokeOutcome | qualify_mod.CheckResult | None:
    """Consent, ONE request, revalidation, evidence (never under a lock
    across the prompt or the request). None: declined (nothing sent)."""

    line = ctx.layer.lines[key]
    effective_docs = operator_mod.apply_transports(ctx.docs, operator_mod.transport_selections(ctx.docs, ctx.ledger))
    plan = operator_mod.smoke_plan(effective_docs, ctx.layer, key)
    sentinel, served, problem = render_identity(runtime)
    if problem:
        raise OperatorCommandError(f"{verb}: {problem} — nothing sent")
    unserved = _served_check(key, line, served)
    if unserved:
        raise OperatorCommandError(f"{verb}: {unserved} — nothing sent")
    contracts = runtime.contract_identity()
    consented = _qualify_authority(runtime, ctx, key, (sentinel, problem), contracts)
    if not consent.confirm(operator_mod.smoke_consent_text(plan), input_stream=input_stream):
        return None
    _qualify_revalidator(runtime, key, consented)(0)
    provider = (ctx.layer.providers[line.provider_id].entry if line.provider_id in ctx.layer.providers
                else effective_docs["providers"]["providers"][line.provider_id])
    if catalog.is_keyed_compat(provider):
        battery = qualify_mod.build_plan(key, line.core_entry, digest=line.definition_digest,
                                        provider_id=line.provider_id, provider=provider, checks=("smoke",),
                                        keyed_replay=True)
        (outcome,) = qualify_mod.run_battery(battery, runtime.qualify_post)
    else:
        outcome = runtime.smoke(plan.alias)
    # The smoke-evidence commit is its own phase (no served
    # change, so no preview); the consent and the call ran with no lock.
    with providers_cmd.operator_write(runtime, preflight=None):
        fresh = providers_cmd.operator_context(runtime)
        now = fresh.layer.lines.get(key)
        after, served_after, problem_after = render_identity(runtime)
        if (now is None or _served_check(key, now, served_after) or
                _qualify_authority(runtime, fresh, key, (after, problem_after), runtime.contract_identity()) != consented):
            raise OperatorCommandError(f"{verb}: configuration changed during the smoke — evidence not recorded; retry")
        # The smoke merges into the line's evidence (a
        # transient outcome never replaces a pass; other checks stay).
        if isinstance(outcome, qualify_mod.CheckResult):
            record = outcome.record(operator_mod.utc_stamp(), runtime.contract_identity())
        else:
            record = operator_mod.smoke_check_record(outcome, at=operator_mod.utc_stamp(),
                                                     contracts=runtime.contract_identity().as_document())
        operator_mod.record_checks(fresh.env, fresh.schemas, key, digest=plan.digest, host=plan.host,
                                   versions=runtime.evidence_versions(), results=[(("smoke",), record)])
    http = f"HTTP {outcome.http}" if outcome.http is not None else "no HTTP status"
    detail = "" if outcome.result == "pass" else f": {outcome.reason}"
    report(output_stream, f"smoke {key}: {outcome.result} ({http}{detail}); evidence recorded for "
                          f"{line.definition_digest[:16]}")
    return outcome


def _operator_line(ctx: providers_cmd.OperatorContext, key: str, verb: str) -> operator_mod.ResolvedOperatorLine:
    layer = ctx.layer
    line = layer.lines.get(key)
    if line is not None:
        return line
    if key in layer.pending:
        raise OperatorCommandError(f"{verb}: {key} is pending migration — claude-multi providers migrate-custom --apply")
    if key in layer.displaced:
        displaced = layer.displaced[key]
        raise OperatorCommandError(
            f"{verb}: the catalog serves {displaced.provider_id}/{displaced.core_entry['wire_model']}; "
            f"{key} yields to it — admit or bind that catalog line instead")
    file_id = operator_mod.line_file_ids(ctx.read).get(key)
    found = [p.text() for p in ctx.layer.problems_by_file.get(operator_mod.file_label(file_id), ())] if file_id else []
    detail = ("\n  " + "\n  ".join(found)) if found else ""
    raise OperatorCommandError(f"{verb}: no valid declaration of {key} (claude-multi providers validate){detail}")


def _catalog_view(runtime: runtime_mod.Runtime) -> Any:
    return runtime.lineup_catalog()


# ------------------------------------------------------------ verbs
_EFFORT = re.compile(r"^([a-z]+)(?:=([a-z0-9-]+))?$")


def _derived_key(wire: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", wire.lower()).strip("-")
    key = f"custom-{slug}"[:48].rstrip("-")
    return key if operator_mod.NEW_KEY.fullmatch(key) else None


def _efforts(args: argparse.Namespace, provider: Mapping[str, Any]) -> tuple[Any, str]:
    pairs = []
    for item in args.effort:
        match = _EFFORT.fullmatch(item)
        if match is None:
            raise providers_cmd.OperatorUsageError(f"--effort {item!r}: LEVEL or LEVEL=CONTRACT")
        pairs.append((match.group(1), match.group(2)))
    if pairs:
        with_contract = [contract is not None for _level, contract in pairs]
        if any(with_contract) and not all(with_contract):
            raise providers_cmd.OperatorUsageError("--effort: give every level a contract (a map) or none (a list)")
        efforts: Any = ({level: contract for level, contract in pairs} if all(with_contract)
                        else [level for level, _contract in pairs])
    else:
        # The reviewed high contract when the provider has one, else ["high"].
        high = next((c for c in provider.get("payload_contracts", []) if str(c).endswith("-high")), None)
        efforts = {"high": high} if high is not None else ["high"]
    levels = list(efforts) if isinstance(efforts, list) else sorted(efforts)
    default = args.default_effort or ("high" if "high" in levels else levels[0])
    return efforts, default


def _models_add(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, output_stream: TextIO) -> int:
    ctx = providers_cmd.operator_context(runtime)
    providers_cmd.require_ledger(ctx, "models add")
    provider_id = args.provider
    catalog_provider = ctx.docs["providers"]["providers"].get(provider_id)
    previous = ctx.read.files.get(provider_id)
    if previous is not None:
        try:
            document = strict_json.loads(previous)
        except strict_json.StrictJSONError:
            return fail(f"models add: {operator_mod.file_label(provider_id)} is not valid JSON — "
                        "claude-multi providers validate")
    elif catalog_provider is not None:
        document = {"version": operator_mod.DATA_VERSION, "lines": {}}
    else:
        return fail(f"models add: unknown provider {provider_id} — declare it first: "
                    f"claude-multi providers add {provider_id} ...")
    provider = (ctx.layer.providers[provider_id].entry if provider_id in ctx.layer.providers
                else catalog_provider or document.get("provider") or {})
    key = args.key or _derived_key(args.wire)
    if key is None:
        return fail(f"models add: no valid key derives from {args.wire!r} — name it with --as custom-<name>")
    efforts, default = _efforts(args, provider)
    context: dict[str, Any] = {"declared_tokens": args.context, "source": args.source}
    if args.source_ref is not None:
        context["source_ref"] = args.source_ref
    line = {"wire_model": args.wire, "display": args.display or args.wire, "efforts": efforts,
            "default_effort": default, "context": context}
    return declare_line(runtime, provider_id, key, line, verb="models add", output_stream=output_stream)


def declare_line(
    runtime: runtime_mod.Runtime, provider_id: str, key: str, line: Mapping[str, Any], *, verb: str,
    output_stream: TextIO,
) -> int:
    """The declaration transaction (``models add``; ``discover
    --add`` reuses it): validate the whole proposed layer, write one
    providers.d file under the frozen lock order, re-render or undo. A
    declared line has no admission badge; existing lines are never edited here."""

    ctx = providers_cmd.operator_context(runtime)
    providers_cmd.require_ledger(ctx, verb)
    catalog_provider = ctx.docs["providers"]["providers"].get(provider_id)
    previous = ctx.read.files.get(provider_id)
    if previous is not None:
        try:
            document = strict_json.loads(previous)
        except strict_json.StrictJSONError:
            return fail(f"{verb}: {operator_mod.file_label(provider_id)} is not valid JSON — "
                        "claude-multi providers validate")
    elif catalog_provider is not None:
        document = {"version": operator_mod.DATA_VERSION, "lines": {}}
    else:
        return fail(f"{verb}: unknown provider {provider_id} — declare it first: "
                    f"claude-multi providers add {provider_id} ...")
    wire = line["wire_model"]
    if key in document.get("lines", {}):
        return fail(f"{verb}: {key} is already declared in {operator_mod.file_label(provider_id)} — "
                    f"edit it: claude-multi models edit {key}")
    document = {**document, "lines": {**document.get("lines", {}), key: dict(line)}}
    raw = operator_mod.document_bytes(document)
    layer = providers_cmd.proposed(ctx, {provider_id: raw})
    own = [p for p in operator_mod.file_problems(layer, provider_id)]
    if key in layer.displaced:
        return fail(f"{verb}: {provider_id}/{wire} is a catalog line — admit or bind that line instead")
    if own or key not in layer.lines:
        return fail(f"{verb}: not declared:\n  " + "\n  ".join(p.text() for p in own or
                                                                 layer.problems_by_file.get(
                                                                     operator_mod.file_label(provider_id), ())))
    providers_cmd.refuse_blocking(layer, verb)
    preflight = providers_cmd.served_preflight(runtime, verb, changes={provider_id: raw})
    with providers_cmd.operator_write(runtime, preflight=preflight):
        fresh = providers_cmd.operator_context(runtime)
        if fresh.read.files.get(provider_id) != previous:
            return fail(f"{verb}: {operator_mod.file_label(provider_id)} changed meanwhile — nothing written; retry")
        from claude_multi.setup import providers as setup_providers

        with providers_cmd.guarded_writes() as undo:
            setup_providers.write_declaration(undo, fresh, provider_id, raw, previous)
        result = providers_cmd.render_or_undo(runtime, undo.run, verb, output_stream)
    resolved = layer.lines[key]
    entry = resolved.core_entry
    selectors = ", ".join(selector for _level, selector, _contract in catalog.line_selectors(dict(entry)))
    declared = entry["context"]["declared_tokens"]
    report(output_stream, f"declared {key} — New · not admitted · selectors {selectors} · class custom-{declared}")
    report(output_stream, f"{_agent_status(resolved)} · validated floor {entry['context']['validated_tokens']} (not near-limit measured)")
    status = layer.route_status.get(provider_id)
    if status in ("unapproved", "changed"):
        report(output_stream, f"provider {provider_id}: route {status} — not served until "
                              f"claude-multi providers approve {provider_id}")
    report(output_stream, f"select this model on a configured route; optional: claude-multi models admit {key} "
                          f"or claude-multi models qualify {key} --smoke")
    providers_cmd.verify(runtime, result, output_stream)
    return 0


def _agent_status(line: operator_mod.ResolvedOperatorLine) -> str:
    """Declaration recommendations, not permission to bind an agent."""

    entry = line.core_entry
    if "agents" not in entry["capabilities"]:
        return "lead recommended; explicit agent bindings are allowed"
    roles = entry["roles"]
    shown = "all roles" if roles == "all" else ", ".join(roles) or "no roles declared"
    return f"agent recommendation: {shown}; qualification is optional"


def _checklist(output_stream: TextIO, text: str) -> None:
    report(output_stream, f"  ✓ {text}")


def _models_admit(runtime: runtime_mod.Runtime, key: str, *, input_stream: TextIO, output_stream: TextIO) -> int:
    from claude_multi.setup import model as setup_model

    verb = f"admit {key}"
    consent.require_human(f"models admit {key}", runtime.gateway_environ())
    ctx = None
    line = None
    if key in runtime.catalog.lines:
        if runtime.catalog.lines[key].get("status", "active") != "new":
            raise OperatorCommandError(f"{verb}: only status new catalog lines are admitted")
    else:
        ctx = providers_cmd.operator_context(runtime)
        providers_cmd.require_ledger(ctx, verb)
        line = _operator_line(ctx, key, verb)
        _checklist(output_stream, f"declaration valid ({line.source})")
    if not consent.confirm(
        f"Admit {key}? Record an optional local attestation only; no requests are sent, "
        "route approval and qualification are unchanged. [y/N] ", input_stream=input_stream,
    ):
        raise setup_model.Declined(f"models {verb}: not admitted — nothing changed")
    # No served preview: neither badge changes selectors, routes or credentials.
    # Keep the normal writer barrier, store locks and definition revalidation.
    with providers_cmd.operator_write(runtime, preflight=None):
        if line is None:
            runtime.settings_store.admit_line(key, catalog=runtime.catalog)
        else:
            fresh = providers_cmd.operator_context(runtime)
            providers_cmd.require_ledger(fresh, verb)
            now = fresh.layer.lines.get(key)
            if now is None or now.definition_digest != line.definition_digest:
                raise OperatorCommandError(f"{verb}: configuration changed while awaiting confirmation — "
                                           "nothing admitted; retry")

            def record_badge(document: dict[str, Any]) -> None:
                # Settings lock -> api-key leaf. Both bits are required for a
                # badge, so an interrupted first admission cannot invent one.
                operator_mod.update_ledger(fresh.env, fresh.schemas, lambda ledger: ledger["admissions"].__setitem__(
                    key, operator_mod.admission_record(fresh.layer, key, at=operator_mod.utc_stamp(), via="admit")))
                document["admitted_lines"] = sorted(set(document.get("admitted_lines", [])) | {key})

            runtime.settings_store.update(record_badge, catalog=_catalog_view(runtime))
    report(output_stream, f"admitted {key}. Optional badge recorded; route approval and qualification unchanged.")
    return 0


REVOKE_QUESTION = ("Revoke the optional admission badge for {key}? The line remains usable on a configured route; "
                   "qualification evidence is unchanged. [y/N] ")


def _confirm_revoke(key: str, *, input_stream: TextIO, interactive: bool, yes: bool) -> None:
    """The revoke confirmation: default No on a terminal; ``--yes`` without one."""

    from claude_multi.setup import model as setup_model

    if yes:
        return
    if not interactive:
        raise cli_errors.CLIError(f"models revoke {key} needs a terminal to confirm; nothing changed",
                                  remedy=f"claude-multi models revoke {key} --yes")
    if not consent.confirm(REVOKE_QUESTION.format(key=key), input_stream=input_stream):
        raise setup_model.Declined(f"models revoke {key}: not revoked — nothing changed")


def _models_revoke(runtime: runtime_mod.Runtime, key: str, *, input_stream: TextIO, output_stream: TextIO,
                   interactive: bool, yes: bool) -> int:
    if key in runtime.catalog.lines:
        _confirm_revoke(key, input_stream=input_stream, interactive=interactive, yes=yes)
        with providers_cmd.operator_write(runtime, preflight=None):
            runtime.settings_store.revoke_line(key, catalog=runtime.catalog)
    else:
        ctx = providers_cmd.operator_context(runtime)
        providers_cmd.require_ledger(ctx, f"revoke {key}")
        grant = ctx.ledger.admissions.get(key) if ctx.ledger is not None else None
        if grant is None and key not in providers_cmd.admitted_keys(runtime):
            return fail(f"revoke {key}: {key} is not admitted")
        _confirm_revoke(key, input_stream=input_stream, interactive=interactive, yes=yes)
        with providers_cmd.operator_write(runtime, preflight=None):
            fresh = providers_cmd.operator_context(runtime)
            providers_cmd.require_ledger(fresh, f"revoke {key}")
            now = fresh.ledger.admissions.get(key) if fresh.ledger is not None else None
            if now != grant:
                raise OperatorCommandError(f"revoke {key}: admission changed while awaiting confirmation — "
                                           "nothing revoked; retry")

            def clear_badge(document: dict[str, Any]) -> None:
                if now is not None:
                    # Clear the digest-bound record first; neither write
                    # changes the route or the qualification evidence.
                    operator_mod.update_ledger(fresh.env, fresh.schemas, lambda doc: doc["admissions"].pop(key, None))
                document["admitted_lines"] = sorted(set(document.get("admitted_lines", [])) - {key})

            runtime.settings_store.update(clear_badge, catalog=_catalog_view(runtime))
    report(output_stream, f"revoked {key}. Admission badge removed; use availability and qualification unchanged.")
    return 0


def _models_edit(runtime: runtime_mod.Runtime, key: str, *, input_stream: TextIO, output_stream: TextIO,
                 interactive: bool, declaration: Mapping[str, Any] | None = None) -> int:
    if not interactive:
        return fail(f"models edit {key} needs a terminal for the editor")
    ctx = providers_cmd.operator_context(runtime)
    providers_cmd.require_ledger(ctx, f"models edit {key}")
    file_id = operator_mod.line_file_ids(ctx.read).get(key)
    if file_id is None:
        return fail(f"models edit {key}: no providers.d declaration of {key}")
    previous = ctx.read.files[file_id]
    document = strict_json.loads(previous)
    fragment = operator_mod.document_bytes({key: document["lines"][key]})
    try:
        # Value-free: the existing line is unvalidated (it may carry a literal).
        operator_mod.check_writable_target(operator_mod.providers_dir(ctx.env), file_id, None)
    except operator_mod.OperatorError as exc:
        return fail(str(exc))
    kept = None
    if declaration is not None:
        replacement = {key: dict(declaration)}
    else:
        edited, kept, problem = providers_cmd.run_editor(runtime, fragment, f"models edit {key}")
        if edited is None:
            return fail(f"{key} was not changed: {problem}; your edit is kept at {kept}")
        try:
            replacement = strict_json.loads(edited)
        except strict_json.StrictJSONError:
            return fail(f"{key} was not changed: not strict JSON; your edit is kept at {kept}")
        if not isinstance(replacement, dict) or set(replacement) != {key}:
            return fail(f"{key} was not changed: keep exactly one top-level key {key!r}; your edit is kept at {kept}")
    recovery = f"your edit is kept at {kept}" if kept is not None else "form draft was not saved"
    lines = dict(document["lines"])
    lines[key] = replacement[key]
    raw = operator_mod.document_bytes({**document, "lines": lines})
    if raw == previous:
        if kept is not None:
            kept.unlink(missing_ok=True)
        report(output_stream, f"{key}: unchanged")
        return 0
    layer = providers_cmd.proposed(ctx, {file_id: raw})
    own = operator_mod.file_problems(layer, file_id)
    if own:
        return fail(f"{key} was not changed:\n  " + "\n  ".join(p.text() for p in own)
                    + f"\n{recovery}")
    for line in providers_cmd.definition_consequences(ctx.layer, layer, {key}, providers_cmd.record_scan(runtime),
                                                      providers_cmd.admitted_keys(runtime)):
        report(output_stream, line)
    try:
        preflight = providers_cmd.served_preflight(runtime, f"models edit {key}", changes={file_id: raw})
    except OperatorCommandError as exc:
        return fail(f"{exc}\n{recovery}")
    if not consent.confirm(f"Write {operator_mod.file_label(file_id)}? [y/N] ", input_stream=input_stream):
        return fail(f"nothing written; {recovery}")
    with providers_cmd.operator_write(runtime, preflight=preflight):
        fresh = providers_cmd.operator_context(runtime)
        if fresh.read.files.get(file_id) != previous:
            return fail(f"models edit {key}: the file changed while you edited it — nothing written; "
                        f"{recovery}")
        from claude_multi.setup import providers as setup_providers

        with providers_cmd.guarded_writes() as undo:
            setup_providers.write_declaration(undo, fresh, file_id, raw, previous)
        result = providers_cmd.render_or_undo(runtime, undo.run, f"models edit {key}", output_stream)
    if kept is not None:
        kept.unlink(missing_ok=True)
    report(output_stream, f"wrote {operator_mod.file_label(file_id)}")
    providers_cmd.verify(runtime, result, output_stream)
    return 0


def _successor_rewrites(runtime: runtime_mod.Runtime, key: str,
                        successor: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(kind, name, slot, reviewed spec) references to rewrite; refuses an
    unusable successor. The reviewed spec is the slot's binding as read, so
    a commit rewrites only a slot that still matches it."""

    lcat = runtime.lineup_catalog()
    try:
        resolved = lcat.resolve_key(successor).key
    except catalog.CatalogError:
        resolved = None
    if resolved is None or resolved == key:
        raise OperatorCommandError(f"models rm {key}: successor {successor} does not resolve to another live line")
    eff = runtime.current_effective()
    entry = lcat.lines[resolved]
    if not settings_mod.line_offered(resolved, entry, eff):
        reason = eff.unavailable_lines.get(resolved, f"provider {entry['provider']} is off in Settings")
        raise OperatorCommandError(f"models rm {key}: successor {resolved} is not offered ({reason})")
    efforts = entry.get("efforts") or ()
    levels = set(efforts) if isinstance(efforts, list) else set(efforts)
    rewrites: list[tuple[str, str, str]] = []
    for name in runtime.profiles.names():
        if not runtime.profiles.has_user(name):
            continue
        document = runtime.profiles.load(name)
        slots = [("lead", document.get("lead"))] + sorted((document.get("agents") or {}).items())
        for slot, spec in slots:
            if isinstance(spec, Mapping) and spec.get("model") == key:
                effort = spec.get("effort")
                if effort not in levels and not (slot == "lead" and effort == "ultracode"):
                    raise OperatorCommandError(f"models rm {key}: successor {resolved} has no effort {effort} "
                                               f"(profile {name} {slot})")
                rewrites.append(("profile", name, slot, dict(spec)))
    for name, spec in sorted(runtime.bindings.bindings().items()):
        if spec.get("model") == key:
            if spec.get("effort") not in levels:
                raise OperatorCommandError(f"models rm {key}: successor {resolved} has no effort {spec.get('effort')} "
                                           f"(binding {name})")
            rewrites.append(("binding", name, "", dict(spec)))
    return rewrites


def _line_users(ctx: providers_cmd.OperatorContext, scan: continuity_mod.RecordScan, key: str) -> frozenset[str]:
    """Live sessions naming any alias of ``key`` (declared or captured)."""

    aliases: set[str] = set()
    if key in ctx.layer.lines:
        aliases |= set(operator_mod.line_aliases(ctx.layer.lines[key].core_entry))
    if ctx.ledger is not None:
        aliases |= {alias for alias, capture in ctx.ledger.aliases.items() if capture["source"] == f"operator:{key}"}
    return providers_cmd.live_users(scan, aliases)


class _StaleReference(OperatorCommandError):
    """A reviewed reference changed between the preview and the commit."""


def _models_rm(runtime: runtime_mod.Runtime, key: str, successor: str | None, *, input_stream: TextIO,
               output_stream: TextIO, interactive: bool, yes: bool = False) -> int:
    """``models rm KEY [--successor KEY] [--yes]``: refused while sessions
    run on it; where profiles or named bindings use it, a successor takes
    their place (the rewrites are listed first). Asks y/N unless ``--yes``."""

    from claude_multi.setup import lines as setup_lines
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import texts as setup_texts

    plan = setup_lines.plan_line_remove(runtime, key, successor=successor)
    if plan.live_users:
        return fail(f"models rm {key}: {setup_lines.live_refusal(plan.live_users)}")
    if plan.references and successor is None:
        places = ", ".join(f"{kind} {name}{(' ' + slot) if slot else ''}" for kind, name, slot in plan.references)
        return fail(f"models rm {key}: " + setup_texts.LINE_NEEDS_SUCCESSOR.format(
            key=key, places=places, candidates=", ".join(plan.candidates) or "none"))
    providers_cmd.show_served(plan.served)
    for line in plan.rewrites:
        report(output_stream, line)
    question = (setup_texts.REWRITE_QUESTION.format(key=key) if plan.rewrites
                else setup_texts.REMOVE_LINE_QUESTION.format(key=key))
    if not yes:
        if not interactive:
            return fail(f"models rm {key}: needs a terminal to confirm, or --yes; nothing changed")
        if not consent.confirm(question, input_stream=input_stream):
            raise setup_model.Declined()
    applied = setup_lines.apply_line_remove(runtime, plan, setup_model.Confirmation.given(plan))
    for line in applied.lines:
        report(output_stream, line)
    report(output_stream, "Captured aliases remain served until pruned.")
    return 0


def _models_show(runtime: runtime_mod.Runtime, key: str, resolved: bool, evidence_view: bool,
                 output_stream: TextIO) -> int:
    ctx = providers_cmd.operator_context(runtime)
    if evidence_view:
        evidence = operator_mod.load_evidence(ctx.env, ctx.schemas)
        found = evidence.lines.get(key) if evidence is not None else None
        output_stream.write(strict_json.pretty_file_bytes({"key": key, "evidence": found}).decode("utf-8"))
        return 0
    line = ctx.layer.lines.get(key)
    if resolved:
        if line is None:
            return fail(f"models show {key}: no valid operator declaration (claude-multi providers validate)")
        document = {"key": key, "definition_digest": line.definition_digest, "entry": line.core_entry,
                    "family": line.family, "origin": line.origin, "selector_mode": line.selector_mode,
                    "source": line.source, "provider": line.provider_id}
        output_stream.write(strict_json.pretty_file_bytes(document).decode("utf-8"))
        return 0
    lcat = runtime.lineup_catalog()
    if line is None and key in lcat.lines:
        entry = lcat.lines[key]
        report(output_stream, f"{key}: {lcat.origin(key)} line · {entry['provider']}/{entry['wire_model']} · "
                              f"status {entry.get('status', 'active')}")
        return 0
    if line is None:
        status = providers_cmd.line_status(key, ctx, providers_cmd.admitted_keys(runtime))
        return fail(f"models show {key}: {status} (claude-multi providers validate)")
    entry = line.core_entry
    evidence = operator_mod.load_evidence(ctx.env, ctx.schemas)
    found = evidence.lines.get(key) if evidence is not None else None
    if found is None:
        smoke = "not run"
    elif found.get("digest") != line.definition_digest:
        smoke = "stale definition"
    else:
        check = found.get("checks", {}).get("smoke")
        smoke = f"{check['result']} ({check.get('reason', 'no reason')})" if check else "not run"
    provider = lcat.providers[line.provider_id]
    qualification = operator_mod.evidence_view(
        evidence, key, digest=line.definition_digest, levels=entry["efforts"],
        contracts=runtime.contract_identity().as_document(), pool_line=provider["transport"]["kind"] == "oauth-pool",
    )
    eff = runtime.current_effective()
    use = ("usable" if settings_mod.line_offered(key, entry, eff) else
           eff.unavailable_lines.get(key, f"provider {line.provider_id} is off in Settings"))
    selectors = ", ".join(selector for _level, selector, _c in catalog.line_selectors(dict(entry)))
    report(output_stream, f"{key}: {line.origin} line in {line.source} · {line.provider_id}/{entry['wire_model']}")
    report(output_stream, f"  status {providers_cmd.line_status(key, ctx, providers_cmd.admitted_keys(runtime))} · "
                          f"{_agent_status(line)} · class {entry['context']['ordinary_profile']} · validated floor "
                          f"{entry['context']['validated_tokens']}")
    report(output_stream, f"  selectors {selectors} · default effort {entry['default_effort']}")
    report(output_stream, f"  use: {use} · family: {line.family}")
    report(output_stream, f"  qualification: {qualification.state}"
                          + (f" ({', '.join(qualification.gaps)})" if qualification.gaps else "")
                          + (f" · exact-client: {qualification.exact_client}"
                             if qualification.exact_client != "not-required" else ""))
    report(output_stream, f"  smoke evidence: {smoke} · definition {line.definition_digest[:16]}")
    return 0


def _check_path(result: qualify_mod.CheckResult) -> tuple[str, ...]:
    if result.check == qualify_mod.CHECK_EFFORTS:
        return ("efforts", str(result.item))
    if result.check == qualify_mod.CHECK_TOOLS:
        return ("tools", str(result.item))
    return (result.check,)


def _result_line(key: str, result: qualify_mod.CheckResult) -> str:
    name = {qualify_mod.CHECK_EFFORTS: f"effort {result.item}", qualify_mod.CHECK_TOOLS: f"tools ({result.item})",
            qualify_mod.CHECK_EXACT: "exact-client"}.get(result.check, result.check)
    return f"{name} {key}: {result.text()}"


def exact_client_input(runtime: runtime_mod.Runtime, ctx: providers_cmd.OperatorContext,
                       key: str) -> qualify_mod.ExactClientInput:
    """The line's production compile as an admitted Direct lead (the scope a
    launch would write), the input of the offline exact-client check."""

    import copy

    from claude_multi import profile as profile_mod
    from claude_multi import scope as scope_mod

    line = ctx.layer.lines[key]
    entry = line.core_entry
    docs = operator_mod.merge_docs(ctx.docs, ctx.layer, legacy=ctx.legacy)
    lcat = profile_mod.LineupCatalog.from_docs(docs)
    eff = settings_mod.effective({"version": 1, "admitted_lines": [key]}, provider_ids=lcat.providers,
                                 line_keys=lcat.lines)
    seed = copy.deepcopy(runtime.catalog.seed_profiles["direct"])
    seed.pop("seed", None)
    seed.update(name="qualify", lead={"model": key, "effort": entry["default_effort"]})
    lineup = profile_mod.resolve(seed, lcat, effective=eff)
    plan = scope_mod.compile_lineup_scope(lineup, lcat, eff, runtime.catalog.prompt_bodies,
                                          scope_mod.catalog_meta_v2(docs), lineup_generation=1)
    selector = lineup.lead.binding.selector
    compiled = plan.settings.get("env", {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    client = entry["context"]["client_tokens"]
    client_effort = qualify_mod.is_client_effort(entry)
    efforts = qualify_mod.line_levels(entry) if client_effort else (entry["default_effort"],)
    return qualify_mod.ExactClientInput(
        key=key, selector=selector, wire=selector.removesuffix("[1m]"), efforts=tuple(efforts),
        client_effort=client_effort, output_limit=line.output_tokens,
        expected_window={"client": client, "compiled": int(compiled) if compiled is not None else client},
        settings=plan.settings, plan=plan,
    )


def _qualify_authority(runtime: runtime_mod.Runtime, ctx: providers_cmd.OperatorContext, key: str,
                       render: tuple[str | None, str], contracts: Any) -> tuple[Any, ...]:
    """What the qualification consent authorizes, as one comparable value:
    the line definition, its provider's route and transport authorization,
    the credential's presence (by name), the loaded render and the contract
    identity. Built from ``ctx`` (no network, no secret value)."""

    line = ctx.layer.lines.get(key)
    if line is None:
        return (None,)
    pid = line.provider_id
    resolved = ctx.layer.providers.get(pid)
    provider = resolved.entry if resolved is not None else ctx.docs["providers"]["providers"].get(pid)
    selection = operator_mod.transport_selections(ctx.docs, ctx.ledger).get(pid)
    secret = (selection.alternative.secret_name if selection is not None and selection.alternative is not None
              else (((provider or {}).get("transport", {}).get("auth") or {}).get("secret_ref") or "")
              .removeprefix("env:") or None)
    present = secret is not None and _credential_present(ctx, provider or {}, secret)
    return (line.definition_digest, pid, ctx.layer.route_status.get(pid, "catalog"),
            resolved.route_digest if resolved is not None else None,
            strict_json.canonical_file_bytes(provider) if provider is not None else None,
            selection, secret, present, render, contracts,
            catalog.keyed_compat_problem(provider, runtime.catalog.docs["gateway"]))


def _qualify_revalidator(runtime: runtime_mod.Runtime, key: str, consented: tuple[Any, ...]) -> Any:
    """``before_send`` for the battery: re-derive the authority immediately
    before every request (no lock held, no secret value read); any change
    stops the remaining requests and records nothing."""

    def check(sent: int) -> None:
        text = qualify_mod.STALE_CONFIRM_TEXT if sent == 0 else qualify_mod.STALE_SEND_TEXT
        sessions.check_state_marker(runtime.session_store.root)
        try:
            fresh = _qualification_context(runtime, key, f"qualify {key}")
            providers_cmd.require_ledger(fresh, f"qualify {key}")
        except providers_cmd._REFUSALS:
            raise OperatorCommandError(text.format(key=key)) from None
        sentinel, served, problem = render_identity(runtime)
        line = fresh.layer.lines.get(key)
        if line is None or _served_check(key, line, served):
            raise OperatorCommandError(text.format(key=key))
        if _qualify_authority(runtime, fresh, key, (sentinel, problem), runtime.contract_identity()) != consented:
            raise OperatorCommandError(text.format(key=key))

    return check


def _models_qualify(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                    output_stream: TextIO) -> int:
    """``models qualify KEY [checks]``: one consent listing every
    possible request, the bounded battery through the loopback gateway, the
    offline exact-client check for first-party pool lines, then a revalidated
    evidence commit (never an admission, never a declaration edit)."""

    key = args.key
    verb = f"qualify {key}"
    consent.require_human(f"models qualify {key}", runtime.gateway_environ())
    if key in runtime.catalog.lines:
        return fail(f"models qualify {key}: catalog lines carry reviewed evidence; qualify applies to operator lines")
    ctx = _qualification_context(runtime, key, verb)
    providers_cmd.require_ledger(ctx, verb)
    line = _operator_line(ctx, key, verb)
    checks = qualify_mod.requested_checks(
        smoke=bool(getattr(args, "smoke", False)), efforts=bool(getattr(args, "efforts", False)),
        tools=bool(getattr(args, "tools", False)), stream=bool(getattr(args, "stream", False)),
        context=getattr(args, "context", None), agents=bool(getattr(args, "agents", False)),
    )
    pid = line.provider_id
    provider = (ctx.layer.providers[pid].entry if pid in ctx.layer.providers
                else ctx.docs["providers"]["providers"][pid])
    selection = operator_mod.transport_selections(ctx.docs, ctx.ledger).get(pid)
    try:
        plan = qualify_mod.build_plan(
            key, line.core_entry, digest=line.definition_digest, provider_id=pid, provider=provider,
            checks=checks, tools_variant=getattr(args, "tool_choice", None) or qualify_mod.TOOLS_FORCED,
            context_tokens=getattr(args, "context", None), t2=ctx.layer.providers.get(pid), transport=selection,
            keyed_replay=catalog.is_keyed_compat(provider),
        )
    except qualify_mod.QualifyError as exc:
        raise providers_cmd.OperatorUsageError(str(exc)) from exc
    # Refusals before any request (and before the consent).
    route = ctx.layer.route_status.get(pid, "catalog")
    if route not in operator_mod.ROUTE_USABLE:
        raise OperatorCommandError(f"{verb}: " + operator_mod.route_status_text(pid, route))
    if selection is not None and selection.problem is not None:
        raise OperatorCommandError(f"{verb}: {selection.problem} — nothing sent")
    secret = (selection.alternative.secret_name if selection is not None and selection.alternative is not None
              else ((provider["transport"].get("auth") or {}).get("secret_ref") or "").removeprefix("env:") or None)
    if secret is not None and not _credential_present(ctx, provider, secret):
        raise OperatorCommandError(f"{verb}: credential {secret} is not set — claude-multi providers set-key {pid}")
    sentinel, served, problem = render_identity(runtime)
    if problem:
        raise OperatorCommandError(f"{verb}: {problem} — nothing sent")
    unserved = _served_check(key, line, served)
    if unserved:
        raise OperatorCommandError(f"{verb}: {unserved} — nothing sent")
    sessions.check_state_marker(runtime.session_store.root)
    contracts = runtime.contract_identity()
    consented = _qualify_authority(runtime, ctx, key, (sentinel, problem), contracts)
    if not consent.confirm(qualify_mod.consent_text(plan), input_stream=input_stream):
        return fail(f"{verb}: declined — nothing sent")
    # The frozen consent contract: revalidate immediately before every send
    # (a change while the prompt was open, or between two requests, stops the
    # remaining requests), and again under the lock before the commit.
    results = list(qualify_mod.run_battery(plan, runtime.qualify_post,
                                           before_send=_qualify_revalidator(runtime, key, consented)))
    if plan.exact_client:
        outcome = runtime.run_exact_client(exact_client_input(runtime, ctx, key))
        results.append(qualify_mod.exact_client_result(outcome))
    at = operator_mod.utc_stamp()
    # Evidence-only commit phase (no served change, no preview).
    with providers_cmd.operator_write(runtime, preflight=None):
        fresh = providers_cmd.operator_context(runtime)
        providers_cmd.require_ledger(fresh, verb)
        now = fresh.layer.lines.get(key)
        after, served_after, problem_after = render_identity(runtime)
        if (now is None or _served_check(key, now, served_after) or
                _qualify_authority(runtime, fresh, key, (after, problem_after), runtime.contract_identity()) != consented):
            raise OperatorCommandError(qualify_mod.STALE_TEXT.format(key=key))
        recorded = operator_mod.record_checks(
            fresh.env, fresh.schemas, key, digest=line.definition_digest, host=plan.host,
            versions=runtime.evidence_versions(),
            results=[(_check_path(result), result.record(at, contracts)) for result in results],
        )
    floor = line.core_entry["context"]["validated_tokens"]
    for result in results:
        if result.check == qualify_mod.CHECK_CONTEXT:
            # The floor a reload resolves: from the evidence actually persisted.
            after_floor, _when = operator_mod.evidence_floor(
                recorded, key, digest=line.definition_digest,
                declared=line.core_entry["context"]["declared_tokens"], default_floor=floor)
            report(output_stream, qualify_mod.context_text(key, result, floor, after_floor))
        else:
            report(output_stream, _result_line(key, result))
    report(output_stream, f"evidence recorded for {line.definition_digest[:16]}; qualification never admits "
                          "or enables agents")
    return 0 if all(result.result == qualify_mod.PASS for result in results) else 1


def _models_command(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO, output_stream: TextIO,
    interactive: bool,
) -> int:
    """``claude-multi models [VERB]``: exit 0 ok, 1 refused, 2 usage."""

    command = getattr(args, "models_command", None)
    candidates = bool(getattr(args, "candidates", False))
    if getattr(args, "candidates_all", False) and not candidates:
        consent.prompt_stream().write("claude-multi models: --all requires --candidates\n")
        return 2
    if candidates and command is not None:
        consent.prompt_stream().write(f"claude-multi models: --candidates cannot accompany `models {command}`\n")
        return 2
    if candidates:
        return _models_candidates(runtime, bool(getattr(args, "candidates_all", False)), output_stream)
    if command is None:
        return _models_listing(runtime, output_stream)
    if command == "list":
        return _models_list(runtime, args, output_stream)
    try:
        if command == "add":
            return _models_add(runtime, args, output_stream=output_stream)
        if command == "admit":
            return _models_admit(runtime, args.key, input_stream=input_stream, output_stream=output_stream)
        if command == "revoke":
            return _models_revoke(runtime, args.key, input_stream=input_stream, output_stream=output_stream,
                                  interactive=interactive, yes=bool(getattr(args, "yes", False)))
        if command == "edit":
            return _models_edit(runtime, args.key, input_stream=input_stream, output_stream=output_stream,
                                interactive=interactive)
        if command == "rm":
            return _models_rm(runtime, args.key, args.successor, input_stream=input_stream,
                              output_stream=output_stream, interactive=interactive,
                              yes=bool(getattr(args, "yes", False)))
        if command == "show":
            return _models_show(runtime, args.key, args.resolved, args.evidence, output_stream)
        if command == "qualify":
            return _models_qualify(runtime, args, input_stream=input_stream, output_stream=output_stream)
    except providers_cmd.OperatorUsageError as exc:
        consent.prompt_stream().write(f"claude-multi models {command}: {exc}\n")
        return 2
    except sessions.StateMarkerError:
        raise
    except KeyboardInterrupt:
        consent.prompt_stream().write("claude-multi: cancelled\n")
        return 130
    except providers_cmd._REFUSALS as exc:
        return providers_cmd.refusal_exit(exc)
    raise cli_errors.CLIError(f"unsupported models command {command!r}")
