"""The ``providers`` command (and ``migrate-custom``).

Declarations are T2 input (``providers.d/<id>.json``); route approvals,
admissions and captures are tool-owned ledger state. Reports go to stdout;
prompts and progress to stderr (``consent``). A declaration is allowed
anywhere (it is inert until approved and admitted); every verb that grants
authority (route approval, key import, migration grants) passes the shared
human guard first, before any secret access or side effect.

Every write validates the whole proposed layer first, takes the locks in
the frozen order (migration guard -> token-rotation lock, non-blocking ->
operator store lock -> Settings -> api-key leaf), re-renders under the
explicit policy and verifies the reload after the locks are released. A
render that refuses restores what this command changed, so providers.d and
the ledger never disagree with the served config. ``providers apply`` is
the sole served mutation command (with its preview and barrier here).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TextIO, TYPE_CHECKING

from claude_multi import catalog
from claude_multi import continuity as continuity_mod
from claude_multi import custom
from claude_multi import errors
from claude_multi import errors as cli_errors
from claude_multi import operator as operator_mod
from claude_multi import profile as profile_mod
from claude_multi import proxy as proxy_mod
from claude_multi import secret_store
from claude_multi import served_plan
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import strict_json
from claude_multi import termtext
import claude_multi.cli.consent as consent

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


class OperatorCommandError(cli_errors.CLIError):
    """A refused operator verb (exit 1: validation, safety, credential or commit)."""


class OperatorUsageError(cli_errors.CLIError):
    """Argument combinations argparse cannot express (exit 2)."""


# The command-level refusals that exit 1 (argparse misuse stays 2).
_REFUSALS = (
    cli_errors.CLIError, operator_mod.OperatorError, proxy_mod.ProxyError, settings_mod.SettingsError,
    secret_store.SecretStoreError, state.StateError, sessions.MigrationBusyError, errors.ClaudeMultiError,
)

TEMPLATES: Mapping[str, dict[str, Any]] = {
    "anthropic-compatible": {
        "version": 1,
        "provider": {
            "display": "Example",
            "kind": "anthropic-compatible",
            "base_url": "https://api.example.invalid/anthropic",
            "auth": {"kind": "bearer", "secret_ref": "env:EXAMPLE_API_KEY"},
            "independence_family": "example",
        },
        "lines": {
            "custom-example-model": {
                "wire_model": "example-model-1",
                "display": "Example model",
                "efforts": ["high"],
                "default_effort": "high",
                "context": {"declared_tokens": 131072, "source": "docs",
                            "source_ref": "https://docs.example.invalid/models"},
            },
        },
    },
    # Keyed generic chat; bearer auth from one env:NAME secret,
    # no static headers. Offered only while the trusted audit gate is open.
    "openai-compatible": {
        "version": 1,
        "provider": {
            "display": "Example chat",
            "kind": "openai-compatible",
            "base_url": "https://api.example.invalid/v1",
            "auth": {"kind": "bearer", "secret_ref": "env:EXAMPLE_CHAT_API_KEY"},
            "independence_family": "example",
        },
        "lines": {
            "custom-example-chat": {
                "wire_model": "example-chat-1",
                "display": "Example chat model",
                "efforts": ["low", "medium", "high"],
                "default_effort": "high",
                "context": {"declared_tokens": 131072, "source": "docs",
                            "source_ref": "https://docs.example.invalid/models"},
            },
        },
    },
    "openai-compatible-lan": {
        "version": 1,
        "provider": {
            "display": "LAN box",
            "kind": "openai-compatible-lan",
            "base_url": "http://box.lan:8000/v1",
            "auth": {"kind": "none"},
            "independence_family": "local",
        },
        "lines": {
            "custom-lan-model": {
                "wire_model": "lan-model",
                "display": "LAN model",
                "efforts": ["high"],
                "default_effort": "high",
                "context": {"declared_tokens": 32768, "source": "operator"},
            },
        },
    },
}


def fail(message: str) -> int:
    consent.prompt_stream().write(f"claude-multi: {termtext.visible_message(message)}\n")
    consent.prompt_stream().flush()
    return 1


def report(output_stream: TextIO, text: str) -> None:
    """One report line: each line and tab-separated field sanitized alone."""

    output_stream.write("\n".join("\t".join(termtext.visible_text(field) for field in line.split("\t"))
                                   for line in str(text).split("\n")) + "\n")


# ------------------------------------------------------------ shared operator context
@dataclass(frozen=True)
class OperatorContext:
    env: dict[str, str]
    docs: dict[str, Any]
    legacy: dict[str, Any]
    snapshot: operator_mod.OperatorSnapshot
    schemas: operator_mod.OperatorSchemas

    @property
    def layer(self) -> operator_mod.OperatorLayer:
        return self.snapshot.layer

    @property
    def ledger(self) -> operator_mod.OperatorLedger | None:
        return self.snapshot.ledger

    @property
    def read(self) -> operator_mod.ProvidersRead:
        return self.snapshot.read or operator_mod.ProvidersRead(
            operator_mod.providers_dir(self.env), False, False, {}, False, ())


def operator_context(runtime: runtime_mod.Runtime, *, metadata_only: bool = False) -> OperatorContext:
    env = runtime.gateway_environ()
    legacy = custom.load_registry(runtime.environ)
    snapshot = (operator_mod.load_snapshot(env, runtime.catalog.docs, asset_root=runtime.asset_root,
                                          legacy=legacy, secret_values=None)
                if metadata_only else runtime.operator_snapshot())
    schemas = snapshot.schemas or operator_mod.load_schemas(runtime.asset_root)
    return OperatorContext(env, runtime.catalog.docs, legacy, snapshot, schemas)


def stored_values(ctx: OperatorContext) -> frozenset[str]:
    return proxy_mod._stored_secret_values(ctx.env)


def proposed(ctx: OperatorContext, changes: Mapping[str, bytes | None]) -> operator_mod.OperatorLayer:
    # The stored-literal scan is lazy, invoked only when the candidate
    # layer has an independently valid contribution (a solely rejected
    # closed-keyed declaration never reads the store).
    return operator_mod.proposed_layer(
        ctx.docs, ctx.read, changes, schemas=ctx.schemas, ledger=ctx.ledger, legacy=ctx.legacy,
        secret_values=lambda: stored_values(ctx),
    )


def keyed_gate_refusal(runtime: runtime_mod.Runtime, kind: str | None) -> str | None:
    """The trusted accessor decides the keyed kind (closed: the same
    sanitized refusal the operator layer reports, before ``operator_context``)."""

    if kind == operator_mod.KEYED_KIND and not catalog.keyed_compat_audited(runtime.catalog.docs.get("gateway")):
        return catalog.KEYED_AUDIT_CLOSED
    return None


def require_ledger(ctx: OperatorContext, verb: str) -> None:
    if ctx.snapshot.ledger_error is not None:
        raise OperatorCommandError(f"{verb}: {ctx.snapshot.ledger_error} — nothing changed")


def ledger_refusal(runtime: runtime_mod.Runtime, verb: str) -> None:
    """``require_ledger`` without an operator context: the ledger read and
    schema check only (metadata; never the secret store)."""

    env = runtime.gateway_environ()
    if not os.path.lexists(operator_mod.ledger_path(env)):
        return
    try:
        operator_mod.load_ledger(env, operator_mod.load_schemas(runtime.asset_root))
    except operator_mod.OperatorError:
        raise OperatorCommandError(f"{verb}: {operator_mod.LEDGER_UNUSABLE} — nothing changed") from None


def refuse_blocking(layer: operator_mod.OperatorLayer, verb: str) -> None:
    """No silently partial render: the whole proposed layer must be valid."""

    blocking = operator_mod.blocking_problems(layer)
    if blocking:
        lines = [problem.text() for problem in blocking[:8]]
        if len(blocking) > 8:
            lines.append(f"… and {len(blocking) - 8} more")
        raise OperatorCommandError(f"{verb}: the operator layer would be invalid — nothing written:\n  "
                                   + "\n  ".join(lines))


def key_references(runtime: runtime_mod.Runtime) -> dict[str, list[str]]:
    """line key -> the profiles and named bindings that name it (metadata only)."""

    return runtime.operator_key_references()


def record_scan(runtime: runtime_mod.Runtime) -> continuity_mod.RecordScan:
    return continuity_mod.scan_records(runtime.session_store.root)


def live_users(scan: continuity_mod.RecordScan, aliases: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for alias in aliases:
        found |= set(scan.refs.get(alias, frozenset())) & set(scan.live)
    return frozenset(found)


def refuse_unknown_liveness(scan: continuity_mod.RecordScan, verb: str) -> None:
    if scan.directory_error is not None or scan.unreadable:
        ids = ", ".join(stem[:8] for stem in scan.unreadable) or scan.directory_error
        raise OperatorCommandError(f"{verb}: cannot prove liveness (unreadable session records: {ids}) — nothing changed")


def admitted_keys(runtime: runtime_mod.Runtime) -> frozenset[str]:
    try:
        return frozenset(runtime.settings_store.load().get("admitted_lines", []))
    except settings_mod.SettingsError as exc:
        raise OperatorCommandError(f"cannot read operator settings: {exc}") from exc


def line_status(key: str, ctx: OperatorContext, admitted: Iterable[str]) -> str:
    layer, ledger = ctx.layer, ctx.ledger
    if key in layer.pending:
        return "pending migration"
    if key in layer.displaced:
        return "displaced by the catalog"
    line = layer.lines.get(key)
    if line is None:
        return "invalid"
    grant = ledger.admissions.get(key) if ledger is not None else None
    if key in set(admitted) and grant is not None:
        if grant["digest"] != line.definition_digest:
            return "changed since admission (optional re-admit)"
        return "admitted"
    return "New · not admitted"


# ------------------------------------------------------------ the shared preflight
@dataclass(frozen=True)
class ServedPreflight:
    """A served mutation's preview plan plus the CAS sample its commit
    phase revalidates (``operator_write``)."""

    verb: str
    plan: served_plan.ServedChangePlan
    sample: str
    # The reference fingerprint of the exact record scan the displayed plan
    # was built from (a destructive plan revalidates it; None otherwise).
    references: str | None = None


def _authority(layer: operator_mod.OperatorLayer, ledger: operator_mod.OperatorLedger | None,
               admitted: Iterable[str]) -> dict[str, str]:
    """Separate admission badges, route approvals and transport choices.

    A badge conveys no use authority and never changes a served selector.
    """

    granted = set(admitted)
    found: dict[str, str] = {}
    for key, line in layer.lines.items():
        grant = ledger.admissions.get(key) if ledger is not None else None
        if key in granted and grant is not None:
            found[f"line {key}"] = ("admitted" if grant["digest"] == line.definition_digest
                                    else "changed since admission")
        else:
            found[f"line {key}"] = "New · not admitted"
    for key in granted - set(layer.lines):
        found[f"line {key}"] = "admitted"
    for pid, status in layer.route_status.items():
        found[f"route {pid}"] = status
    for pid, choice in (ledger.transport_choices if ledger is not None else {}).items():
        found[f"transport {pid}"] = choice
    return found


def _sample(runtime: runtime_mod.Runtime, *, references: bool = False) -> str:
    """What a confirmed served plan depends on (no secret value enters it):
    the published identities, providers.d bytes, the ledger, admissions,
    the legacy custom.json registry (a legacy wire or base URL
    is a candidate input too), the persisted continuity set, the root
    authority, the presence of every secret those inputs name and — with
    ``references`` — the record references.

    The preflight takes it BEFORE it reads any plan input, so a change that
    lands while the plan is built or shown differs at the commit phase."""

    env = runtime.gateway_environ()
    snapshot = runtime.operator_snapshot()
    read = snapshot.read
    published = proxy_mod.published_identity(runtime.home)
    try:
        admitted = sorted(runtime.settings_store.load().get("admitted_lines", []))
    except settings_mod.SettingsError:
        admitted = ["<unreadable>"]
    document = {
        "published": None if published.routes is None else {
            selector: route.as_document() for selector, route in published.routes.items()},
        "published_unknown": published.unknown,
        "files": {fid: hashlib.sha256(raw).hexdigest() for fid, raw in sorted((read.files if read else {}).items())},
        "ledger": snapshot.ledger.sha256 if snapshot.ledger is not None else snapshot.ledger_error,
        "admitted": admitted,
        "legacy": _legacy_digest(env),
        "continuity": _continuity_digest(runtime),
        "root": proxy_mod.root_authority(runtime.home),
        "key_file": key_file_identity(env),
        "secrets": sorted(name for name in _secret_names(runtime) if secret_store.default_store(env).is_set(name)),
    }
    if references:
        scan = continuity_mod.scan_records(runtime.session_store.root)
        document["references"] = served_plan.references_from_scan(scan).fingerprint()
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def _legacy_digest(env: Mapping[str, str]) -> str:
    """The legacy registry as the candidate render reads it (canonical bytes;
    an unloadable registry samples its error, never a value)."""

    try:
        registry = custom.load_registry(dict(env))
    except custom.CustomModelsError as exc:
        return f"<unloadable: {exc}>"
    return hashlib.sha256(strict_json.canonical_file_bytes(registry)).hexdigest()


def _continuity_digest(runtime: runtime_mod.Runtime) -> str | None:
    try:
        document = continuity_mod.read(runtime.home)
    except continuity_mod.ContinuityError as exc:
        return f"<unreadable: {exc}>"
    return None if document is None else hashlib.sha256(strict_json.canonical_file_bytes(document)).hexdigest()


def _legacy_secret_names(env: Mapping[str, str]) -> set[str]:
    try:
        registry = custom.load_registry(dict(env))
    except custom.CustomModelsError:
        return set()
    return {spec["secret_env"] for spec in registry.get("providers", {}).values()
            if isinstance(spec, Mapping) and isinstance(spec.get("secret_env"), str)}


def key_file_identity(env: Mapping[str, str]) -> list[str]:
    """``[source, real path]`` of the API-key file every reader uses now
    (an unreadable pointer samples its error, never a value)."""

    try:
        location = secret_store.key_file_location(env)
    except secret_store.SecretStoreError as exc:
        return ["unreadable", str(exc)]
    return [location.source, os.path.realpath(location.path)]


def _secret_names(runtime: runtime_mod.Runtime) -> list[str]:
    names = _legacy_secret_names(runtime.gateway_environ())
    for pid, provider in runtime.catalog.docs["providers"]["providers"].items():
        ref = ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref")
        if isinstance(ref, str):
            names.add(ref.removeprefix("env:"))
        # The key of a reviewed transport alternative counts too: switching
        # to it relies on that key being there (offered by this build or not).
        for choice in operator_mod.TRANSPORT_CHOICES:
            alternative = operator_mod.transport_alternative(pid, choice)
            if alternative is not None:
                names.add(alternative.secret_name)
    snapshot = runtime.operator_snapshot()
    for provider in snapshot.layer.providers.values():
        if provider.secret_name:
            names.add(provider.secret_name)
    return sorted(names)


def served_preflight(
    runtime: runtime_mod.Runtime,
    verb: str,
    *,
    changes: Mapping[str, bytes | None] | None = None,
    ledger_edit: Callable[[dict[str, Any]], None] | None = None,
    admit: Iterable[str] = (),
    revoke: Iterable[str] = (),
    layer: operator_mod.OperatorLayer | None = None,
    assume_present: Iterable[str] = (),
    assume_absent: Iterable[str] = (),
    provider_enabled: Mapping[str, bool] | None = None,
    candidate_environ: Mapping[str, str] | None = None,
    show: bool = True,
    sample: str | None = None,
) -> ServedPreflight:
    """The one planner every served mutator calls before its first
    configuration or authority write.

    Lock-free and write-free: the proposed providers.d ``changes``, the
    ``ledger_edit`` (applied to an in-memory copy) and the admission
    change are planned against the PUBLISHED render. The preview goes to
    the prompt stream. A removal or retarget with unknown live impact
    refuses here (nothing written); the returned sample is revalidated by
    :func:`operator_write` inside the barrier. ``candidate_environ`` is the
    environment the render will read once the change is made (another key
    file selected); by default the current one. ``sample`` is the CAS
    sample of a caller that took it before reading its own plan facts
    (:func:`_sample`); by default it is taken here, first.
    """

    # The CAS sample precedes every plan read (the plan can only be newer
    # than it, which the commit phase then refuses); the references come
    # from the very scan the displayed plan uses.
    sample = _sample(runtime) if sample is None else sample
    ctx = operator_context(runtime)
    ledger = ctx.ledger
    replace_ledger = False
    if ledger_edit is not None and ctx.snapshot.ledger_error is None:
        document = copy.deepcopy(operator_mod.ledger_document(ctx.ledger))
        ledger_edit(document)
        ledger = operator_mod.parse_ledger(strict_json.canonical_file_bytes(document),
                                           ctx.schemas.ledger)
        replace_ledger = True
    if layer is None and (changes or replace_ledger):
        layer = operator_mod.proposed_layer(
            ctx.docs, ctx.read, dict(changes or {}), schemas=ctx.schemas, ledger=ledger, legacy=ctx.legacy,
            secret_values=lambda: stored_values(ctx),
        )
    before_admitted = set(admitted_keys(runtime))
    after_admitted = (before_admitted | set(admit)) - set(revoke)
    authority_before = _authority(ctx.layer, ctx.ledger, before_admitted)
    authority_after = _authority(layer or ctx.layer, ledger, after_admitted)
    if provider_enabled:
        # A Settings provider toggle is an authority change (offered at
        # the next launch), never a served one.
        try:
            eff = runtime.current_effective()
        except cli_errors.CLIError:
            eff = None
        for pid, enabled in provider_enabled.items():
            was = settings_mod.provider_enabled(eff, pid) if eff is not None else None
            authority_before[f"provider {pid}"] = "unknown" if was is None else ("on" if was else "off")
            authority_after[f"provider {pid}"] = "on" if enabled else "off"
    published = proxy_mod.published_identity(runtime.home)
    render_environ = dict(candidate_environ) if candidate_environ is not None else runtime.gateway_environ()
    candidate = proxy_mod.candidate_document(
        runtime.home, environ=render_environ, layer=layer, ledger=ledger,
        replace_ledger=replace_ledger, state_root=runtime.session_store.root,
        assume_present=assume_present, assume_absent=assume_absent,
    )
    scan = continuity_mod.scan_records(runtime.session_store.root)
    references = served_plan.references_from_scan(scan)
    plan = served_plan.build_plan(
        published.routes, served_plan.routes_from_document(candidate),
        references=references, state_root=str(runtime.session_store.root),
        gateway=served_plan.gateway_label(candidate), authority_before=authority_before,
        authority_after=authority_after, before_unknown=published.unknown,
    )
    if show:
        consent.prompt_stream().write("".join(termtext.visible_text(line) + "\n" for line in plan.lines()))
        consent.prompt_stream().flush()
    refusal = plan.refusal()
    if refusal is not None:
        raise OperatorCommandError(f"{verb}: {refusal}")
    return ServedPreflight(verb, plan, sample,
                           references.fingerprint() if plan.destructive else None)


def show_served(served: ServedPreflight | None) -> None:
    """The served-change preview of a planned change, on the prompt stream."""

    if served is None:
        return
    consent.prompt_stream().write("".join(termtext.visible_text(line) + "\n" for line in served.plan.lines()))
    consent.prompt_stream().flush()


def references_now(runtime: runtime_mod.Runtime) -> str:
    """The reference fingerprint of a fresh record scan (commit phase)."""

    return served_plan.references_from_scan(
        continuity_mod.scan_records(runtime.session_store.root)).fingerprint()


SERVED_BUSY = state.BARRIER_BUSY_TEXT + " — nothing changed; retry when it completes"


@contextlib.contextmanager
def operator_write(runtime: runtime_mod.Runtime, *, preflight: ServedPreflight | None) -> Iterator[state.BarrierToken]:
    """One served-change commit phase around an operator mutation.

    Lock order: migration guard -> served-change
    barrier (bounded wait) -> token rotation (non-blocking; refuses with the
    neutral text) -> operator store lock -> (Settings, api-key leaf inside the
    command). Inside the barrier the root authority and the
    ``preflight`` sample are revalidated: anything that moved after the
    confirmation refuses with nothing applied. The render inside the phase
    asserts the yielded token. ``preflight`` is required by keyword: local
    admission and evidence-only phases pass None and revalidate their own metadata.
    """

    env = runtime.gateway_environ()
    with contextlib.ExitStack() as stack:
        try:
            barrier = stack.enter_context(sessions.served_change_phase(
                runtime.session_store.root, runtime.home, timeout=runtime.served_barrier_timeout))
        except state.BarrierBusyError as exc:
            raise OperatorCommandError(SERVED_BUSY) from exc
        refusal = runtime.root_authority_refusal()
        if refusal is not None:
            raise OperatorCommandError(refusal)
        directory = proxy_mod.config_dir(runtime.home)
        state.ensure_private_dir(directory)
        rotation = state.FileLock(directory / "token-rotation")
        if not rotation.acquire(blocking=False):
            raise OperatorCommandError(SERVED_BUSY)
        stack.callback(rotation.release)
        stack.enter_context(operator_mod.store_lock(env))
        if preflight is not None and (
                _sample(runtime) != preflight.sample
                or (preflight.references is not None and references_now(runtime) != preflight.references)):
            raise OperatorCommandError(f"{preflight.verb}: {served_plan.CHANGED_REFUSAL}")
        previous = runtime.held_barrier
        runtime.held_barrier = barrier
        try:
            yield barrier
        finally:
            runtime.held_barrier = previous


def restore_file(env: Mapping[str, str], file_id: str, previous: bytes | None) -> None:
    """Undo this command's providers.d change (the store lock is held)."""

    if previous is None:
        operator_mod.remove_provider_file(env, file_id)
    else:
        operator_mod.write_provider_bytes(env, file_id, previous)


