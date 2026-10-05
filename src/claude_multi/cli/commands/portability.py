"""``claude-multi export`` and ``claude-multi import``.

Export reads every source through the ordinary safe stores (never the
credential store) and refuses an invalid source rather than writing a
misleading partial bundle. Stdout export and the import preview run on the
read-only Runtime branch and write nothing; ``export --out`` writes the file
(0600, fsynced) and only then the confirmed-export receipt.

``import --apply`` is the command service (:func:`apply_import`, which the
TUI calls): the human guard, the shared served preview, one confirmation
per keyless origin and one for the whole displayed plan, then ONE
served-change commit phase that re-plans against the target and refuses a
plan that moved. Writes go through the ordinary stores only (providers.d
files, Settings, named bindings, profiles); imported trust requests never
reach the ledger or the admissions. Followers of a written profile get a
pending change (never a live apply).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, TextIO

from claude_multi import catalog
from claude_multi import custom
from claude_multi import errors
from claude_multi import errors as cli_errors
from claude_multi import lineup as lineup_mod
from claude_multi import operator as operator_mod
from claude_multi import portability
from claude_multi import profile as profile_mod
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import strict_json
from claude_multi import termtext
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.consent as consent

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


IMPORT_VERB = "import --apply"
APPLY_QUESTION = "Apply the changes marked ready above? [y/N] "


def _say(text: str) -> None:
    consent.prompt_stream().write("".join(termtext.visible_text(line) + "\n" for line in text.splitlines()))
    consent.prompt_stream().flush()


def _report(output_stream: TextIO, lines: Iterable[str]) -> None:
    output_stream.write("".join(termtext.visible_text(line) + "\n" for line in lines))
    output_stream.flush()


# ============================================================ export
def _profile_names(store: profile_mod.ProfileStore) -> list[str]:
    """Every seed plus every ``*.json`` stem in the profiles directory —
    including files ``names()`` skips (symlinks, unsafe names), so an
    unreadable one refuses the export instead of silently dropping out.

    Fail-closed enumeration (never glob, which hides directory errors): only
    a genuinely absent directory means "no user profiles"; an unreadable or
    invalid one raises ``OSError`` before anything is emitted or written.
    Dot files are the store's own (the seed install record, kept copies of
    replaced or deleted profiles), never profiles."""

    found = set(store.names())
    try:
        with os.scandir(store.root) as entries:
            found |= {entry.name[:-len(".json")] for entry in entries
                      if entry.name.endswith(".json") and len(entry.name) > len(".json")
                      and not entry.name.startswith(".")}
    except FileNotFoundError:
        if os.path.lexists(store.root):
            raise  # a dangling symlink is not an absent directory
    return sorted(found)


def _unreadable_profiles(store: profile_mod.ProfileStore, exc: OSError) -> str:
    return f"the profiles directory {store.root} cannot be read ({exc.strerror or type(exc).__name__})"


def _refuse(text: str) -> cli_errors.CLIError:
    return cli_errors.CLIError(f"export refused: {text} — nothing exported")


def collect_export(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """The portable document of this host (no write, no lock, no provider,
    no credential read: the providers.d screen is shape-only here)."""

    store = runtime.profiles
    seeds = runtime.catalog.seed_profiles
    sources: list[portability.ProfileSource] = []
    try:
        names = _profile_names(store)
    except OSError as exc:
        raise _refuse(_unreadable_profiles(store, exc)) from exc
    for name in names:
        try:
            has_user = store.has_user_strict(name)
            document = store.load(name) if has_user else None
        except profile_mod.ProfileError as exc:
            raise _refuse(f"profile {name}: {exc}") from exc
        except OSError as exc:
            raise _refuse(_unreadable_profiles(store, exc)) from exc
        sources.append(portability.ProfileSource(name, document, seeds.get(name)))
    try:
        bindings = runtime.bindings.bindings()
    except profile_mod.BindingError as exc:
        raise _refuse(str(exc)) from exc
    try:
        settings_document = runtime.settings_store.load()
    except settings_mod.SettingsError as exc:
        raise _refuse(str(exc)) from exc
    env = runtime.gateway_environ()
    legacy = custom.load_registry(runtime.environ)
    snapshot = operator_mod.load_snapshot(env, runtime.catalog.docs, asset_root=runtime.asset_root,
                                          legacy=legacy, secret_values=None)
    if snapshot.ledger_error is not None:
        raise _refuse(snapshot.ledger_error)
    problems = [problem for problem in operator_mod.blocking_problems(snapshot.layer)
                if problem.file.startswith(operator_mod.PROVIDERS_DIRNAME + "/")]
    problems += [problem for problem in (snapshot.read.problems if snapshot.read else ())
                 if problem.code not in operator_mod.NONBLOCKING_CODES and problem not in problems]
    if problems:
        raise _refuse("providers.d has problems:\n  " + "\n  ".join(problem.text() for problem in problems[:8]))
    if legacy.get("providers") or legacy.get("models"):
        _say("note: legacy custom.json declarations are not exported — migrate them first: "
             "claude-multi providers migrate-custom")
    try:
        return portability.build_export(
            launcher_version=runtime.launcher_version,
            catalog_version=runtime.catalog_version,
            profiles=sources,
            selected_seed=catalog.DEFAULT_SEED,
            bindings=bindings,
            settings_document=settings_document,
            provider_files=snapshot.read.files if snapshot.read else {},
            ledger=snapshot.ledger,
            asset_root=runtime.asset_root,
            reconnect=_reconnect(runtime),
        )
    except portability.PortabilityError as exc:
        raise _refuse(str(exc)) from exc


def _reconnect(runtime: runtime_mod.Runtime) -> dict[str, list[str]]:
    """The API-key names set here and the providers signed in here (names only)."""

    from claude_multi.setup import providers as setup_providers

    return setup_providers.reconnect_facts(runtime)


def export_command(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    """``claude-multi export [--out FILE]``."""

    out: Path | None = getattr(args, "export_out", None)
    roots = portability.forbidden_roots(runtime.environ, runtime.home)
    target = portability.check_output_path(out, roots) if out is not None else None
    document = collect_export(runtime)
    data = portability.export_bytes(document)
    payload = data
    _say("\n".join(portability.EXPORT_SUMMARY))
    if target is None:
        # Stdout cannot prove the caller saved anything: no receipt.
        output_stream.write(payload.decode("utf-8"))
        output_stream.flush()
        return 0
    try:
        portability.write_output(target, payload)
    except OSError as exc:
        raise portability.PortabilityError(
            f"export --out {target}: the write failed ({exc.strerror or type(exc).__name__}) — no export "
            "receipt was recorded") from exc
    sha = portability.digest(payload)
    try:
        receipt = portability.write_receipt(runtime.session_store.root, sha, asset_root=runtime.asset_root)
    except (state.StateError, OSError, errors.ClaudeMultiError) as exc:
        raise cli_errors.CLIError(f"export: wrote {target}, but the confirmed-export receipt could not be "
                                  f"recorded ({termtext.visible_message(exc)})") from exc
    _report(output_stream, [f"exported the operator configuration to {target} "
                            f"(sha256 {sha[:16]}…); confirmed export recorded at {receipt.exported_at}"])
    return 0


# ============================================================ import
def _effective_for(runtime: runtime_mod.Runtime, snapshot: operator_mod.OperatorSnapshot,
                   docs: dict[str, Any], document: Any) -> settings_mod.Effective:
    import claude_multi.cli.runtime as runtime_mod

    eff = settings_mod.effective(
        document, provider_ids=docs["providers"]["providers"],
        line_keys=(docs["models-v2"] if "models-v2" in docs else docs["models"])["models"],
    )
    return runtime_mod.operator_effective(eff, snapshot)


def import_target(runtime: runtime_mod.Runtime) -> portability.ImportTarget:
    """The target host through its ordinary stores and validators (reads
    only: the operator snapshot screens declarations against the stored
    credential values in memory, as ``providers add`` does)."""

    ctx = providers_cmd.operator_context(runtime)
    providers_cmd.require_ledger(ctx, "import")
    store = runtime.profiles
    profiles: dict[str, Any] = {}
    unreadable: dict[str, str] = {}
    try:
        names = _profile_names(store)
    except OSError as exc:
        raise cli_errors.CLIError(f"import: {_unreadable_profiles(store, exc)} — nothing written") from exc
    for name in names:
        try:
            if store.has_user_strict(name):
                profiles[name] = store.load(name)
        except profile_mod.ProfileError as exc:
            unreadable[name] = str(exc)
        except OSError as exc:
            raise cli_errors.CLIError(f"import: {_unreadable_profiles(store, exc)} — nothing written") from exc
    try:
        bindings = runtime.bindings.bindings()
        settings_document = runtime.settings_store.load()
    except (profile_mod.BindingError, settings_mod.SettingsError) as exc:
        raise cli_errors.CLIError(f"import: {exc} — nothing written") from exc
    legacy = custom.load_registry(runtime.environ)
    lcat = runtime.lineup_catalog()
    docs = operator_mod.merge_docs(runtime.catalog.docs, ctx.layer, legacy=legacy)
    tolerated = settings_mod.unknown_entries(settings_document, catalog=lcat, custom_registry=legacy)
    on_disk_binding = settings_document.get(settings_mod.WORKFLOW_DEFAULT_BINDING_KEY)

    def settings_error(document: Any) -> str | None:
        try:
            settings_mod.check_save(document, catalog=lcat, custom_registry=legacy, tolerated=tolerated,
                                    tolerated_binding=on_disk_binding)
            _effective_for(runtime, ctx.snapshot, docs, document)
        except settings_mod.SettingsError as exc:
            return str(exc)
        return None

    def binding_errors(name: str, value: Any, _bindings: Any, document: Any) -> list[str]:
        return profile_mod.binding_errors(name, value, lcat, _effective_for(runtime, ctx.snapshot, docs, document))

    def profile_errors(document: Any, bindings_now: Any, settings_now: Any) -> list[str]:
        return list(profile_mod.evaluate(document, lcat, bindings=dict(bindings_now),
                                         effective=_effective_for(runtime, ctx.snapshot, docs, settings_now),
                                         ad_hoc=False).errors)

    def provider_refusal(file_id: str, declaration: Any) -> str | None:
        try:
            operator_mod.check_writable_target(operator_mod.providers_dir(ctx.env), file_id, declaration,
                                               lambda: operator_mod.store_scan_values(ctx.env))
        except operator_mod.OperatorError as exc:
            return str(exc)
        return None

    admitted = frozenset(runtime.current_effective().admitted_lines)
    approved = frozenset(pid for pid, status in ctx.layer.route_status.items() if status == "approved")
    transports = dict(ctx.ledger.transport_choices) if ctx.ledger is not None else {}
    files = dict(ctx.read.files)
    fingerprint = portability.target_fingerprint(
        {name: hashlib.sha256(strict_json.canonical_bytes(doc)).hexdigest() for name, doc in sorted(profiles.items())},
        sorted(unreadable), bindings, settings_document,
        {fid: hashlib.sha256(raw).hexdigest() for fid, raw in sorted(files.items())},
        sorted(approved), sorted(admitted), transports,
        ctx.ledger.sha256 if ctx.ledger is not None else None,
    )
    return portability.ImportTarget(
        profiles=profiles, unreadable_profiles=unreadable, seeds=runtime.catalog.seed_profiles,
        bindings=bindings, settings=settings_document, provider_files=files,
        provider_refusal=provider_refusal, propose=lambda changes: providers_cmd.proposed(ctx, changes),
        approved_routes=approved, admitted=admitted, transport_choices=transports,
        settings_error=settings_error, binding_errors=binding_errors, profile_errors=profile_errors,
        fingerprint=fingerprint, provider_keys=_provider_keys(runtime),
    )


def _provider_keys(runtime: runtime_mod.Runtime) -> dict[str, tuple[str, str]]:
    from claude_multi.setup import providers as setup_providers

    return setup_providers.provider_key_names(runtime)


def _followers_lines(runtime: runtime_mod.Runtime, names: Iterable[str]) -> list[str]:
    names = sorted(set(names))
    if not names:
        return []
    found, unreadable = lineup_mod.followers(runtime, names)
    if not found and not unreadable:
        return []
    running = sum(1 for follower in found if follower.live)
    lines = [f"followers: {len(found)} session(s) follow {', '.join(names)} ({running} running) — each gets a "
             "pending change that applies at its next resume (never applied live)"]
    if unreadable:
        lines.append(f"followers: {len(unreadable)} unreadable session record(s) are not updated")
    return lines


def _served(runtime: runtime_mod.Runtime, plan: portability.ImportPlan, *,
            show: bool) -> providers_cmd.ServedPreflight:
    return providers_cmd.served_preflight(
        runtime, "import", changes=plan.providers, provider_enabled=plan.provider_enabled or None, show=show)


@dataclass(frozen=True)
class Preview:
    """What the operator was shown: the import plan and the exact served
    preflight whose lines were displayed (the commit phase's CAS baseline),
    or the refusal that was displayed instead."""

    plan: portability.ImportPlan
    preflight: providers_cmd.ServedPreflight | None
    refusal: cli_errors.CLIError | None = None


def preview(runtime: runtime_mod.Runtime, bundle: portability.Bundle, output_stream: TextIO,
            *, excluded: Iterable[str] = ()) -> Preview:
    """The read-only preview: the classified plan, the served-change plan of
    its declarations and the followers of the profiles it would write."""

    plan = portability.plan_import(bundle, import_target(runtime), excluded=excluded)
    lines = plan.preview_lines()
    served: providers_cmd.ServedPreflight | None = None
    refusal: cli_errors.CLIError | None = None
    try:
        served = _served(runtime, plan, show=False)
        served_lines = ["", *served.plan.lines()]
    except cli_errors.CLIError as exc:
        refusal = exc
        served_lines = ["", f"served change: {exc}"]
    _report(output_stream, [*lines, *served_lines, *_followers_lines(runtime, plan.profiles)])
    return Preview(plan, served, refusal)


@dataclass
class ApplyReport:
    applied: list[str]
    unconfirmed: list[str]
    not_applied: list[str]
    error: str | None = None
    # Written by this attempt and still on disk although its batch stopped
    # (the rollback could not remove it); never published.
    retained: list[str] = field(default_factory=list)


def _rollback_providers(env: dict[str, str], written: dict[str, bytes]) -> dict[str, str]:
    """Remove this attempt's providers.d files (new files only, the store
    lock held) — only a file still holding exactly the bytes this attempt
    wrote. Returns what could not be removed, with the reason."""

    directory = operator_mod.providers_dir(env)
    retained: dict[str, str] = {}
    for file_id in sorted(written):
        target = directory / f"{file_id}.json"
        detail = "removal returned without an error"
        try:
            if state.read_private(target) != written[file_id]:
                retained[file_id] = "it changed after this import wrote it"
                continue
            providers_cmd.restore_file(env, file_id, None)
        except (errors.ClaudeMultiError, OSError) as exc:
            detail = termtext.visible_message(exc)
        # A normal return is not proof of removal; confirm after every attempt.
        try:
            os.lstat(target)
        except FileNotFoundError:
            pass  # Proven removed: no entry remains.
        except OSError as stat_exc:
            retained[file_id] = (f"its removal is unconfirmed: {detail}; "
                                 f"its presence cannot be checked "
                                 f"({stat_exc.strerror or type(stat_exc).__name__})")
        else:
            retained[file_id] = f"the removal failed: {detail}"
    return retained


def _commit_providers(runtime: runtime_mod.Runtime, env: dict[str, str], providers: dict[str, bytes],
                      report: ApplyReport, output_stream: TextIO) -> tuple[Any, bool]:
    """Write the new providers.d files one by one, then ONE explicit render.

    A write failure (before or after its rename) or a render refusal rolls
    back exactly this attempt's files; returns ``(render result, stopped)``.
    Each declaration is reported on its own: applied, retained (the rollback
    could not remove it; never published) or not applied."""

    labels = {file_id: f"providers.d {file_id}" for file_id in providers}
    written: dict[str, bytes] = {}
    failure: str | None = None
    for file_id in sorted(providers):
        try:
            operator_mod.write_provider_bytes(env, file_id, providers[file_id])
        except state.CommittedStateError as exc:
            written[file_id] = providers[file_id]  # renamed into place, durability unconfirmed
            failure = f"{labels[file_id]}: {exc.strerror or exc}"
            break
        except (errors.ClaudeMultiError, OSError) as exc:
            failure = f"{labels[file_id]}: {termtext.visible_message(exc)}"
            break
        written[file_id] = providers[file_id]
    retained: dict[str, str] = {}
    if failure is None:
        def undo() -> None:
            retained.update(_rollback_providers(env, written))

        try:
            result = providers_cmd.render_or_undo(runtime, undo, "import", output_stream)
        except providers_cmd.OperatorCommandError as exc:
            if not retained:
                raise  # every file was removed again: truly nothing changed
            failure = f"providers.d render: {termtext.visible_message(exc)}"
        else:
            report.applied += [labels[file_id] for file_id in sorted(providers)]
            return result, False
    else:
        retained = _rollback_providers(env, written)
    report.error = failure
    for file_id in sorted(providers):
        if file_id in retained:
            report.retained.append(
                f"{labels[file_id]} (not published: {retained[file_id]}; "
                f"remove it with: claude-multi providers rm {file_id})")
        else:
            report.not_applied.append(labels[file_id])
    return None, True


def apply_import(
    runtime: runtime_mod.Runtime,
    bundle: portability.Bundle,
    *,
    confirmed: str,
    excluded: Iterable[str] = (),
    preflight: providers_cmd.ServedPreflight,
    output_stream: TextIO,
) -> ApplyReport:
    """The command service for a confirmed plan (the TUI calls this too).

    One served-change commit phase (migration guard → barrier → token
    rotation → operator store lock; Settings, bindings and profiles take
    their own store locks inside it): the target is re-read and re-planned,
    and a plan whose digest differs from the ``confirmed`` one refuses with
    nothing written. Writes run in order — providers.d (file by file, then
    one explicit render; a write failure or a render refusal rolls back this
    attempt's files), Settings, named bindings, profiles — and a failure
    after a commit is reported item by item, never as "nothing changed"."""

    report = ApplyReport([], [], [])
    result = None
    with providers_cmd.operator_write(runtime, preflight=preflight) as token:
        plan = portability.plan_import(bundle, import_target(runtime), excluded=excluded)
        if plan.digest != confirmed:
            raise providers_cmd.OperatorCommandError(f"import: {portability.STALE_PLAN}")
        env = runtime.gateway_environ()
        steps: list[tuple[str, Any]] = []
        if plan.settings_changes:
            steps.append(("settings " + ", ".join(plan.settings_changes), "settings"))
        steps += [(f"binding {name}", ("binding", name)) for name in sorted(plan.bindings)]
        steps += [(f"profile {name}", ("profile", name)) for name in sorted(plan.profiles)]
        written_profiles: list[str] = []
        if plan.providers:
            result, stopped = _commit_providers(runtime, env, dict(plan.providers), report, output_stream)
            if stopped:
                report.not_applied += [label for label, _ in steps]
                steps = []
        for index, (label, step) in enumerate(steps):
            try:
                if step == "settings":
                    wanted = plan.settings

                    def mutate(document: dict[str, Any]) -> None:
                        for key in settings_mod.POLICY_KEYS:
                            if key in plan.settings_changes:
                                document[key] = wanted[key]
                        for pid, enabled in plan.provider_enabled.items():
                            document.setdefault("providers", {})[pid] = {"enabled": enabled}

                    runtime.settings_store.update(mutate, catalog=runtime.lineup_catalog(),
                                                  custom_registry=custom.load_registry(runtime.environ))
                elif step[0] == "binding":
                    name = step[1]

                    def add(document: dict[str, Any], name: str = name) -> None:
                        if name in document["bindings"]:
                            raise profile_mod.BindingError(f"named binding {name!r} appeared meanwhile; "
                                                           "it is not overwritten")
                        document["bindings"][name] = dict(plan.bindings[name])

                    runtime.bindings.update(add, cat=runtime.lineup_catalog(), profiles=runtime.profiles,
                                            effective=runtime.current_effective())
                else:
                    runtime.profiles.create(plan.profiles[step[1]])
                    written_profiles.append(step[1])
                report.applied.append(label)
            except state.CommittedStateError as exc:
                report.unconfirmed.append(f"{label} (written; durability unconfirmed: {exc.strerror or exc})")
                if step[0] == "profile":
                    written_profiles.append(step[1])
                report.error = f"{label}: {exc.strerror or exc}"
                report.not_applied += [later for later, _ in steps[index + 1:]]
                break
            except (errors.ClaudeMultiError, OSError) as exc:
                report.error = f"{label}: {termtext.visible_message(exc)}"
                report.not_applied += [later for later, _ in steps[index:]]
                break
        if written_profiles:
            # Inside this phase (the held token, never a second acquisition):
            # followers get a pending change; a served mutation never live-applies.
            lineup_mod.on_saved(runtime, written_profiles, apply_live=False, out=output_stream,
                                barrier=token, served=True)
    if result is not None:
        providers_cmd.verify(runtime, result, output_stream)
    return report


def import_command(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
                   output_stream: TextIO) -> int:
    """``claude-multi import FILE [--apply]``."""

    apply = bool(getattr(args, "import_apply", False))
    if apply:
        # Before any credential-store read (the declaration screen) or write.
        consent.require_human(IMPORT_VERB, runtime.environ)
    try:
        bundle = portability.load_export(portability.read_bundle(args.import_file), asset_root=runtime.asset_root)
    except portability.PortabilityError as exc:
        raise cli_errors.CLIError(f"{exc} — nothing written") from exc
    shown = preview(runtime, bundle, output_stream)
    plan = shown.plan
    if not apply:
        _report(output_stream, ["", f"Import plan digest: {plan.digest[:16]}",
                                "Nothing was written. Apply the ready items: claude-multi import FILE --apply"])
        return 0
    if not plan.has_changes:
        _report(output_stream, ["", "Nothing to apply on this host."])
        return 0
    excluded: set[str] = set()
    for provider_id, origin in plan.keyless:
        if not consent.confirm(portability.KEYLESS_QUESTION.format(provider=provider_id, origin=origin),
                               input_stream=input_stream):
            excluded.add(provider_id)
    if excluded:
        _report(output_stream, ["", f"excluded (declined): {', '.join(sorted(excluded))} — the plan becomes:"])
        shown = preview(runtime, bundle, output_stream, excluded=excluded)
        plan = shown.plan
        if not plan.has_changes:
            _report(output_stream, ["", "Nothing to apply on this host."])
            return 0
    if shown.preflight is None:
        # The displayed served plan refused: never re-plan it silently.
        raise shown.refusal or cli_errors.CLIError("import: no served preview — nothing written")
    if not consent.confirm(APPLY_QUESTION, input_stream=input_stream):
        _say("import: declined — nothing written")
        return 1
    # The commit revalidates exactly the preflight whose lines were shown:
    # anything that moved since (a new session on a retargeted alias
    # included) refuses there; the confirmation baseline is never refreshed.
    report = apply_import(runtime, bundle, confirmed=plan.digest, excluded=excluded, preflight=shown.preflight,
                          output_stream=output_stream)
    skipped = [item.brief() for item in plan.items
               if item.status in (portability.CONFLICT, portability.BLOCKED)]
    if report.error is not None:
        _report(output_stream, [
            "", f"Import stopped at {report.error}.",
            "Applied: " + (", ".join(report.applied) or "nothing") + ".",
            *([f"Written, durability unconfirmed: {', '.join(report.unconfirmed)}."] if report.unconfirmed else []),
            *([f"Written and kept: {', '.join(report.retained)}."] if report.retained else []),
            "Not applied: " + (", ".join(report.not_applied) or "nothing") + ".",
            portability.APPLIED_TRUST,
            "Fix the cause and rerun claude-multi import FILE --apply (applied items are unchanged then).",
        ])
        return 1
    _report(output_stream, [
        "", portability.APPLIED_HEADER,
        "Not applied: " + ("; ".join(skipped) if skipped else "nothing") + ".",
        portability.APPLIED_TRUST,
        portability.APPLIED_RERUN,
        *plan.trust_lines(),
    ])
    return 0


_REFUSALS = (*providers_cmd._REFUSALS, portability.PortabilityError)


def command(runtime: runtime_mod.Runtime, args: argparse.Namespace, *, input_stream: TextIO,
            output_stream: TextIO) -> int:
    """Dispatch; a refusal exits 1 with its text on stderr (like the
    operator verbs), argparse misuse stays 2."""

    try:
        if args.command == "export":
            return export_command(runtime, args, output_stream)
        return import_command(runtime, args, input_stream=input_stream, output_stream=output_stream)
    except _REFUSALS as exc:
        return providers_cmd.fail(str(exc))
