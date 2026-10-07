"""The ``discover`` command: approved plans, --all and --feed.

Every provider listing, the codex plan listing and the public feed peek pass
the one human guard (``consent.require_human``): no Claude-session marker
(present, even empty) and real stdin/stdout terminals, before any secret
access, plan or request. The earlier in-session y/N and the
``--yes-i-approve-this-provider-call`` flag are retired; the flag is still
parsed only to print its migration error (exit 2).

A plan is immutable and names every request, its auth kind and secret
*name* (never a value), why and the caps; one y/N (default no) covers it.
After the answer the plan is derived again: a changed fingerprint (a
provider, route, secret name or URL edit while the prompt was open) sends
nothing. Listings are observations: marks and drift are printed, absence
only from a complete listing, and nothing is declared unless ``--add``
names the wire (the declaration transaction, New · Off).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Mapping, TextIO, TYPE_CHECKING

import claude_multi.cli.commands.models as models_cmd
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.consent as consent
from claude_multi import catalog as catalog_mod
from claude_multi import custom
from claude_multi import discovery
from claude_multi import operator as operator_mod
from claude_multi import proxy as proxy_mod
from claude_multi import secret_store
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import strict_json

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


CLAUDE_SESSION_ENV_KEYS = consent.SESSION_MARKERS
OUTSIDE_SESSION_HINT = "run it in a separate shell"
LEGACY_FLAG_ERROR = ("--yes-i-approve-this-provider-call cannot replace interactive approval;\n"
                     "run discover in a terminal outside Claude Code sessions.")

fail = providers_cmd.fail
report = providers_cmd.report


def _claude_session_marker(environ: dict[str, str]) -> str | None:
    """The first Claude-session variable set in ``environ`` (None outside)."""

    return consent.session_marker(environ)


def _provider_call_tty() -> bool:
    """Seam: this process's stdin AND stdout are real terminals (never /dev/tty)."""

    return consent.stdio_ttys()


def _in_session_provider_call_gate(verb: str, environ: Mapping[str, str]) -> None:
    """The unified guard for every discovery provider call (the earlier
    in-session y/N ended; kept by name for the compatibility map)."""

    marker = consent.session_marker(environ)
    if marker is not None:
        raise consent.ConsentRefused(consent.guard_text(verb, f"{marker} set"))
    if not _provider_call_tty():
        raise consent.ConsentRefused(consent.guard_text(verb, "stdin/stdout is not a terminal"))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _transport(runtime: runtime_mod.Runtime) -> Any:
    return runtime.listing_transport or proxy_mod.bounded_get


def _fetch(runtime: runtime_mod.Runtime, url: str, headers: dict[str, str]) -> bytes:
    return _transport(runtime)(url, headers, deadline=discovery.LISTING_DEADLINE_SECONDS,
                               max_bytes=discovery.LISTING_MAX_BYTES)


def _view(runtime: runtime_mod.Runtime) -> tuple[providers_cmd.OperatorContext, dict[str, Any]]:
    ctx = providers_cmd.operator_context(runtime)
    docs = operator_mod.merge_docs(runtime.catalog.docs, ctx.layer, legacy=ctx.legacy)
    return ctx, docs


def _transports(ctx: providers_cmd.OperatorContext) -> dict[str, str]:
    """The reviewed transport choices in effect, for honest plans."""

    return dict(ctx.ledger.transport_choices) if ctx.ledger is not None else {}


def _plan_one(runtime: runtime_mod.Runtime, provider_id: str, *, account: bool = False,
              wire: str | None = None) -> discovery.ListingCall:
    ctx, docs = _view(runtime)
    return discovery.plan_provider(provider_id, docs=docs, layer=ctx.layer, descriptors=proxy_mod._LISTING_SUPPORT,
                                   transports=_transports(ctx), account=account, wire=wire)


def _revalidate(runtime: runtime_mod.Runtime, call: discovery.ListingCall) -> None:
    """Re-derive the call; any difference is a stale plan (nothing sent)."""

    try:
        fresh = _plan_one(runtime, call.provider_id, account=call.account, wire=call.wire)
    except discovery.PlanRefusal as exc:
        if exc.route:
            raise providers_cmd.OperatorCommandError(exc.reason) from None
        raise providers_cmd.OperatorCommandError(
            discovery.STALE_TEXT.format(provider=call.provider_id)) from None
    if fresh.fingerprint != call.fingerprint:
        raise providers_cmd.OperatorCommandError(discovery.STALE_TEXT.format(provider=call.provider_id))