def published_unconfirmed(exc: proxy_mod.ConfigPublishedError, verb: str) -> Any:
    """Report a config replacement whose directory durability is unconfirmed
    (never "nothing changed"): the change is kept, since the gateway may
    already serve it; returns the render's ``(target, result, report)``."""

    consent.prompt_stream().write(
        f"claude-multi: {verb}: the new gateway config was published but its durability is unconfirmed "
        f"({termtext.visible_message(str(exc))}); the change is kept — rerun claude-multi providers apply "
        "to confirm it\n")
    return exc.rendered


def render_or_undo(
    runtime: runtime_mod.Runtime, undo: Callable[[], None], verb: str, output_stream: TextIO,
) -> Any:
    """Explicit render under the held locks; a refusal undoes this command's
    change and reports it (the previous config keeps serving)."""

    try:
        _target, result, continuity_report = runtime.render_gateway(policy="explicit")
    except proxy_mod.ConfigPublishedError as exc:
        # The new config is already visible: keep the inputs that match it.
        _target, result, continuity_report = published_unconfirmed(exc, verb)
    except (errors.ClaudeMultiError, OSError) as exc:
        undo()
        raise OperatorCommandError(f"{verb}: the gateway render refused — nothing changed:\n{exc}") from exc
    except BaseException:
        # An interrupt (or any other failure) during the render undoes too.
        undo()
        raise
    for note in continuity_report.operator:
        consent.prompt_stream().write(f"operator: {termtext.visible_text(note)}\n")
    return result


def verify(runtime: runtime_mod.Runtime, result: Any, output_stream: TextIO) -> str:
    """Wait for the sentinel after the locks are released; never claims
    served when a restart is needed."""

    outcome = runtime.verify_reload(result.sentinel)
    report(output_stream, outcome.message)
    return outcome.status


def run_editor(runtime: runtime_mod.Runtime, raw: bytes, label: str) -> tuple[bytes | None, Path | None, str | None]:
    """``$VISUAL``/``$EDITOR`` on a private 0600 temp copy: (bytes, kept path, problem)."""

    editor = runtime.environ.get("VISUAL") or runtime.environ.get("EDITOR")
    if not editor:
        raise OperatorCommandError(f"{label}: set VISUAL or EDITOR to edit a declaration")
    descriptor, temporary = tempfile.mkstemp(prefix="claude-multi-operator-", suffix=".json")
    path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    env = dict(runtime.environ)
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    completed = subprocess.run([*shlex.split(editor), str(path)], env=env, check=False)
    if completed.returncode != 0:
        return None, path, f"the editor exited with status {completed.returncode}"
    try:
        edited = path.read_bytes()
    except OSError as exc:
        return None, path, str(exc)
    return edited, path, None


def definition_consequences(
    before: operator_mod.OperatorLayer, after: operator_mod.OperatorLayer, keys: Iterable[str],
    scan: continuity_mod.RecordScan | None, admitted: Iterable[str],
) -> list[str]:
    """What an edit does to admissions, served aliases and live sessions."""

    lines: list[str] = []
    granted = set(admitted)
    for key in sorted(set(keys)):
        old, new = before.lines.get(key), after.lines.get(key)
        if old is None and new is not None:
            lines.append(f"{key}: declared (New · not admitted)")
        elif old is not None and new is None:
            lines.append(f"{key}: removed or invalid — captured aliases stay served until pruned")
        elif old is not None and new is not None and old.definition_digest != new.definition_digest:
            fields = operator_mod.changed_fields({"diagnostic": {"fields": before.metadata[key]["field_hashes"]}},
                                                 after, key)
            text = f"{key}: definition changed ({', '.join(fields)})"
            if key in granted:
                text += " — its optional admission badge lapses (not a use restriction); re-admit: claude-multi models admit " + key
            if old.core_entry["wire_model"] != new.core_entry["wire_model"] and scan is not None:
                users = live_users(scan, operator_mod.line_aliases(old.core_entry))
                if users:
                    text += f"; live sessions retarget at the next reload: {', '.join(sorted(u[:8] for u in users))}"
            lines.append(text)
    return lines


# ------------------------------------------------------------ verbs
PROVIDERS_LIST_SCHEMA = 1
_SOURCE_WORDS = {"catalog": "shipped", "operator": "operator", "operator-migrated": "operator", "custom": "legacy"}
STATE_UNAVAILABLE = "unavailable"  # the provider facts could not be read
STATE_NOT_LOADED = "not-loaded"  # a providers.d file that does not load
_ROUTE_STATE_TEXT = {"route-unapproved": "route not approved", "route-changed": "route changed"}


def _provider_states(runtime: runtime_mod.Runtime) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """``(facts by id, connections by id, why they are unavailable)``: names,
    counts and states only, never a key value; no provider request."""

    import claude_multi.cli.gateway_facts as gateway_facts
    from claude_multi.setup import status

    try:
        loaded = gateway_facts._provider_facts(runtime)
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError) as exc:
        return {}, {}, termtext.visible_message(exc)
    facts = {str(fact["id"]): fact for fact in loaded.facts}
    try:
        found = status.connections(runtime, facts=loaded.facts)
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError) as exc:
        return facts, {}, termtext.visible_message(exc)
    return facts, {item.provider_id: item for item in found}, None