def _secret(runtime: runtime_mod.Runtime, call: discovery.ListingCall) -> str | None:
    if call.secret_name is None:
        return None
    try:
        value = secret_store.default_store(runtime.gateway_environ()).get(call.secret_name)
    except secret_store.SecretStoreError as exc:
        raise providers_cmd.OperatorCommandError(f"discover {call.provider_id}: {exc}") from None
    if value is None:
        raise providers_cmd.OperatorCommandError(
            f"discover {call.provider_id}: credential {call.secret_name} is not set — nothing sent; "
            f"claude-multi providers set-key {call.provider_id}")
    return value


def _execute(runtime: runtime_mod.Runtime, call: discovery.ListingCall) -> proxy_mod.ListingResult:
    """Revalidate, read the secret by name, revalidate again, send ONE request."""

    _revalidate(runtime, call)
    secret = _secret(runtime, call)
    _revalidate(runtime, call)
    if call.auth == "pool-credential":
        gateway_info = runtime.catalog.docs["gateway"]["gateway"]
        listed = proxy_mod.list_codex_plan_models(
            gateway_info, environ=runtime.environ,
            fetch=lambda url, headers: _fetch(runtime, url, headers))
        return proxy_mod.ListingResult(tuple(dict(item) for item in listed), True)
    headers = proxy_mod.listing_headers(call.auth, call.shape, secret=secret, header=call.header)
    try:
        raw = _fetch(runtime, call.url, headers)
    except Exception as exc:
        # A transport exception may contain headers; only fixed categories leave here.
        detail = str(exc) if isinstance(exc, proxy_mod.ListingFailure) else type(exc).__name__
        raise proxy_mod.ProxyError(detail) from None
    if secret and secret.encode() in raw:
        raise proxy_mod.ProxyError("listing response contained credential material")
    result = proxy_mod.parse_listing(raw, call.shape)
    if secret and secret in json.dumps(result.entries, ensure_ascii=False):
        raise proxy_mod.ProxyError("listing response contained credential material")
    if call.wire is not None and (len(result.entries) != 1 or result.entries[0]["id"] != call.wire):
        raise proxy_mod.ProxyError("model endpoint returned a different id")
    return result


def account_listing(runtime, call, public, *, confirm, notice):
    """An optional second request with its own consent; public rows always survive."""

    if call.provider_id != "openrouter" or call.tier != "T1" or call.wire is not None:
        return public
    account = _plan_one(runtime, call.provider_id, account=True)
    store = secret_store.default_store(runtime.gateway_environ())
    if not store.is_set(account.secret_name):
        notice("OpenRouter: public listing only (no key configured); stealth ids can be added directly.")
        return public
    if not confirm(discovery.consent_text([account])):
        notice("OpenRouter: account-filtered listing declined; keeping the public listing.")
        return public
    try:
        result = _execute(runtime, account)
    except proxy_mod.ProxyError as exc:
        notice(f"OpenRouter: account-filtered listing failed ({exc}); keeping the public listing.")
        return public
    return proxy_mod.ListingResult(discovery.merge_listings(public.entries, result.entries),
                                   public.complete and result.complete)


def listing(runtime, provider_id, *, confirm, notice):
    """Shared CLI/TUI listing journey, with every request disclosed separately."""

    consent.require_human(f"discover {provider_id}", runtime.gateway_environ())
    call = _plan_one(runtime, provider_id)
    if call.secret_name is not None and not secret_store.default_store(runtime.gateway_environ()).is_set(call.secret_name):
        raise providers_cmd.OperatorCommandError(
            f"discover {provider_id}: credential {call.secret_name} is not set — nothing sent; "
            f"claude-multi providers set-key {provider_id}")
    if not confirm(discovery.consent_text([call])):
        return None
    result = _execute(runtime, call)
    result = account_listing(runtime, call, result, confirm=confirm, notice=notice)
    return call, result