def providers_list_document(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """The ``providers list --json`` document: every provider (shipped,
    operator and legacy) sorted by id, plus the providers.d files that do
    not load. ``state`` is one of ``setup.status.CONNECTION_STATES``,
    ``unavailable`` or ``not-loaded``; ``credential`` is a variable or
    account-pool name, never a value; ``served`` is ``N/M``, ``unknown``
    (gateway not observed) or ``—`` (nothing expected)."""

    ctx = operator_context(runtime)
    layer, read = ctx.layer, ctx.read
    admitted = admitted_keys(runtime)
    by_file: dict[str, list[str]] = {}
    for key, file_id in operator_mod.line_file_ids(read).items():
        by_file.setdefault(file_id, []).append(key)
    file_ids = set(read.files) | {problem.file.removeprefix(f"{operator_mod.PROVIDERS_DIRNAME}/")
                                  .removesuffix(".json") for problem in layer.problems
                                  if problem.file.endswith(".json")}
    facts, connections, unavailable = _provider_states(runtime)
    entries: list[dict[str, Any]] = []
    for provider_id in sorted(set(facts) | file_ids):
        fact, conn = facts.get(provider_id), connections.get(provider_id)
        declared = layer.providers.get(provider_id)
        if conn is not None:
            state_word = conn.state
        elif fact is None and provider_id in file_ids and provider_id not in layer.providers:
            state_word = STATE_NOT_LOADED
        else:
            state_word = STATE_UNAVAILABLE
        entries.append({
            "id": provider_id,
            "display": str(fact["display"]) if fact is not None else provider_id,
            "source": _SOURCE_WORDS.get(str(fact.get("source")), "shipped") if fact is not None else "operator",
            "file": operator_mod.file_label(provider_id) if provider_id in file_ids else None,
            "kind": conn.kind if conn is not None else None,
            "transport": conn.transport if conn is not None else None,
            "enabled": bool(fact.get("enabled", True)) if fact is not None else None,
            "state": state_word,
            "credential": conn.credential if conn is not None else None,
            "accounts": len(conn.accounts) if conn is not None else None,
            "models": int(fact.get("models", 0)) if fact is not None else None,
            "served": conn.served if conn is not None else "unknown",
            "route": layer.route_status.get(provider_id) if declared is not None else None,
            "lines": [{"key": key, "status": line_status(key, ctx, admitted)}
                      for key in sorted(by_file.get(provider_id, []))],
            "problems": len(layer.problems_by_file.get(operator_mod.file_label(provider_id), ())),
        })
    legacy = ctx.legacy
    legacy_counts = ({"providers": len(legacy.get("providers", {})), "models": len(legacy.get("models", {}))}
                     if (legacy.get("providers") or legacy.get("models")) and not layer.marker else None)
    return {"schema_version": PROVIDERS_LIST_SCHEMA, "list": "providers", "providers": entries,
            "unavailable": unavailable, "legacy_registry": legacy_counts}


def _provider_head(entry: Mapping[str, Any], layer: operator_mod.OperatorLayer) -> str:
    """One listing row: source and declaration, then on/off, state, models and served."""

    from claude_multi.setup import texts as setup_texts

    provider_id = entry["id"]
    head = f"{provider_id}\t{entry['source']} provider"
    declared = layer.providers.get(provider_id)
    if declared is not None:
        head += f" · {declared.kind} · {declared.origin} · route {entry['route'] or '?'}"
    if entry["state"] == STATE_NOT_LOADED:
        head += " · not loaded"
    elif entry["state"] == STATE_UNAVAILABLE:
        head += " · state unavailable"
    else:
        state_text = _ROUTE_STATE_TEXT.get(entry["state"]) or setup_texts.STATE_TEXT.get(entry["state"], entry["state"])
        head += f" · {'on' if entry['enabled'] else 'off'} · {state_text}"
        count = entry["models"]
        head += f" · {count} model{'s' if count != 1 else ''} · served {entry['served']}"
    if entry["problems"]:
        head += f" · {entry['problems']} problem(s): claude-multi providers validate"
    return head


def _providers_list(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    """``providers list [--json]``: every provider with its state; read only."""

    document = providers_list_document(runtime)
    if getattr(args, "json_output", False):
        output_stream.write(strict_json.pretty_file_bytes(document).decode("utf-8"))
        return 0
    if document["unavailable"] is not None:
        report(output_stream, f"provider state unavailable: {document['unavailable']}")
    layer = operator_context(runtime).layer
    for entry in document["providers"]:
        report(output_stream, _provider_head(entry, layer))
        for line in entry["lines"]:
            report(output_stream, f"  {line['key']}\t{line['status']}")
    if not document["providers"]:
        report(output_stream, "(no providers)")
    legacy = document["legacy_registry"]
    if legacy is not None:
        report(output_stream, f"custom.json\tlegacy registry · {legacy['providers']} providers, "
                              f"{legacy['models']} models · preview: claude-multi providers migrate-custom")
    return 0


def _resolved_provider(ctx: OperatorContext, provider_id: str) -> dict[str, Any]:
    layer = ctx.layer
    provider = layer.providers.get(provider_id)
    catalog_provider = ctx.docs["providers"]["providers"].get(provider_id)
    lines = {
        key: {
            "definition_digest": line.definition_digest,
            "entry": line.core_entry,
            "family": line.family,
            "origin": line.origin,
            "selector_mode": line.selector_mode,
            "source": line.source,
        }
        for key, line in sorted(layer.lines.items()) if line.provider_id == provider_id
    }
    source = ctx.docs["models-v2"] if "models-v2" in ctx.docs else ctx.docs["models"]
    catalog_lines = {key: dict(entry) for key, entry in sorted(source["models"].items())
                     if isinstance(entry, Mapping) and entry.get("provider") == provider_id}
    return {
        "provider": provider.entry if provider is not None else catalog_provider,
        "provider_origin": "operator" if provider is not None else "catalog",
        "catalog_lines": catalog_lines,
        "route": layer.route_status.get(provider_id, "catalog" if catalog_provider is not None else "invalid"),
        "route_digest": provider.route_digest if provider is not None else None,
        "lines": lines,
        "pending": sorted(key for key, fid in operator_mod.line_file_ids(ctx.read).items()
                          if fid == provider_id and key in layer.pending),
        "problems": [problem.text() for problem in
                     layer.problems_by_file.get(operator_mod.file_label(provider_id), ())],
    }


def _providers_show(runtime: runtime_mod.Runtime, provider_id: str, resolved: bool, output_stream: TextIO) -> int:
    """``providers show ID [--resolved]``: a shipped provider (the catalog)
    or one a providers.d declaration names, with its models; read only."""

    ctx = operator_context(runtime)
    label = operator_mod.file_label(provider_id)
    shipped = ctx.docs["providers"]["providers"].get(provider_id)
    declared = provider_id in ctx.read.files or label in ctx.layer.problems_by_file
    if not declared and shipped is None:
        return fail(f"providers show {provider_id}: no shipped provider and no {label} "
                    "(the providers: claude-multi providers list)")
    document = _resolved_provider(ctx, provider_id)
    if resolved:
        output_stream.write(strict_json.pretty_file_bytes(document).decode("utf-8"))
        return 0
    admitted = admitted_keys(runtime)
    provider = ctx.layer.providers.get(provider_id)
    if provider is not None:
        auth = "none (keyless)" if provider.auth_kind == "none" else f"{provider.auth_kind} env:{provider.secret_name}"
        report(output_stream, f"provider {provider_id}: {provider.kind} · {provider.origin} · auth {auth} · "
                              f"route {document['route']}")
    elif shipped is not None and declared:
        report(output_stream, f"provider {provider_id}: catalog provider (this file declares lines only)")
    elif shipped is not None:
        transport = shipped.get("transport") if isinstance(shipped.get("transport"), Mapping) else {}
        report(output_stream, f"provider {provider_id}: shipped provider · {shipped.get('display', provider_id)} · "
                              f"transport {transport.get('kind', '?')}")
    else:
        report(output_stream, f"provider {provider_id}: not loaded")
    for key, entry in document["catalog_lines"].items():
        selectors = " · ".join(selector for _level, selector, _contract in
                               operator_mod.catalog.line_selectors(dict(entry)))
        status = str(entry.get("status", "active"))
        if status == "new":
            status = "admitted" if key in admitted else "New · Off"
        context = entry.get("context") if isinstance(entry.get("context"), Mapping) else {}
        report(output_stream, f"  {key}\t{entry.get('wire_model', '?')}\tclass {context.get('ordinary_profile', '?')}\t"
                              f"{status}\tselectors {selectors}")
    for key, spec in document["lines"].items():
        entry = spec["entry"]
        selectors = " · ".join(selector for _level, selector, _contract in
                               operator_mod.catalog.line_selectors(dict(entry)))
        report(output_stream, f"  {key}\t{entry['wire_model']}\tclass {entry['context']['ordinary_profile']}\t"
                              f"{line_status(key, ctx, admitted)}\tselectors {selectors}")
    for key in document["pending"]:
        report(output_stream, f"  {key}\tpending migration")
    for problem in document["problems"]:
        report(output_stream, f"  problem: {problem}")
    return 0


def _providers_validate(runtime: runtime_mod.Runtime, candidate: str | None, output_stream: TextIO) -> int:
    if candidate is None:
        ctx = operator_context(runtime)
        layer = ctx.layer
        for problem in layer.problems:
            report(output_stream, problem.text())
        if ctx.snapshot.ledger_error is not None:
            report(output_stream, ctx.snapshot.ledger_error)
        blocking = operator_mod.blocking_problems(layer)
        if blocking or ctx.snapshot.ledger_error is not None:
            return 1
        report(output_stream, f"providers.d: {len(ctx.read.files)} file(s), {len(layer.providers)} provider(s), "
                              f"{len(layer.lines)} line(s) valid")
        return 0
    path = Path(candidate)
    if not operator_mod.FILE_NAME.fullmatch(path.name):
        return fail(f"providers validate: {path.name!r} — provider file names match ^[a-z][a-z0-9-]*\\.json$")
    try:
        raw = state.read_owned_readable(path, max_bytes=operator_mod.READ_LIMIT_BYTES)
    except (state.StateError, OSError) as exc:
        return fail(f"providers validate: {path.name} is unreadable or unsafe "
                    f"({getattr(exc, 'strerror', None) or type(exc).__name__})")
    file_id = path.name[:-5]
    # The metadata preflight precedes operator_context() (no store).
    gated = operator_mod.keyed_preflight(runtime.catalog.docs, file_id, raw)
    if gated is not None:
        report(output_stream, gated.text())
        return 1
    ctx = operator_context(runtime)
    layer = proposed(ctx, {file_id: raw})
    found = layer.problems_by_file.get(operator_mod.file_label(file_id), ())
    for problem in found:
        report(output_stream, problem.text())
    if any(problem.code not in operator_mod.NONBLOCKING_CODES for problem in found):
        return 1
    count = sum(1 for line in layer.lines.values() if line.source == operator_mod.file_label(file_id))
    report(output_stream, f"{operator_mod.file_label(file_id)}: valid ({count} line(s))")
    return 0


def _providers_template(runtime: runtime_mod.Runtime, kind: str, output_stream: TextIO) -> int:
    refusal = keyed_gate_refusal(runtime, kind)
    if refusal is not None:
        return fail(f"providers template --kind {kind}: {refusal}")
    output_stream.write(strict_json.pretty_file_bytes(TEMPLATES[kind]).decode("utf-8"))
    return 0


def _approval_text(provider: operator_mod.ResolvedProvider, present: bool) -> str:
    base = f" (header auth binds the full base URL {provider.base_url})" if provider.auth_kind == "header" else ""
    header = f" header {provider.header}" if provider.header else ""
    return (
        f"approve the credential route of provider {provider.provider_id}:\n"
        f"  origin: {provider.origin}{base}\n"
        f"  auth: {provider.auth_kind}{header} from {provider.secret_name} "
        f"(value not shown; {'present' if present else 'not set yet'})\n"
        f"  listing: {provider.listing_origin or 'none'}\n"
        "The key is sent only to this origin. Approve this route? [y/N] "
    )


def _approved_line(provider: operator_mod.ResolvedProvider) -> str:
    return f"approved route {provider.provider_id}: {provider.auth_kind} env:{provider.secret_name} → {provider.origin}"


def _route_edit(provider: operator_mod.ResolvedProvider) -> Callable[[dict[str, Any]], None]:
    """The planned (in-memory) route approval (the commit writes it with ``setup.providers.grant_route``)."""

    def grant(document: dict[str, Any]) -> None:
        document["routes"][provider.provider_id] = operator_mod.route_record(provider, at=operator_mod.utc_stamp())

    return grant


@contextlib.contextmanager
def guarded_writes() -> Iterator[Any]:
    """The exact undo of a command's writes, each registered before its write
    (``setup.providers.write_declaration``, ``grant_route``, ``set_secret``):
    a failure or an interrupt part-way puts back every write that landed,
    and the original error goes on."""

    from claude_multi.setup import model as setup_model, providers as setup_providers

    undo = setup_model.UndoStack()
    try:
        yield undo
    except BaseException as exc:
        setup_providers.undo_then_raise(undo, exc)
        raise


def _providers_add(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                   output_stream: TextIO) -> int:
    if args.preset is not None:
        return _providers_add_preset(runtime, args, input_stream=input_stream, output_stream=output_stream)
    if args.as_id is not None or args.secret_file is not None:
        raise OperatorUsageError("--as and --secret-file apply to --preset only")
    if getattr(args, "reuse_key", False) or getattr(args, "replace_key", False):
        raise OperatorUsageError("--reuse-key and --replace-key apply to --preset only")
    args.kind = args.kind or "anthropic-compatible"
    missing = [flag for flag, value in (("ID", args.provider_id), ("--base-url", args.base_url),
                                        ("--auth", args.auth), ("--family", args.family)) if value is None]
    if missing:
        raise OperatorUsageError(f"{', '.join(missing)} required (or declare from a reviewed --preset)")
    provider_id = args.provider_id
    refusal = keyed_gate_refusal(runtime, args.kind)
    if refusal is not None:
        return fail(f"providers add {provider_id}: {refusal}")
    if args.auth == "header" and args.header != operator_mod.MIGRATION_HEADER:
        raise OperatorUsageError("--auth header needs --header x-api-key (the only header the gateway sends)")
    if args.auth != "header" and args.header is not None:
        raise OperatorUsageError("--header applies to --auth header only")
    if args.auth == "none" and args.secret_ref is not None:
        raise OperatorUsageError("--auth none takes no --secret-ref")
    if args.auth != "none" and args.secret_ref is None:
        raise OperatorUsageError(f"--auth {args.auth} needs --secret-ref env:NAME")
    if getattr(args, "listing_auth", None) is not None and args.listing_url is None:
        raise OperatorUsageError("--listing-auth requires --listing-url and --listing-shape")
    if (args.listing_url is None) != (args.listing_shape is None):
        raise OperatorUsageError("--listing-url and --listing-shape go together")
    keyed = args.auth != "none"
    approve = keyed and not args.declare_only
    if approve:
        # Before any read of the secret store (the literal scan included).
        consent.require_human(f"providers add {provider_id}", runtime.gateway_environ())
    ctx = operator_context(runtime)
    require_ledger(ctx, f"providers add {provider_id}")
    if provider_id in ctx.docs["providers"]["providers"]:
        return fail(f"providers add {provider_id}: {provider_id} is a catalog provider — declare lines with "
                    f"claude-multi models add {provider_id} <wire> ...")
    target = operator_mod.providers_dir(ctx.env) / f"{provider_id}.json"
    if provider_id in ctx.read.files or os.path.lexists(target):
        return fail(f"providers add {provider_id}: {operator_mod.file_label(provider_id)} exists — "
                    f"edit it: claude-multi providers edit {provider_id}")
    auth: dict[str, Any] = {"kind": args.auth}
    if args.secret_ref is not None:
        auth["secret_ref"] = args.secret_ref
    if args.header is not None:
        auth["header"] = args.header
    block: dict[str, Any] = {"display": args.display or provider_id, "kind": args.kind, "base_url": args.base_url,
                             "auth": auth, "independence_family": args.family}
    if args.contracts:
        block["payload_contracts"] = [item.strip() for item in args.contracts.split(",") if item.strip()]
    if args.listing_url is not None:
        block["listing"] = {"url": args.listing_url, "shape": args.listing_shape,
                            "auth": getattr(args, "listing_auth", None) or ("provider" if args.auth != "none" else "none")}
    document = {"version": operator_mod.DATA_VERSION, "provider": block, "lines": {}}
    raw = operator_mod.document_bytes(document)
    layer = proposed(ctx, {provider_id: raw})
    own = operator_mod.file_problems(layer, provider_id)
    if own:
        return fail(f"providers add {provider_id}: not declared:\n  " + "\n  ".join(p.text() for p in own))
    refuse_blocking(layer, f"providers add {provider_id}")
    resolved = layer.providers[provider_id]
    preflight = served_preflight(
        runtime, f"providers add {provider_id}", changes={provider_id: raw},
        ledger_edit=_route_edit(resolved) if approve else None,
    )
    if approve:
        present = secret_store.default_store(ctx.env).is_set(resolved.secret_name or "")
        if not consent.confirm(_approval_text(resolved, present), input_stream=input_stream):
            return fail(f"providers add {provider_id}: route not approved — nothing written "
                        "(declare only with --declare-only)")
    with operator_write(runtime, preflight=preflight):
        fresh = operator_context(runtime)
        if provider_id in fresh.read.files or os.path.lexists(target):
            return fail(f"providers add {provider_id}: configuration changed while awaiting confirmation — "
                        "nothing written; retry")
        from claude_multi.setup import providers as setup_providers

        with guarded_writes() as undo:
            setup_providers.write_declaration(undo, fresh, provider_id, raw)
            if approve:
                layer = proposed(fresh, {provider_id: raw})
                setup_providers.grant_route(undo, fresh, layer.providers[provider_id])
        result = render_or_undo(runtime, undo.run, f"providers add {provider_id}", output_stream)
    if approve:
        report(output_stream, f"declared provider {provider_id}")
        report(output_stream, _approved_line(resolved))
    elif keyed:
        report(output_stream, f"declared provider {provider_id} — route unapproved; not rendered")
        report(output_stream, f"next: claude-multi providers approve {provider_id}")
    else:
        report(output_stream, f"declared provider {provider_id} — keyless LAN route")
    if keyed and not secret_store.default_store(ctx.env).is_set(resolved.secret_name or ""):
        report(output_stream, f"note: {resolved.secret_name} is not set — claude-multi providers set-key {provider_id}")
    verify(runtime, result, output_stream)
    return 0


def _providers_add_preset(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                          output_stream: TextIO) -> int:
    """``providers add --preset PRESET [--as ID] [--base-url URL] [--secret-file FILE] [--reuse-key |
    --replace-key] [--declare-only]``.

    A preset is a reviewed template, never a grant: unattended (or with
    ``--declare-only``) it only declares. A keyed preset's route approval and
    a ``--secret-file`` import pass the shared human guard first. When
    another provider already uses the preset's API key, the new provider
    gets its own key name (``setup.providers.preset_key_name``) unless
    ``--reuse-key`` shares the saved key or ``--replace-key`` replaces it
    with ``--secret-file`` after a y/N that names every provider using it.
    """

    manual = [flag for flag, value in (("--kind", args.kind), ("--auth", args.auth), ("--header", args.header),
                                       ("--secret-ref", args.secret_ref), ("--family", args.family),
                                       ("--display", args.display), ("--contracts", args.contracts),
                                       ("--listing-url", args.listing_url),
                                       ("--listing-shape", args.listing_shape), ("--listing-auth", getattr(args, "listing_auth", None))) if value is not None]
    if manual:
        raise OperatorUsageError(f"--preset is exclusive with {', '.join(manual)}")
    if args.provider_id is not None:
        raise OperatorUsageError("--preset takes its id from --as ID (default: the preset name)")
    if args.secret_file is not None and args.declare_only:
        raise OperatorUsageError("--secret-file imports a key, which --declare-only never does")
    reuse_key, replace_key = bool(getattr(args, "reuse_key", False)), bool(getattr(args, "replace_key", False))
    if reuse_key and args.secret_file is not None:
        raise OperatorUsageError("--reuse-key shares the saved key; --replace-key --secret-file FILE replaces it")
    if replace_key and (args.secret_file is None or args.declare_only):
        raise OperatorUsageError("--replace-key replaces the shared key with --secret-file FILE")
    from claude_multi.setup import model as setup_model, providers as setup_providers, texts as setup_texts

    key_use = (setup_providers.KEY_REUSE if reuse_key else setup_providers.KEY_REPLACE if replace_key
               else setup_providers.KEY_OWN)
    name = args.preset
    try:
        raw_preset = operator_mod.load_preset(name, runtime.asset_root)
        document = operator_mod.preset_document(raw_preset, base_url=args.base_url)
    except operator_mod.OperatorError as exc:
        return fail(f"providers add --preset {name}: {exc}")
    provider_id = args.as_id or name
    verb = f"providers add --preset {name}"
    block = document.get("provider")
    auth = block.get("auth") if isinstance(block, Mapping) else None
    keyed = isinstance(auth, Mapping) and auth.get("kind") != "none"
    approve = keyed and not args.declare_only
    if approve or args.secret_file is not None:
        # Before any read of the secret store (the literal scan included).
        consent.require_human(verb, runtime.gateway_environ())
    secret_value: str | None = None
    if args.secret_file is not None:
        if not keyed:
            return fail(f"{verb}: the preset is keyless — there is no key to import")
        try:
            secret_value = operator_mod.read_secret_file(args.secret_file)
        except operator_mod.OperatorError as exc:
            return fail(f"{verb}: {exc}")
    ctx = operator_context(runtime)
    require_ledger(ctx, verb)
    if block is not None and provider_id in ctx.docs["providers"]["providers"]:
        return fail(f"{verb}: {provider_id} is a catalog provider — a preset never overrides it")
    target = operator_mod.providers_dir(ctx.env) / f"{provider_id}.json"
    if provider_id in ctx.read.files or os.path.lexists(target):
        return fail(f"{verb}: {operator_mod.file_label(provider_id)} exists — "
                    f"edit it: claude-multi providers edit {provider_id}")
    key_line = ""
    sharers: tuple[str, ...] = ()
    if keyed and isinstance(auth, Mapping):
        # Another provider's saved key is never replaced unasked: one more
        # provider from this preset gets its own key name by default.
        preset_secret = str(auth.get("secret_ref") or "").removeprefix("env:")
        try:
            secret_name, preset_sharers = setup_providers.preset_key_name(ctx, provider_id, preset_secret, key_use)
        except setup_model.SetupError as exc:
            return fail(f"{verb}: {exc}")
        sharers = setup_providers.key_sharers(ctx, secret_name, exclude=provider_id)
        if key_use == setup_providers.KEY_OWN and sharers:
            return fail(f"{verb}: " + setup_texts.KEY_NAME_TAKEN.format(name=secret_name, providers=", ".join(sharers)))
        if secret_name != preset_secret:
            document = setup_providers.with_secret_name(document, secret_name)
            key_line = setup_texts.PRESET_KEY_OWN.format(id=provider_id, name=secret_name, preset_name=preset_secret,
                                                         providers=", ".join(preset_sharers))
        elif key_use == setup_providers.KEY_REUSE:
            if not secret_store.default_store(ctx.env).is_set(secret_name):
                return fail(f"{verb}: " + setup_texts.PRESET_KEY_NOTHING_TO_REUSE.format(name=secret_name,
                                                                                        id=provider_id))
            key_line = setup_texts.PRESET_KEY_REUSE.format(id=provider_id, name=secret_name,
                                                           providers=", ".join(sharers))
    raw = operator_mod.document_bytes(document)
    layer = proposed(ctx, {provider_id: raw})
    own = operator_mod.file_problems(layer, provider_id)
    if own:
        return fail(f"{verb}: not declared:\n  " + "\n  ".join(p.text() for p in own))
    refuse_blocking(layer, verb)
    resolved = layer.providers.get(provider_id)
    store = secret_store.default_store(ctx.env)
    preflight = served_preflight(
        runtime, verb, changes={provider_id: raw},
        ledger_edit=_route_edit(resolved) if approve and resolved is not None else None,
        assume_present=(resolved.secret_name,) if secret_value is not None and resolved is not None
        and resolved.secret_name else (),
    )
    if approve and resolved is not None:
        present = secret_value is not None or store.is_set(resolved.secret_name or "")
        if not consent.confirm(_approval_text(resolved, present), input_stream=input_stream):
            return fail(f"{verb}: route not approved — nothing written (declare only with --declare-only)")
    if key_use == setup_providers.KEY_REPLACE and resolved is not None:
        if not consent.confirm(setup_texts.PRESET_KEY_REPLACE_QUESTION.format(
                name=resolved.secret_name, providers=", ".join(sharers)), input_stream=input_stream):
            return fail(f"{verb}: {resolved.secret_name} kept — nothing written")
    with operator_write(runtime, preflight=preflight):
        fresh = operator_context(runtime)
        if provider_id in fresh.read.files or os.path.lexists(target):
            return fail(f"{verb}: configuration changed while awaiting confirmation — nothing written; retry")
        if resolved is not None and resolved.secret_name and setup_providers.key_sharers(
                fresh, resolved.secret_name, exclude=provider_id) != sharers:
            return fail(f"{verb}: configuration changed while awaiting confirmation — nothing written; retry")

        with guarded_writes() as undo:
            setup_providers.write_declaration(undo, fresh, provider_id, raw)
            if approve and resolved is not None:
                now = proposed(fresh, {provider_id: raw}).providers.get(provider_id)
                if now is None or now.route_digest != resolved.route_digest:
                    undo.run()
                    return fail(f"{verb}: configuration changed while awaiting confirmation — nothing written; "
                                "retry")
                setup_providers.grant_route(undo, fresh, now)
            if secret_value is not None and resolved is not None and resolved.secret_name:
                setup_providers.set_secret(undo, store, resolved.secret_name, secret_value)
        result = render_or_undo(runtime, undo.run, verb, output_stream)
    lines_note = f" with {len(layer.lines)} New · Off line(s)" if any(
        line.provider_id == provider_id for line in layer.lines.values()) else ""
    if approve and resolved is not None:
        report(output_stream, f"declared provider {provider_id} from preset {name}{lines_note}")
        report(output_stream, _approved_line(resolved))
    elif keyed:
        report(output_stream, f"declared provider {provider_id} from preset {name}{lines_note} — "
                              "route unapproved; not rendered")
        report(output_stream, f"next: claude-multi providers approve {provider_id}")
    else:
        report(output_stream, f"declared provider {provider_id} from preset {name}{lines_note} — keyless LAN route")
    if key_line:
        report(output_stream, key_line)
    if secret_value is not None and resolved is not None:
        report(output_stream, f"stored {resolved.secret_name} ({store.description()}; value not shown)"
                              + (f" — the key of {', '.join(sharers)} too" if sharers else ""))
    elif keyed and resolved is not None and not store.is_set(resolved.secret_name or ""):
        report(output_stream, f"note: {resolved.secret_name} is not set — claude-multi providers set-key {provider_id}")
    for key in sorted(k for k, line in layer.lines.items() if line.provider_id == provider_id):
        report(output_stream, f"next: claude-multi models admit {key}")
    verify(runtime, result, output_stream)
    return 0


def _transport_selectors(ctx: OperatorContext, provider_id: str) -> frozenset[str]:
    """Every selector base the provider serves today (catalog and operator
    lines, passthrough routes): what a transport switch moves."""

    provider = ctx.docs["providers"]["providers"][provider_id]
    bases = {route["name"] for route in provider.get("passthrough_routes", [])}
    for key, entry in ctx.docs["models-v2"]["models"].items():
        if entry.get("provider") == provider_id:
            bases.update(operator_mod.line_aliases(entry))
    for line in ctx.layer.lines.values():
        if line.provider_id == provider_id:
            bases.update(operator_mod.line_aliases(line.core_entry))
    return frozenset(bases)


def _providers_transport(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                         output_stream: TextIO) -> int:
    """``providers transport PROVIDER [oauth-pool|api-key] [--secret-file FILE]``:
    a reviewed alternative, the same selectors, a guarded route approval;
    never an automatic fallback between the two. Switching back to the
    account offers to remove the saved key."""

    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers
    from claude_multi.setup import texts as setup_texts

    provider_id, choice = args.provider_id, args.transport_choice
    verb = f"providers transport {provider_id}" + (f" {choice}" if choice else "")
    ctx = operator_context(runtime, metadata_only=choice is not None)
    catalog_provider = ctx.docs["providers"]["providers"].get(provider_id)
    if catalog_provider is None or catalog_provider["transport"]["kind"] != operator_mod.TRANSPORT_POOL:
        return fail(f"{verb}: {operator_mod.transport_problem(ctx.docs, provider_id, operator_mod.TRANSPORT_POOL)}")
    if choice is None:
        current = setup_providers.current_transport(runtime, provider_id, ctx)
        selection = operator_mod.transport_selections(ctx.docs, ctx.ledger).get(provider_id)
        report(output_stream, f"{provider_id}\ttransport\t{current}")
        if selection is not None and selection.problem is not None:
            report(output_stream, f"{provider_id}\tproblem\t{selection.problem}")
        for option in operator_mod.TRANSPORT_CHOICES:
            problem = operator_mod.transport_problem(ctx.docs, provider_id, option)
            alternative = operator_mod.transport_alternative(provider_id, option)
            detail = (f"{alternative.auth_kind} env:{alternative.secret_name} → {alternative.origin}"
                      if alternative is not None else f"the {catalog_provider['transport']['pool']} OAuth pool")
            report(output_stream, f"{provider_id}\t{option}\t{'closed: ' + problem if problem else detail}")
        return 0
    if args.secret_file is not None and choice == operator_mod.TRANSPORT_POOL:
        raise OperatorUsageError("--secret-file applies to the api-key transport only")
    problem = operator_mod.transport_problem(ctx.docs, provider_id, choice)
    if problem is not None:
        return fail(f"{verb}: {problem}")
    consent.require_human(verb, runtime.gateway_environ())
    secret_value: str | None = None
    if args.secret_file is not None:
        try:
            secret_value = operator_mod.read_secret_file(args.secret_file)
        except operator_mod.OperatorError as exc:
            return fail(f"{verb}: {exc}")
    try:
        plan = setup_providers.plan_transport(runtime, provider_id, choice)
    except setup_model.Refused as exc:
        if str(exc) == setup_texts.TRANSPORT_ALREADY.format(id=provider_id, choice=choice):
            report(output_stream, str(exc))
            return 0
        raise
    show_served(plan.served)
    prompt = consent.prompt_stream()
    prompt.write("".join(termtext.visible_text(line) + "\n" for line in plan.lines))
    if not consent.confirm("Approve this transport? [y/N] ", input_stream=input_stream):
        raise setup_model.Declined(f"{verb}: not approved — nothing changed")
    value: setup_model.Secret | None = None
    display = setup_providers.transport_names(ctx.docs, provider_id)["display"]
    if secret_value is not None:
        value = setup_model.Secret(secret_value)
    elif plan.secret_name is not None and not plan.key_present:
        typed = consent.read_secret(setup_texts.KEY_PROMPT.format(display=display), input_stream=input_stream)
        if not typed:
            return fail(f"{verb}: {setup_texts.KEY_EMPTY}")
        value = setup_model.Secret(typed)
    applied = setup_providers.apply_transport(runtime, plan, setup_model.Confirmation.given(plan), value)
    for line in applied.lines:
        report(output_stream, line)
    removed = None
    if choice == operator_mod.TRANSPORT_POOL:
        removed = _offer_key_removal(runtime, provider_id, input_stream=input_stream, output_stream=output_stream,
                                     question=setup_texts.TRANSPORT_REMOVE_KEY)
    return applied_exit(applied, removed)


def _offer_key_removal(runtime: runtime_mod.Runtime, provider_id: str, *, input_stream: TextIO,
                       output_stream: TextIO, question: str, secret: tuple[str, str] | None = None) -> Any:
    """Ask whether the saved key, no longer used, goes too (its own
    transaction); returns what it applied, or None."""

    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers

    try:
        plan = setup_providers.plan_remove_key(runtime, provider_id, secret=secret)
    except setup_model.SetupError:
        return None
    if not consent.confirm(question.format(name=plan.secret_name, display=plan.display), input_stream=input_stream):
        return None
    applied = setup_providers.apply_remove_key(runtime, plan, setup_model.Confirmation.given(plan))
    for line in applied.lines:
        report(output_stream, line)
    return applied


def _providers_edit(runtime: runtime_mod.Runtime, provider_id: str, *, input_stream: TextIO,
                    output_stream: TextIO, interactive: bool) -> int:
    if not interactive:
        return fail(f"providers edit {provider_id} needs a terminal for the editor")
    previous = edit_target(runtime, provider_id)
    if isinstance(previous, int):
        return previous
    edited, kept, problem = run_editor(runtime, previous, f"providers edit {provider_id}")
    if edited is None:
        return fail(f"{operator_mod.file_label(provider_id)} was not changed: {problem}; your edit is kept at {kept}")
    return edit_declaration(runtime, provider_id, previous, edited, input_stream=input_stream,
                            output_stream=output_stream, kept=kept)


def edit_target(runtime: runtime_mod.Runtime, provider_id: str) -> bytes | int:
    """The current bytes of a provider you added, ready for an edit, or the
    exit status of the refusal (already reported)."""

    # Everything before the edit and its keyed preflight is metadata only
    # (providers.d bytes, the ledger, the target checks); the store-scanning
    # operator_context() is built only after the edited bytes pass it.
    env = runtime.gateway_environ()
    ledger_refusal(runtime, f"providers edit {provider_id}")
    directory = operator_mod.providers_dir(env)
    previous = operator_mod.read_providers_dir(directory).files.get(provider_id)
    if previous is None:
        return fail(f"providers edit {provider_id}: no readable {operator_mod.file_label(provider_id)}")
    try:
        # Value-free: the existing file is unvalidated and may carry a
        # credential literal, so a read-only refusal never echoes it.
        operator_mod.check_writable_target(directory, provider_id, None)
    except operator_mod.OperatorError as exc:
        return fail(str(exc))
    return previous


def edited_declaration(previous: bytes, values: Mapping[str, str]) -> bytes:
    """``previous`` with the edit form's answers applied: the display name,
    the base URL, how the key is sent, the model family and the model list
    URL (blank removes it). The id, the kind and the key name stay; every
    other field is kept as it was."""

    document = strict_json.loads(previous)
    block = document["provider"]
    if "display" in values:
        block["display"] = values["display"].strip() or block.get("display")
    if "url" in values:
        block["base_url"] = values["url"].strip()
    auth = block.get("auth")
    if "auth" in values and isinstance(auth, dict) and auth.get("kind") in ("header", "bearer"):
        auth["kind"] = values["auth"]
        if values["auth"] == "header":
            auth.setdefault("header", operator_mod.MIGRATION_HEADER)
        else:
            auth.pop("header", None)
    if "family" in values:
        block["independence_family"] = values["family"].strip() or "unknown"
    if "listing" in values:
        listing = values["listing"].strip()
        if not listing:
            block.pop("listing", None)
        else:
            current = block.get("listing") if isinstance(block.get("listing"), dict) else {}
            shape = "anthropic" if block.get("kind") == "anthropic-compatible" else "openai"
            block["listing"] = {"shape": shape, "auth": "provider", **current, "url": listing}
    return operator_mod.document_bytes(document)


def edit_declaration(runtime: runtime_mod.Runtime, provider_id: str, previous: bytes, edited: bytes, *,
                     input_stream: TextIO, output_stream: TextIO, kept: Path | None = None) -> int:
    """The edit transaction of a provider you added, from its edited bytes
    (the editor's or the form's): the keyed preflight, validation, the
    consequences, the served-change preview, the confirmation, the locked
    write that refuses a file changed meanwhile, the render (undone when it
    refuses) and the verified reload. ``kept``: the editor's copy of the
    edit, named in every refusal and removed once written."""

    label = operator_mod.file_label(provider_id)
    keep = f"\nyour edit is kept at {kept}" if kept is not None else ""
    if edited == previous:
        if kept is not None:
            kept.unlink(missing_ok=True)
        report(output_stream, f"{label}: unchanged")
        return 0
    # The metadata preflight of the edited bytes precedes any validation
    # that could read the store.
    gated = operator_mod.keyed_preflight(runtime.catalog.docs, provider_id, edited)
    if gated is not None:
        return fail(f"{gated.file} was not changed:\n  {gated.text()}{keep}")
    ctx = operator_context(runtime)
    require_ledger(ctx, f"providers edit {provider_id}")
    layer = proposed(ctx, {provider_id: edited})
    own = operator_mod.file_problems(layer, provider_id)
    if own:
        return fail(f"{label} was not changed:\n  " + "\n  ".join(p.text() for p in own) + keep)
    try:
        refuse_blocking(layer, f"providers edit {provider_id}")
    except OperatorCommandError as exc:
        return fail(f"{exc}{keep}")
    keys = {key for key, line in ctx.layer.lines.items() if line.provider_id == provider_id}
    keys |= {key for key, line in layer.lines.items() if line.provider_id == provider_id}
    consequences = definition_consequences(ctx.layer, layer, keys, record_scan(runtime), admitted_keys(runtime))
    if provider_id in layer.providers and layer.route_status.get(provider_id) == "changed":
        consequences.append(f"provider {provider_id}: the route changes — not rendered until "
                            f"claude-multi providers approve {provider_id}")
    for line in consequences:
        report(output_stream, line)
    try:
        preflight = served_preflight(runtime, f"providers edit {provider_id}", changes={provider_id: edited})
    except OperatorCommandError as exc:
        return fail(f"{exc}{keep}")
    if not consent.confirm(f"Write {label}? [y/N] ", input_stream=input_stream):
        from claude_multi.setup import model as setup_model

        # A declined change is exit 3, like every other declined change.
        raise setup_model.Declined(f"{label}: nothing written{keep.replace(chr(10), '; ', 1)}")
    with operator_write(runtime, preflight=preflight):
        fresh = operator_context(runtime)
        if fresh.read.files.get(provider_id) != previous:
            return fail(f"providers edit {provider_id}: the file changed while you edited it — nothing written"
                        f"{keep.replace(chr(10), '; ', 1)}")
        from claude_multi.setup import providers as setup_providers

        with guarded_writes() as undo:
            setup_providers.write_declaration(undo, fresh, provider_id, edited, previous)
        result = render_or_undo(runtime, undo.run, f"providers edit {provider_id}", output_stream)
    if kept is not None:
        kept.unlink(missing_ok=True)
    report(output_stream, f"wrote {label}")
    verify(runtime, result, output_stream)
    return 0


def _providers_approve(runtime: runtime_mod.Runtime, provider_id: str, *, input_stream: TextIO,
                       output_stream: TextIO) -> int:
    verb = f"providers approve {provider_id}"
    consent.require_human(verb, runtime.gateway_environ())
    ctx = operator_context(runtime)
    require_ledger(ctx, verb)
    provider = ctx.layer.providers.get(provider_id)
    if provider is None:
        found = ctx.layer.problems_by_file.get(operator_mod.file_label(provider_id), ())
        detail = ("\n  " + "\n  ".join(p.text() for p in found)) if found else ""
        return fail(f"{verb}: no valid provider declaration {operator_mod.file_label(provider_id)}{detail}")
    if provider.auth_kind == "none":
        report(output_stream, f"provider {provider_id} is keyless: there is no credential route to approve")
        return 0
    if ctx.layer.route_status.get(provider_id) == "approved":
        report(output_stream, f"route {provider_id} is already approved ({provider.origin})")
        return 0
    present = secret_store.default_store(ctx.env).is_set(provider.secret_name or "")
    preflight = served_preflight(runtime, verb, ledger_edit=_route_edit(provider))
    if not consent.confirm(_approval_text(provider, present), input_stream=input_stream):
        return fail(f"{verb}: not approved — nothing changed")
    with operator_write(runtime, preflight=preflight):
        fresh = operator_context(runtime)
        require_ledger(fresh, verb)
        now = fresh.layer.providers.get(provider_id)
        if now is None or now.route_digest != provider.route_digest:
            return fail(f"{verb}: configuration changed while awaiting confirmation — nothing approved; retry")
        from claude_multi.setup import providers as setup_providers

        with guarded_writes() as undo:
            setup_providers.grant_route(undo, fresh, now)
        result = render_or_undo(runtime, undo.run, verb, output_stream)
    report(output_stream, _approved_line(provider))
    verify(runtime, result, output_stream)
    return 0


def _providers_set_key(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                       output_stream: TextIO) -> int:
    """``providers set-key ID [--secret-file FILE] [--yes]``: set or replace
    the API key of a shipped provider or one you added; the gateway reloads
    with it. A replacement asks first (default No; ``--yes`` skips the
    question); a key other providers use too names every one of them, since
    the new key replaces theirs as well. The previous key is restored if the
    render refuses."""

    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers
    from claude_multi.setup import texts as setup_texts

    verb = f"providers set-key {args.provider_id}"
    consent.require_human(verb, runtime.gateway_environ())
    plan = setup_providers.plan_set_key(runtime, args.provider_id)
    show_served(plan.served)
    if plan.replaces_shared:
        question = setup_texts.KEY_SHARED_REPLACE_PROMPT.format(name=plan.secret_name, n=plan.current_length,
                                                               providers=plan.key_users)
    else:
        question = setup_texts.KEY_REPLACE_PROMPT.format(display=plan.display, n=plan.current_length)
    if plan.replaces and getattr(args, "yes", False):
        if plan.replaces_shared:
            prompt = consent.prompt_stream()
            prompt.write(termtext.visible_text(setup_texts.KEY_SHARED_REPLACED_NOTE.format(
                name=plan.secret_name, providers=plan.key_users)) + "\n")
            prompt.flush()
    elif plan.replaces and not consent.confirm(question, input_stream=input_stream):
        report(output_stream, setup_texts.KEY_KEPT.format(display=plan.display))
        return 3
    if args.secret_file is not None:
        try:
            value = operator_mod.read_secret_file(args.secret_file)
        except operator_mod.OperatorError as exc:
            return fail(f"{verb}: {exc}")
    else:
        value = consent.read_secret(setup_texts.KEY_PROMPT.format(display=plan.display), input_stream=input_stream)
    if not value:
        return fail(f"{verb}: {setup_texts.KEY_EMPTY}")
    applied = setup_providers.apply_set_key(runtime, plan, setup_model.Confirmation.given(plan),
                                            setup_model.Secret(value))
    for line in applied.lines:
        report(output_stream, line)
    code = applied_exit(applied)
    if code == 0:
        from claude_multi.setup import status as setup_status

        for pid in setup_status.without_models(runtime, (args.provider_id,)):
            report(output_stream, setup_texts.NO_MODELS_NEXT.format(id=pid))
    return code


def _providers_remove_key(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                          output_stream: TextIO) -> int:
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers
    from claude_multi.setup import texts as setup_texts

    consent.require_human(f"providers remove-key {args.provider_id}", runtime.gateway_environ())
    name = getattr(args, "name", None)
    secret = setup_providers.retained_key(runtime, args.provider_id, name) if name is not None else None
    plan = setup_providers.plan_remove_key(runtime, args.provider_id, secret=secret)
    show_served(plan.served)
    consent.prompt_stream().write("".join(termtext.visible_text(line) + "\n" for line in plan.lines))
    if not args.yes and not consent.confirm(setup_texts.REMOVE_KEY_QUESTION, input_stream=input_stream):
        raise setup_model.Declined(setup_texts.KEY_KEPT.format(display=plan.display))
    applied = setup_providers.apply_remove_key(runtime, plan, setup_model.Confirmation.given(plan))
    for line in applied.lines:
        report(output_stream, line)
    return applied_exit(applied)


def _providers_sign_in(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                       output_stream: TextIO) -> int:
    """``providers sign-in anthropic|openai [--no-browser]``: the typed
    personal-use acknowledgement (once per text), then the gateway program's
    sign-in in this terminal. Exit 0 signed in, 1 not, 130 cancelled."""

    from claude_multi.setup import model as setup_model
    from claude_multi.setup import signin
    from claude_multi.setup import texts as setup_texts

    provider_id = args.provider_id
    consent.require_human(f"providers sign-in {provider_id}", runtime.gateway_environ())
    pool = signin.pool_of(provider_id)
    if not signin.pool_offered(pool):
        return fail(setup_texts.SIGNIN_STRICT)
    ack = signin.current_ack(runtime.environ, pool)
    if ack is None:
        _text_id, text = signin.ack_text(pool)
        prompt = consent.prompt_stream()
        prompt.write(text + "\n")
        prompt.write(setup_texts.ACK_PROMPT)
        prompt.flush()
        typed = (consent._answer_stream(input_stream).readline() or "").strip()
        try:
            ack = signin.record_ack(runtime, pool, typed)
        except setup_model.Refused as exc:
            raise setup_model.Declined(str(exc)) from exc
    plan = signin.plan_sign_in(runtime, provider_id, method="address" if args.no_browser else None)
    output_stream.write(signin.header(plan) + "\n")
    output_stream.flush()
    outcome = signin.run_sign_in(runtime, plan, ack=ack, runner=runtime.signin_runner
                                 if getattr(runtime, "signin_runner", None) is not None else None)
    for line in signin.outcome_lines(plan, outcome, line=True):
        report(output_stream, line)
    return {"signed-in": 0, "cancelled": 130}.get(outcome.status, 1)


def _providers_sign_out(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                        output_stream: TextIO) -> int:
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import signin
    from claude_multi.setup import texts as setup_texts

    provider_id = args.provider_id
    consent.require_human(f"providers sign-out {provider_id}", runtime.gateway_environ())
    plan = signin.plan_sign_out(runtime, provider_id)
    consent.prompt_stream().write("".join(termtext.visible_text(line) + "\n" for line in plan.lines))
    if not args.yes and not consent.confirm(setup_texts.SIGNOUT_QUESTION, input_stream=input_stream):
        raise setup_model.Declined()
    outcome = signin.apply_sign_out(runtime, plan, setup_model.Confirmation.given(plan))
    for line in outcome.lines:
        report(output_stream, line)
    report(output_stream, setup_texts.SIGNED_OUT_UNDO_LINE.format(
        id=provider_id, backup=outcome.backup,
        auth_dir=termtext.visible_text(str(signin.auth_dir(runtime)))))
    return 0 if outcome.complete else 1


def _providers_test(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                    output_stream: TextIO) -> int:
    from claude_multi.setup import check
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import texts as setup_texts

    consent.require_human("providers test", runtime.gateway_environ())
    plan = check.plan_test(runtime, list(dict.fromkeys(args.provider_ids)))
    for note in plan.excluded:
        report(output_stream, note)
    if not plan.targets:
        return fail(setup_texts.TEST_NOTHING)
    if not consent.confirm(check.consent_text(plan), input_stream=input_stream):
        raise setup_model.Declined()
    results = check.run_test(runtime, plan, setup_model.Confirmation.given(plan),
                             on_result=lambda result: report(output_stream, result.text))
    return 0 if results and all(result.outcome == "pass" for result in results) and not plan.excluded else 1


def _providers_toggle(runtime: runtime_mod.Runtime, provider_id: str, enabled: bool, *,
                      output_stream: TextIO) -> int:
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers
    from claude_multi.setup import texts as setup_texts

    try:
        plan = setup_providers.plan_toggle(runtime, provider_id, enabled)
    except setup_model.Refused as exc:
        state_word = "enabled" if enabled else "disabled"
        if str(exc) == setup_texts.TOGGLE_ALREADY.format(id=provider_id, state=state_word):
            report(output_stream, str(exc))
            return 0
        raise
    applied = setup_providers.apply_toggle(runtime, plan, setup_model.Confirmation.given(plan))
    for line in applied.lines:
        report(output_stream, line)
    return 0


def _providers_rm(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                  output_stream: TextIO) -> int:
    """``providers rm ID [--yes]``: refused (with the fix of each blocker)
    while its lines are admitted, bound or live; then asks, removes the
    declaration and its route approval, and offers to remove its key."""

    from claude_multi.setup import model as setup_model
    from claude_multi.setup import providers as setup_providers
    from claude_multi.setup import texts as setup_texts

    plan = setup_providers.plan_remove_provider(runtime, args.provider_id)
    if plan.blockers:
        fixes = [blocker.fix_cli for blocker in plan.blockers]
        users = [blocker.subject for blocker in plan.blockers if blocker.kind in ("live", "unknown-liveness")]
        if users:
            fixes.append(sessions.mark_ended_remedy(users))
        return fail(f"providers rm {args.provider_id}: " + setup_texts.REMOVE_PROVIDER_REFUSED.format(
            fixes="; ".join(fixes)))
    show_served(plan.served)
    if not args.yes and not consent.confirm(setup_texts.REMOVE_PROVIDER_QUESTION.format(file=plan.file_display),
                                            input_stream=input_stream):
        raise setup_model.Declined()
    applied = setup_providers.apply_remove_provider(runtime, plan, setup_model.Confirmation.given(plan))
    for line in applied.lines:
        report(output_stream, line)
    removed = None
    if plan.key_present and plan.secret_name:
        if args.yes or not consent.stdio_ttys() or consent.session_marker(runtime.gateway_environ()):
            report(output_stream, setup_texts.KEY_KEPT_AFTER_RM.format(name=plan.secret_name, id=args.provider_id))
        else:
            removed = _offer_key_removal(runtime, args.provider_id, input_stream=input_stream,
                                         output_stream=output_stream, question=setup_texts.REMOVE_KEY_TOO_QUESTION,
                                         secret=(args.provider_id, plan.secret_name))
    return applied_exit(applied, removed)


def _providers_apply(runtime: runtime_mod.Runtime, *, output_stream: TextIO) -> int:
    # ``providers apply`` is THE publication of declared
    # (hand- or tool-edited) state: the shared preview, then the barrier.
    preflight = served_preflight(runtime, "providers apply")
    with operator_write(runtime, preflight=preflight):
        try:
            _target, result, continuity_report = runtime.render_gateway(policy="explicit")
        except proxy_mod.ConfigPublishedError as exc:
            _target, result, continuity_report = published_unconfirmed(exc, "providers apply")
        except (errors.ClaudeMultiError, OSError) as exc:
            return fail(f"providers apply: {exc}")
    for note in continuity_report.operator:
        consent.prompt_stream().write(f"operator: {termtext.visible_text(note)}\n")
    report(output_stream, f"providers available: {', '.join(result.available_providers) or 'none'}")
    status = verify(runtime, result, output_stream)
    return proxy_mod.RELOAD_CHECK_EXIT.get(status, 1)


# ------------------------------------------------------------ migrate-custom
def _migration_inputs(runtime: runtime_mod.Runtime, ctx: OperatorContext) -> tuple[bytes | None, dict[str, Any] | None]:
    path = custom.registry_path(runtime.environ)
    try:
        source = state.read_private(path) if os.path.lexists(path) else None
    except state.StateError as exc:
        raise OperatorCommandError(f"providers migrate-custom: custom.json is unreadable or unsafe ({exc})") from exc
    return source, operator_mod.read_marker(ctx.env)


def _admit_migrated(runtime: runtime_mod.Runtime, ctx: OperatorContext) -> Callable[[tuple[str, ...], operator_mod.OperatorLayer], None]:
    def admit(keys: tuple[str, ...], layer: operator_mod.OperatorLayer) -> None:
        if not keys:
            return
        view = profile_mod.LineupCatalog.from_docs(operator_mod.merge_docs(ctx.docs, layer))
        runtime.settings_store.update(
            lambda document: document.__setitem__(
                "admitted_lines", sorted(set(document.get("admitted_lines", [])) | set(keys))),
            catalog=view,
        )

    return admit


def _providers_migrate(runtime: runtime_mod.Runtime, apply: bool, *, input_stream: TextIO,
                       output_stream: TextIO) -> int:
    verb = "providers migrate-custom"
    if apply:
        consent.require_human(f"{verb} --apply", runtime.gateway_environ())
    parity = custom.registry_parity(runtime.environ)
    if parity is not None:
        return fail(f"{verb}: refused — {parity}")
    ctx = operator_context(runtime)
    require_ledger(ctx, verb)
    source, marker = _migration_inputs(runtime, ctx)
    plan = operator_mod.plan_custom_migration(ctx.docs, ctx.legacy, source, ctx.read, schemas=ctx.schemas,
                                              ledger=ctx.ledger, marker_doc=marker)
    for line in operator_mod.migration_preview_lines(plan, key_refs=key_references(runtime)):
        report(output_stream, line)
    if plan.status == "refused":
        return 1
    if not apply:
        if plan.status in ("ready", "changed"):
            report(output_stream, "dry run: nothing written — apply with claude-multi providers migrate-custom --apply")
        return 0
    if plan.status == "nothing" or (plan.status == "done" and not plan.pending):
        return 0
    planned_layer = plan.layer

    def planned(document: dict[str, Any]) -> None:
        stamp = operator_mod.utc_stamp()
        for pid in plan.routes:
            document["routes"][pid] = operator_mod.route_record(planned_layer.providers[pid], at=stamp)
        for key in plan.admissions:
            document["admissions"][key] = operator_mod.admission_record(planned_layer, key, at=stamp, via="migrate")

    preflight = served_preflight(
        runtime, f"{verb} --apply", layer=planned_layer,
        ledger_edit=planned if planned_layer is not None and (plan.routes or plan.admissions) else None,
        admit=plan.admissions,
    )
    if plan.status != "done" and not consent.confirm(
            f"Migrate custom.json ({len(plan.targets)} providers.d file(s), {len(plan.admissions)} admission(s))? "
            "[y/N] ", input_stream=input_stream):
        return fail(f"{verb}: not applied — nothing written")
    with operator_write(runtime, preflight=preflight):
        fresh = operator_context(runtime)
        require_ledger(fresh, verb)
        fresh_source, fresh_marker = _migration_inputs(runtime, fresh)
        again = operator_mod.plan_custom_migration(fresh.docs, fresh.legacy, fresh_source, fresh.read,
                                                   schemas=fresh.schemas, ledger=fresh.ledger, marker_doc=fresh_marker)
        if (again.status, again.source_sha256, again.targets, again.writes) != \
                (plan.status, plan.source_sha256, plan.targets, plan.writes):
            return fail(f"{verb}: configuration changed while awaiting confirmation — nothing written; retry")
        operator_mod.apply_custom_migration(fresh.env, again, schemas=fresh.schemas,
                                            admit=_admit_migrated(runtime, fresh))
        try:
            _target, result, continuity_report = runtime.render_gateway(policy="explicit")
        except proxy_mod.ConfigPublishedError as exc:
            _target, result, continuity_report = published_unconfirmed(exc, verb)
        except (errors.ClaudeMultiError, OSError) as exc:
            return fail(f"{verb}: migrated (files, routes, admissions and marker committed) but the render "
                        f"refused — the previous config keeps serving; fix it, then claude-multi providers apply:\n{exc}")
    for note in continuity_report.operator:
        consent.prompt_stream().write(f"operator: {termtext.visible_text(note)}\n")
    migrated = sorted(f"custom-{mid}" for mid in fresh.legacy.get("models", {}))
    expected = [key for key in migrated if key not in {row.key for row in again.displaced}]
    missing = [key for key in expected if key not in set(result.served)]
    if missing:
        report(output_stream, f"migrated, but these selectors are not rendered: {', '.join(missing)} — "
                              "claude-multi doctor")
        verify(runtime, result, output_stream)
        return 1
    report(output_stream, f"migrated custom.json: {len(again.targets)} providers.d file(s); "
                          f"selectors unchanged: {', '.join(expected) or 'none'}")
    verify(runtime, result, output_stream)
    return 0


def _providers_command(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO, output_stream: TextIO,
    interactive: bool,
) -> int:
    """``claude-multi providers ...``: exit 0 ok, 1 refused, 2 usage."""

    command = args.providers_command
    try:
        if command == "list":
            return _providers_list(runtime, args, output_stream)
        if command == "show":
            return _providers_show(runtime, args.provider_id, args.resolved, output_stream)
        if command == "validate":
            return _providers_validate(runtime, args.file, output_stream)
        if command == "template":
            return _providers_template(runtime, args.kind, output_stream)
        if command == "add":
            return _providers_add(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "edit":
            return _providers_edit(runtime, args.provider_id, input_stream=input_stream,
                                   output_stream=output_stream, interactive=interactive)
        if command == "approve":
            return _providers_approve(runtime, args.provider_id, input_stream=input_stream,
                                      output_stream=output_stream)
        if command == "set-key":
            return _providers_set_key(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "remove-key":
            return _providers_remove_key(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "sign-in":
            return _providers_sign_in(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "sign-out":
            return _providers_sign_out(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "test":
            return _providers_test(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command in ("enable", "disable"):
            return _providers_toggle(runtime, args.provider_id, command == "enable", output_stream=output_stream)
        if command == "rm":
            return _providers_rm(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "apply":
            return _providers_apply(runtime, output_stream=output_stream)
        if command == "transport":
            return _providers_transport(runtime, args, input_stream=input_stream, output_stream=output_stream)
        if command == "migrate-custom":
            return _providers_migrate(runtime, bool(args.migrate_apply), input_stream=input_stream,
                                      output_stream=output_stream)
    except OperatorUsageError as exc:
        consent.prompt_stream().write(f"claude-multi providers {command}: {termtext.visible_message(exc)}\n")
        return 2
    except sessions.StateMarkerError:
        raise
    except KeyboardInterrupt:
        consent.prompt_stream().write("claude-multi: cancelled\n")
        return 130
    except _REFUSALS as exc:
        return refusal_exit(exc)
    raise cli_errors.CLIError(f"unsupported providers command {command!r}")


def applied_exit(*applied: Any) -> int:
    """The exit status of a verb that applied a change: 1 when the gateway
    refused the local key at the reload (the change is kept, but the
    gateway is not ready), else 0."""

    return 1 if any(item is not None and item.reload == "token_mismatch" for item in applied) else 0


def refusal_exit(exc: BaseException) -> int:
    """A refusal on stderr with its fix: 3 when the person said no, else 1."""

    from claude_multi.setup import model as setup_model

    text = exc.line_text if isinstance(exc, setup_model.SetupError) else str(exc)
    remedy = getattr(exc, "remedy", None)
    if remedy and remedy not in text:
        text += f"\n  fix: {remedy}"
    fail(text)
    return 3 if isinstance(exc, setup_model.Declined) else 1