def lookup_stealth(runtime, wire, *, confirm, notice):
    """Anonymous add-by-id lookup. Failure leaves facts unknown, never guessed."""

    consent.require_human("discover openrouter", runtime.gateway_environ())
    call = _plan_one(runtime, "openrouter", wire=wire)
    unknown = {"id": wire, "display_name": "", "context_length": None, "openrouter": True}
    if not confirm(discovery.consent_text([call])):
        notice("OpenRouter: model lookup declined; facts unknown; manual declaration remains available.")
        return call, unknown
    try:
        result = _execute(runtime, call)
    except proxy_mod.ProxyError as exc:
        notice(f"OpenRouter: model lookup failed ({exc}); facts unknown; manual declaration remains available.")
        return call, unknown
    return call, result.entries[0]


def _marks_inputs(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    ctx = providers_cmd.operator_context(runtime)
    return {
        "operator": discovery.operator_states(ctx.layer, ctx.ledger, providers_cmd.admitted_keys(runtime)),
        "catalog_lines": runtime.catalog.lines,
        "legacy_models": custom.load_registry(runtime.environ).get("models", {}),
        "retired": runtime.catalog.retired,
    }


def _report_listing(
    runtime: runtime_mod.Runtime, call: discovery.ListingCall, result: proxy_mod.ListingResult,
    output_stream: TextIO, *, heading: bool,
) -> list[discovery.MarkedEntry]:
    if heading:
        report(output_stream, f"== {call.provider_id} (GET {call.url})")
    inputs = _marks_inputs(runtime)
    marked = discovery.mark_listing(call.provider_id, result.entries, **inputs)
    if not marked:
        report(output_stream, f"{call.provider_id}: the provider advertised no models")
    for item in marked:
        report(output_stream, discovery.entry_text(item))
    for line in discovery.drift_lines(call.provider_id, result.entries, complete=result.complete,
                                      operator=inputs["operator"], today=_today()):
        report(output_stream, line)
    if not result.complete:
        reason = "single-model lookup" if call.wire is not None else "listing incomplete (the provider reported more pages)"
        report(output_stream, f"{call.provider_id}: {reason}; absence is not concluded")
    return marked


def _usage(message: str) -> int:
    consent.prompt_stream().write(f"claude-multi discover: {message}\n")
    return 2


def _discover_pool_listing(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO, output_stream: TextIO,
    interactive: bool = True,
) -> int:
    """``discover openai``: the codex plan listing through ONE pool
    credential, its own single-provider consent (never folded into --all)."""

    return _discover_provider(runtime, args, input_stream=input_stream, output_stream=output_stream)


def _discover_provider(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO, output_stream: TextIO,
) -> int:
    provider_id = args.provider
    verb = f"discover {provider_id}"
    adds = args.discover_add or []
    confirm = lambda text: consent.confirm(text, input_stream=input_stream)
    notice = lambda text: report(output_stream, text)
    if provider_id == "openrouter" and adds and all(discovery.stealth_id(wire) for wire in adds):
        code = 0
        for wire in adds:
            call, entry = lookup_stealth(runtime, wire, confirm=confirm, notice=notice)
            marked = _report_listing(runtime, call, proxy_mod.ListingResult((entry,), False),
                                     output_stream, heading=False)
            one = argparse.Namespace(**vars(args))
            one.discover_add = [wire]
            result = _declare(runtime, one, call, marked, output_stream=output_stream)
            code = code or result
        return code
    try:
        observed = listing(runtime, provider_id, confirm=confirm, notice=notice)
    except discovery.PlanRefusal as exc:
        return fail(exc.reason)
    except proxy_mod.ProxyError as exc:
        return fail(f"{verb}: listing failed ({exc})")
    if observed is None:
        consent.prompt_stream().write(f"{verb}: declined — nothing sent\n")
        return 0
    call, result = observed
    marked = _report_listing(runtime, call, result, output_stream, heading=False)
    if args.discover_add:
        return _declare(runtime, args, call, marked, output_stream=output_stream)
    return 0


def _registry_model(runtime: runtime_mod.Runtime, provider: Mapping[str, Any], wire: str) -> tuple[Any, str | None]:
    section = catalog_mod.registry_section(dict(provider))
    root = catalog_mod.registry_dir(runtime.environ, runtime.asset_root)
    if section is None or root is None:
        return None, None
    try:
        registry = catalog_mod.load_pinned_registry(root)
    except catalog_mod.CatalogError:
        return None, None
    model = registry.meta.get(section, {}).get(wire)
    return model, f"pinned registry {section} {_today()}"


def missing_context_text(provider: str, wire: str) -> str:
    return (f"discover {provider} --add {wire}: the listing states no context — "
            f'claude-multi models add {provider} {wire} --context N --source docs --source-ref "<URL, date>"')


def _declare(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, call: discovery.ListingCall,
    marked: list[discovery.MarkedEntry], *, output_stream: TextIO,
) -> int:
    """``--add``: the explicit declaration from this listing (one transaction)."""

    verb = f"discover {call.provider_id}"
    by_id = {item.entry["id"]: item for item in marked}
    ctx, docs = _view(runtime)
    provider = docs["providers"]["providers"].get(call.provider_id)
    if provider is None:
        return fail(f"{verb}: provider {call.provider_id} is no longer declared — nothing declared")
    agent_efforts = runtime.catalog.agent_efforts
    code = 0
    for wire in args.discover_add:
        if call.provider_id == "openrouter" and ":" in wire:
            code = fail(f"{verb}: {wire}: not addable in this release")
            continue
        item = by_id.get(wire)
        if item is None:
            code = fail(f"{verb}: {wire} is not in the listing — declare it manually: "
                        f"claude-multi models add {call.provider_id} {wire} --context N --source docs")
            continue
        if item.mark not in ("candidate", "new"):
            code = fail(f"{verb}: {wire} is {item.mark} — declaration never edits an existing line")
            continue
        taken = set((docs.get("models-v2") or docs["models"])["models"]) | set(ctx.layer.lines)
        key = args.discover_as or discovery.derived_key(wire, taken)
        if key is None:
            code = fail(f"{verb}: no valid key derives from {wire!r} — name it with --as custom-<name>")
            continue
        model, registry_ref = _registry_model(runtime, provider, wire)
        source_url = discovery.OPENROUTER_MODELS_URL + "/user" if item.entry.get("account_only") else call.url
        source_ref = f"{source_url} {_today()}"[:256]
        try:
            line = discovery.declaration_line(
                item.entry, provider=provider, agent_efforts=agent_efforts, source_ref=source_ref,
                context_override=args.discover_context, over_listed=args.over_listed,
                registry_model=model, registry_ref=registry_ref)
        except discovery.DeclarationRefusal as exc:
            if "the listing states no context" in str(exc):
                code = fail(missing_context_text(call.provider_id, wire))
                continue
            code = fail(f"{verb}: {wire}: {exc} — nothing declared")
            continue
        if args.discover_family is not None:
            line["family"] = args.discover_family
        result = models_cmd.declare_line(runtime, call.provider_id, key, line, verb=verb,
                                         output_stream=output_stream)
        code = code or result
    return code


def _discover_all(
    runtime: runtime_mod.Runtime, *, input_stream: TextIO, output_stream: TextIO,
) -> int:
    ctx, docs = _view(runtime)
    eff = runtime.current_effective()
    store = secret_store.default_store(runtime.gateway_environ())

    def present(name: str) -> bool:
        try:
            return store.is_set(name)
        except secret_store.SecretStoreError:
            return False

    plan = discovery.plan_all(docs=docs, layer=ctx.layer, descriptors=proxy_mod._LISTING_SUPPORT,
                              enabled=lambda pid: settings_mod.provider_enabled(eff, pid),
                              secret_present=present, transports=_transports(ctx))
    if not plan.calls:
        for item in plan.skipped:
            consent.prompt_stream().write(f"skipped {item.provider_id}: {item.reason}\n")
        if plan.codex_excluded:
            consent.prompt_stream().write(discovery.CODEX_EXCLUDED + "\n")
        report(output_stream, "discover --all: no provider has a listing to request — nothing sent")
        return 0
    if not consent.confirm(discovery.consent_text(plan.calls, plan.skipped, codex_excluded=plan.codex_excluded),
                           input_stream=input_stream):
        consent.prompt_stream().write("discover --all: declined — nothing sent\n")
        return 0
    failed = 0
    for call in plan.calls:
        try:
            result = _execute(runtime, call)
        except providers_cmd.OperatorCommandError as exc:
            # A changed plan or authorization stops the batch.
            report(output_stream, f"== {call.provider_id} (GET {call.url})")
            fail(f"{exc}; later providers were not requested")
            return 1
        except proxy_mod.ProxyError as exc:
            report(output_stream, f"== {call.provider_id} (GET {call.url})")
            report(output_stream, f"{call.provider_id}: listing failed ({exc})")
            failed += 1
            continue
        result = account_listing(
            runtime, call, result,
            confirm=lambda text: consent.confirm(text, input_stream=input_stream),
            notice=lambda text: report(output_stream, text))
        _report_listing(runtime, call, result, output_stream, heading=True)
    return 1 if failed else 0


def _discover_feed(runtime: runtime_mod.Runtime, *, input_stream: TextIO, output_stream: TextIO) -> int:
    if not consent.confirm(discovery.feed_consent_text(), input_stream=input_stream):
        consent.prompt_stream().write("discover --feed: declined — nothing sent\n")
        return 0
    try:
        raw = _fetch(runtime, discovery.FEED_URL, {"Accept": "application/json"})
        feed = strict_json.loads(raw, strict_json.JSONLimits(max_string=1024 * 1024))
    except proxy_mod.ProxyError as exc:
        return fail(f"discover --feed: the feed request failed ({exc})")
    except (ValueError, RecursionError) as exc:
        return fail(f"discover --feed: the feed is not valid JSON ({type(exc).__name__})")
    root = catalog_mod.registry_dir(runtime.environ, runtime.asset_root)
    registry = None
    if root is not None:
        try:
            registry = catalog_mod.load_pinned_registry(root)
        except catalog_mod.CatalogError:
            registry = None
    try:
        added = discovery.feed_difference(feed, registry)
    except ValueError as exc:
        return fail(f"discover --feed: {exc}")
    report(output_stream, "Public feed difference — advisory, not a pin update")
    if registry is None:
        report(output_stream, "Pinned registry unavailable; every feed id is listed.")
    if added:
        report(output_stream, "A future pin bump may add:")
        for section in sorted(added):
            report(output_stream, f"  {section}: {' '.join(added[section])}")
    else:
        report(output_stream, "The feed lists nothing the pinned registry lacks.")
    report(output_stream, "No catalog, registry, provider, admission, or evidence file changed.")
    return 0


def _discover_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """``claude-multi discover PROVIDER | --all | --feed`` (exit 0 ok, 1 refused
    or a failed request, 2 usage)."""

    if getattr(args, "approve_provider_call", False):
        consent.prompt_stream().write(f"claude-multi: {LEGACY_FLAG_ERROR}\n")
        return 2
    provider = getattr(args, "provider", None)
    modes = [provider is not None, bool(getattr(args, "discover_all", False)),
             bool(getattr(args, "discover_feed", False))]
    if sum(modes) != 1:
        return _usage("name exactly one of PROVIDER, --all or --feed")
    adds = list(getattr(args, "discover_add", []) or [])
    family = args.discover_family
    declaring = adds or args.discover_as is not None or family is not None or args.discover_context is not None or args.over_listed
    if declaring and provider is None:
        return _usage("--add, --as, --family, --context and --over-listed need a PROVIDER")
    if (args.discover_as is not None or family is not None or args.discover_context is not None or args.over_listed) and not adds:
        return _usage("--as, --family, --context and --over-listed need --add WIRE")
    if args.discover_as is not None and len(adds) != 1:
        return _usage("--as names exactly one --add WIRE")
    if family is not None and len(adds) != 1:
        return _usage("--family labels exactly one --add WIRE")
    if args.discover_as is not None and not operator_mod.NEW_KEY.fullmatch(args.discover_as):
        return _usage(f"--as {args.discover_as!r} must match {operator_mod.NEW_KEY.pattern}")
    # Reject malformed labels before a listing. The declaration transaction
    # still runs the canonical per-model schema and printable-text validation.
    if family is not None and not (0 < len(family) <= 64 and family.isprintable()):
        return _usage("--family LABEL: nonempty, single-line printable text (at most 64 characters)")
    if args.over_listed is not None and not (0 < len(args.over_listed) <= discovery.OVER_LISTED_MAX
                                             and args.over_listed.isprintable()):
        return _usage(f"--over-listed REASON: 1..{discovery.OVER_LISTED_MAX} printable characters")
    verb = f"discover {provider}" if provider is not None else (
        "discover --all" if modes[1] else "discover --feed")
    try:
        # Before any plan, secret-store read or request.
        consent.require_human(verb, runtime.environ)
        if provider is not None:
            return _discover_provider(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if modes[1]:
            return _discover_all(runtime, input_stream=input_stream, output_stream=output_stream)
        return _discover_feed(runtime, input_stream=input_stream, output_stream=output_stream)
    except sessions.StateMarkerError:
        raise
    except providers_cmd._REFUSALS as exc:
        return fail(str(exc))
