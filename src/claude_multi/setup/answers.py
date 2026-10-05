"""``claude-multi setup --answers FILE``: a prepared setup.

The answers file names keys only by file (``api_key_file``), never by
value; sign-ins and tests are refused (they need a person at the terminal).
Every file it names is checked before anything is shown or written.

Each answer becomes one item, planned before anything runs: the preview
shows the item's real plan (every write, a route approval's disclosures,
key names, the Claude Code download with its size, a starter profile's
name, slots and spend notes, the default profile), and the item keeps a
digest of what was shown. When it runs, in order and as its own
transaction, the item plans again and goes ahead only while that digest is
unchanged; anything that moved in between refuses that item with nothing
written (the default profile: while ``choices.json`` is still what the
preview read, or what an earlier item of the run wrote). Items that grant
authority (key imports, route approvals, the transport, the key-file
pointer, a Claude Code download) are guarded: on a terminal they run after
the one consolidated confirmation; off a terminal they are skipped and
named. A file is never the approval.

Because every item is planned against the state before the run, an item
that needs a provider only an earlier entry of the same file declares is
refused with the whole file (a usage error, before anything is shown); a
declared endpoint takes its key in its own entry (``api_key_file``).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_multi import errors, operator as operator_mod, paths, secret_store, strict_json
from claude_multi import validate as schema_validate
from claude_multi.setup import model, texts

SCHEMA = "setup-answers.schema.json"
MAX_BYTES = 256 * 1024


class AnswersError(model.SetupError):
    """The answers file is invalid (usage: exit 2)."""


def _schema() -> dict[str, Any]:
    from claude_multi import resources_root

    return strict_json.load(resources_root() / "schemas" / SCHEMA)


def _refusals(document: Any) -> None:
    """The fixed refusals, before the schema (their own texts)."""

    if not isinstance(document, dict):
        return
    items = document.get("providers")
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        pid = item.get("id") if isinstance(item.get("id"), str) else "ID"
        if "sign_in" in item:
            raise AnswersError(texts.ANSWERS_SIGN_IN.format(id=pid))
        if "test" in item:
            raise AnswersError(texts.ANSWERS_TEST.format(id=pid))
        if "api_key" in item:
            raise AnswersError(texts.ANSWERS_INLINE_KEY)
    if "sign_in" in document:
        raise AnswersError(texts.ANSWERS_SIGN_IN.format(id="ID"))
    if "test" in document:
        raise AnswersError(texts.ANSWERS_TEST.format(id="ID"))


def parse(raw: bytes) -> dict[str, Any]:
    """Validate an answers document (raises :class:`AnswersError`).

    A document that does not parse is described by a fixed category only:
    the parser's own text can quote what the file holds (a duplicated key,
    for one), and the file is screened for secrets only once it parses."""

    if len(raw) > MAX_BYTES:
        raise AnswersError(f"the answers file is larger than {MAX_BYTES} bytes")
    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise AnswersError(texts.ANSWERS_NOT_JSON.format(reason=operator_mod._json_failure(exc))) from None
    except (ValueError, RecursionError):
        raise AnswersError(texts.ANSWERS_NOT_JSON.format(reason="it cannot be parsed")) from None
    hits = operator_mod._secret_hits(document, "$", ())
    if hits:
        raise AnswersError(texts.ANSWERS_SECRET.format(path=hits[0]))
    _refusals(document)
    problems = schema_validate.validate(document, _schema(), "$")
    if problems:
        raise AnswersError("the answers file is invalid: " + "; ".join(problems[:4]))
    for index, item in enumerate(document.get("providers", [])):
        modes = [key for key in ("endpoint", "server") if key in item]
        if len(modes) > 1:
            raise AnswersError(f"providers[{index}]: endpoint and server exclude each other")
        if "server" in item and "api_key_file" in item:
            raise AnswersError(f"providers[{index}]: a server on your network takes no key")
        for path_key in ("api_key_file",):
            if path_key in item and not os.path.isabs(item[path_key]):
                raise AnswersError(f"providers[{index}].{path_key} must be an absolute path")
    return document


def check_files(document: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    """Every file the answers name, checked before anything is shown or
    applied (raises :class:`AnswersError`): each ``api_key_file`` is a
    private regular file you own holding one key-shaped line, and
    ``keys_file`` is a key file a pointer may select. Values are read only
    to be checked and are never shown."""

    from claude_multi.setup import providers

    for index, item in enumerate(document.get("providers", [])):
        if "api_key_file" not in item:
            continue
        try:
            providers.check_value(read_key_file(item["api_key_file"]))
        except model.SetupError as exc:
            raise AnswersError(texts.ANSWERS_KEY_FILE_BAD.format(index=index, id=item["id"], reason=exc)) from None
    keys_file = document.get("keys_file")
    if isinstance(keys_file, str):
        try:
            secret_store.check_key_file(secret_store.expand(keys_file, environ), environ)
        except (secret_store.SecretStoreError, OSError) as exc:
            raise AnswersError(texts.ANSWERS_KEYS_FILE_BAD.format(reason=exc)) from None


def load(path: Path | str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The answers file at ``path``, validated; with ``environ`` (the
    runtime's), the files it names are checked too (:func:`check_files`)."""

    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise AnswersError(f"the answers file {target} cannot be read ({exc.strerror})") from exc
    document = parse(raw)
    if environ is not None:
        check_files(document, environ)
    return document


def read_key_file(path: str) -> model.Secret:
    """One key from a private one-line file (absolute, regular, owned, 0600)."""

    try:
        return model.Secret(operator_mod.read_secret_file(path))
    except operator_mod.OperatorError as exc:
        raise AnswersError(str(exc).replace("--secret-file", "api_key_file")) from exc


@dataclass
class Item:
    """One answer: what it does (names only) and how it runs.

    ``lines`` is the preview; ``problem`` says why the item cannot run (found
    while planning it; the item then fails without running); ``run``
    applies it and returns the result."""

    kind: str
    subject: str
    guarded: bool
    lines: tuple[str, ...]
    run: Callable[[], model.Applied] = field(repr=False)
    verb: str = ""
    problem: str | None = None


# ------------------------------------------------------------------ what a preview binds


def _fact(value: Any) -> Any:
    if isinstance(value, bytes):
        return "sha256:" + hashlib.sha256(value).hexdigest()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _fact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_fact(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    return value


def reviewed(plan: Any) -> str:
    """The digest of what a person reviews in ``plan``: every field of it
    except the served-state sample (an earlier item of the same run changes
    that) and the source that selected the key file (a pointer selected by
    an earlier item names the same file the preview planned with)."""

    facts: dict[str, Any] = {}
    for item in dataclasses.fields(plan):
        if item.name in ("digest", "served"):
            continue
        value = getattr(plan, item.name)
        if item.name == "key_file":
            value = os.path.realpath(value[1]) if value and value[1] else ""
        facts[item.name] = _fact(value)
    return model.digest_of("answers-review", type(plan).__name__, facts)


class _KeyFileSelected:
    """The runtime as plans see it once the answers' ``keys_file`` is
    selected: every reader of this view resolves that file (planning only;
    the environment override here names the file the pointer will name)."""

    def __init__(self, runtime: Any, target: Path) -> None:
        self._runtime = runtime
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def gateway_environ(self) -> dict[str, str]:
        return {**self._runtime.gateway_environ(), secret_store.SECRET_ENV_OVERRIDE: str(self._target)}


def _planning_runtime(runtime: Any, document: Mapping[str, Any]) -> Any:
    """The runtime key plans are previewed with: one that reads the
    answers' ``keys_file`` when that item selects another file first."""

    keys_file = document.get("keys_file")
    if not isinstance(keys_file, str):
        return runtime
    env = runtime.gateway_environ()
    target = secret_store.expand(keys_file, env)
    try:
        current = secret_store.key_file_location(env)
    except secret_store.SecretStoreError:
        return _KeyFileSelected(runtime, target)
    if current.source == "environment" or os.path.realpath(current.path) == os.path.realpath(target):
        return runtime  # the override wins over a pointer, or nothing changes
    return _KeyFileSelected(runtime, target)


def _projected_transports(runtime: Any, document: Mapping[str, Any]) -> Any:
    """The runtime a starter is planned with: when the document selects a
    provider transport, its operator reads see that transport chosen and its
    reviewed route approved, as they will be once that item ran (planning
    only; nothing is written). Without a transport answer, or over an
    unusable ledger, the runtime itself."""

    chosen = {entry["id"]: entry["transport"] for entry in document.get("providers", [])
              if isinstance(entry.get("transport"), str)}
    if not chosen:
        return runtime
    current = runtime.operator_snapshot

    def operator_snapshot() -> operator_mod.OperatorSnapshot:
        snapshot = current()
        if snapshot.ledger_error is not None:
            return snapshot
        ledger = snapshot.ledger if snapshot.ledger is not None else operator_mod.OperatorLedger.empty()
        choices, routes = dict(ledger.transport_choices), dict(ledger.routes)
        for pid, choice in chosen.items():
            choices[pid] = choice
            alternative = operator_mod.transport_alternative(pid, choice)
            if alternative is not None:
                routes[pid] = operator_mod.transport_route_record(alternative, at="planned")
        return dataclasses.replace(snapshot, ledger=dataclasses.replace(
            ledger, transport_choices=choices, routes=routes))

    view = copy.copy(runtime)
    view.operator_snapshot = operator_snapshot
    return view


def _problem(exc: BaseException) -> str:
    text = exc.line_text if isinstance(exc, model.SetupError) else str(exc)
    remedy = getattr(exc, "remedy", None)
    return f"{text} — {remedy}" if remedy and remedy not in text else text


def _failing(kind: str, subject: str, guarded: bool, lines: tuple[str, ...], problem: str,
             verb: str = "") -> Item:
    def run() -> model.Applied:
        raise model.Refused(problem)

    return Item(kind, subject, guarded, lines, run, verb, problem)


def _writes(plan: Any) -> tuple[str, ...]:
    return (texts.ANSWERS_WRITES.format(files=", ".join(plan.writes)),) if plan.writes else ()


def _planned_item(kind: str, subject: str, guarded: bool, verb: str, runtime: Any, view: Any,
                  make: Callable[[Any], Any], describe: Callable[[Any], tuple[str, ...]],
                  apply: Callable[[Any, Any, model.Confirmation], model.Applied]) -> Item:
    """An item whose plan is made now for the preview (with ``view``) and
    made again when it runs (with ``runtime``): it applies only while what
    the preview showed is unchanged."""

    try:
        plan = make(view)
    except (model.SetupError, errors.ClaudeMultiError, OSError) as exc:
        return _failing(kind, subject, guarded, (texts.ANSWERS_ITEM_HEAD.format(id=subject, what=kind),),
                        _problem(exc), verb)
    shown = reviewed(plan)

    def run() -> model.Applied:
        fresh = make(runtime)
        if reviewed(fresh) != shown:
            raise model.Stale(texts.ANSWERS_ITEM_WHAT.format(id=subject))
        return apply(runtime, fresh, model.Confirmation.given(fresh))

    return Item(kind, subject, guarded, (*describe(plan), *_writes(plan)), run, verb)


# ------------------------------------------------------------------ the items


def _claude_item(runtime: Any, claude: Mapping[str, Any], progress: Callable[[str], None]) -> Item:
    from claude_multi import acquire, pin
    from claude_multi.setup import external

    source = claude.get("source", "auto")
    from_path = claude.get("from")
    allowed = bool(claude.get("accept_download")) and source in ("auto", "download")
    claude_from = Path(from_path) if from_path and source != "download" else None
    contract = runtime.catalog.docs["native-contract"]
    try:
        version = pin.version(contract)
    except (KeyError, TypeError, ValueError):
        version = "?"
    lines = [texts.ANSWERS_CLAUDE_COPY.format(version=version, source=str(from_path) if claude_from
                                              else texts.ANSWERS_CLAUDE_ANY)]
    reviewed_plan: Any = None
    if allowed:
        try:
            reviewed_plan = acquire.download_plan(contract, runtime.environ, pin.host_platform())
        except (errors.ClaudeMultiError, KeyError, ValueError) as exc:
            return _failing("claude", "claude", True, tuple(lines), _problem(exc), "setup --step claude")
        lines.append(texts.ANSWERS_CLAUDE_DOWNLOAD)
        lines.extend(f"  {line}" for line in reviewed_plan.lines(runtime.environ))
    else:
        lines.append(texts.ANSWERS_CLAUDE_NO_DOWNLOAD)

    def consent(plan: Any) -> bool:
        if reviewed_plan is None:
            return False
        if plan != reviewed_plan:
            raise model.Stale(texts.ANSWERS_DOWNLOAD_WHAT)
        from claude_multi.cli import consent as consent_mod

        consent_mod.require_human(consent_mod.SETUP_DOWNLOAD_VERB, runtime.environ)
        return True  # the consolidated confirmation covered this plan

    def run() -> model.Applied:
        outcome = external.acquire_claude(runtime, consent=consent, progress=progress, claude_from=claude_from)
        if outcome.path is None:
            raise model.Refused(f"Claude Code {outcome.version} is not set up for claude-multi"
                                + (f" ({outcome.notes[0]})" if outcome.notes else ""))
        return model.Applied("claude", "claude", "not-needed", (f"Claude Code {outcome.version}: {outcome.state}",))

    return Item("claude", "claude", allowed, tuple(lines), run, "setup --step claude")


def _gateway_item(runtime: Any) -> Item:
    from claude_multi.setup import external

    def run() -> model.Applied:
        outcome = external.ensure_gateway(runtime)
        if outcome is not None and not outcome.ok:
            raise model.Refused(outcome.message, remedy=outcome.remedy)
        return model.Applied("gateway", "gateway", "not-needed", ("the local gateway is running",))

    return Item("gateway", "gateway", False, (texts.ANSWERS_GATEWAY,), run)


def _keys_file_item(runtime: Any, keys_file: str) -> Item:
    from claude_multi.setup import providers

    env = runtime.gateway_environ()
    target = secret_store.expand(keys_file, env)
    try:
        names = secret_store.check_key_file(target, env)
    except (secret_store.SecretStoreError, OSError) as exc:
        return _failing("keys-file", "keys_file", True, (texts.ANSWERS_KEYS_FILE_HEAD.format(path=keys_file),),
                        str(exc), "setup --keys-file")
    shown = paths.display(target, runtime.environ)
    real = os.path.realpath(target)

    def run() -> model.Applied:
        if os.path.realpath(target) != real:
            raise model.Stale(texts.ANSWERS_ITEM_WHAT.format(id="keys_file"))
        return providers.select_key_file(runtime, target)

    lines = (texts.ANSWERS_KEYS_FILE.format(path=shown, n=len(names)),
             texts.ANSWERS_WRITES.format(files=paths.display(paths.secret_pointer_path(env), runtime.environ)))
    return Item("keys-file", "keys_file", True, lines, run, "setup --keys-file")


def _key_saved(view: Any, name: str) -> bool:
    try:
        return secret_store.default_store(view.gateway_environ()).is_set(name)
    except (secret_store.SecretStoreError, OSError):
        return False


def _key_line(pid: str, name: str, source: str, destination: str) -> str:
    return texts.ANSWERS_KEY.format(id=pid, name=name, source=source, path=destination)


def _toggle_item(runtime: Any, pid: str, enabled: bool) -> Item:
    from claude_multi.setup import providers

    state = "on" if enabled else "off"
    return _planned_item(
        "toggle", pid, False, "", runtime, runtime,
        lambda rt: providers.plan_toggle(rt, pid, enabled),
        lambda plan: (texts.ANSWERS_TOGGLE.format(id=pid, state=state),),
        lambda rt, plan, confirmation: providers.apply_toggle(rt, plan, confirmation))


def _server_item(runtime: Any, pid: str, server: Mapping[str, Any]) -> Item:
    from claude_multi.setup import providers

    return _planned_item(
        "server", pid, False, "", runtime, runtime,
        lambda rt: providers.plan_add_preset(rt, server["preset"], pid, server["base_url"]),
        lambda plan: plan.lines,
        lambda rt, plan, confirmation: providers.apply_add_preset(rt, plan, confirmation))


def _endpoint_item(runtime: Any, view: Any, pid: str, endpoint: Mapping[str, Any], key_file: str | None) -> Item:
    from claude_multi.setup import providers

    values = {"id": pid, "kind": endpoint["kind"], "url": endpoint["base_url"],
              "auth": endpoint.get("auth") or "", "family": endpoint.get("family") or "unknown",
              "listing": endpoint.get("listing") or ""}

    def describe(plan: Any) -> tuple[str, ...]:
        present = bool(key_file) or _key_saved(view, plan.secret_name)
        body = texts.APPROVE_BODY.format(name=plan.secret_name, present=texts.APPROVE_PRESENT[present],
                                         origin=plan.origin, auth_text=texts.AUTH_TEXT[plan.auth],
                                         listing=plan.listing or "none")
        lines = [texts.ANSWERS_ENDPOINT.format(id=pid, kind=texts.KIND_LABELS[plan.kind], base_url=plan.base_url,
                                               family=plan.family),
                 *(f"  {line}" for line in body.split("\n"))]
        if key_file:
            lines.append("  " + _key_line(pid, plan.secret_name, key_file,
                                          paths.display(plan.key_file[1], runtime.environ)))
        return tuple(lines)

    def apply(rt: Any, plan: Any, confirmation: model.Confirmation) -> model.Applied:
        value = read_key_file(key_file) if key_file else None
        return providers.apply_add_endpoint(rt, plan, confirmation, value)

    return _planned_item("endpoint", pid, True, f"add {pid} (approve its route)", runtime, view,
                         lambda rt: providers.plan_add_endpoint(rt, values), describe, apply)


def _transport_item(runtime: Any, view: Any, pid: str, choice: str, key_file: str | None) -> Item:
    from claude_multi.setup import providers

    def describe(plan: Any) -> tuple[str, ...]:
        lines = [texts.ANSWERS_TRANSPORT.format(id=pid, choice=choice), *(f"  {line}" for line in plan.lines)]
        if plan.secret_name is not None:
            destination = paths.display(plan.key_file[1], runtime.environ)
            if key_file:
                lines.append("  " + _key_line(pid, plan.secret_name, key_file, destination))
            elif plan.key_present:
                lines.append("  " + texts.ANSWERS_KEY_SAVED.format(name=plan.secret_name, path=destination))
            else:
                lines.append("  " + texts.ANSWERS_KEY_MISSING.format(name=plan.secret_name, path=destination))
        return tuple(lines)

    def apply(rt: Any, plan: Any, confirmation: model.Confirmation) -> model.Applied:
        value = read_key_file(key_file) if key_file else None
        return providers.apply_transport(rt, plan, confirmation, value)

    return _planned_item("transport", pid, True, f"transport {pid} {choice}", runtime, view,
                         lambda rt: providers.plan_transport(rt, pid, choice), describe, apply)


def _key_item(runtime: Any, view: Any, pid: str, key_file: str) -> Item:
    from claude_multi.setup import providers

    def describe(plan: Any) -> tuple[str, ...]:
        lines = [_key_line(pid, plan.secret_name, key_file, plan.path_display)]
        if plan.replaces:
            lines.append("  " + texts.ANSWERS_KEY_REPLACES.format(display=plan.display, n=plan.current_length))
        if plan.replaces_shared:
            # The preview's one confirmation names every provider whose key changes.
            lines.append("  " + texts.ANSWERS_KEY_SHARED.format(name=plan.secret_name, providers=plan.key_users))
        return tuple(lines)

    return _planned_item("key", pid, True, f"set-key {pid}", runtime, view,
                         lambda rt: providers.plan_set_key(rt, pid), describe,
                         lambda rt, plan, confirmation: providers.apply_set_key(rt, plan, confirmation,
                                                                                read_key_file(key_file)))


@dataclass
class _ChoicesChain:
    """The ``choices.json`` digest the next write of this run compares with:
    the one the preview read, then the one each write of this run produced.
    Anything else that changes the choices while the run waits (a default
    chosen during the confirmation) refuses the item that would write next."""

    expected: str


def _stale_item(subject: str) -> model.Stale:
    return model.Stale(texts.ANSWERS_ITEM_WHAT.format(id=subject))


def _starter_name(runtime: Any) -> str:
    taken = set(runtime.profiles.names())
    name, n = "starter", 2
    while name in taken:
        name, n = f"starter-{n}", n + 1
    return name


def _starter_digest(plan: Any) -> str:
    """What the store holds for the starter once ``plan`` is written."""

    from claude_multi import profile as profile_mod

    return profile_mod.written_digest(profile_mod.parse({**plan.document, "name": plan.name}))


def _choices_writes(runtime: Any, *files: str) -> tuple[str, ...]:
    from claude_multi import choices

    shown = paths.display(choices.path(runtime.environ), runtime.environ)
    return (texts.ANSWERS_WRITES.format(files=", ".join((*files, shown))),)


def _keyed_providers(runtime: Any, document: Mapping[str, Any]) -> frozenset[str]:
    """The providers this document gives an API key before any profile is
    built: every entry with ``api_key_file``, and the providers whose key
    the answers' ``keys_file`` holds. A provider's key name is the one of
    the transport it will use once the document ran: a transport answer (or
    the ledger's current choice) names its reviewed alternative's key, never
    the catalog's account transport (:func:`_projected_transports`)."""

    import claude_multi.cli.commands.providers as providers_cmd

    found = {entry["id"] for entry in document.get("providers", []) if entry.get("api_key_file")}
    keys_file = document.get("keys_file")
    if isinstance(keys_file, str):
        env = runtime.gateway_environ()
        try:
            names = set(secret_store.check_key_file(secret_store.expand(keys_file, env), env))
            ctx = providers_cmd.operator_context(_projected_transports(runtime, document))
        except (secret_store.SecretStoreError, errors.ClaudeMultiError, OSError):
            names, ctx = set(), None
        if ctx is not None:
            chosen = dict(ctx.ledger.transport_choices) if ctx.ledger is not None else {}
            for pid, provider in ctx.docs["providers"]["providers"].items():
                alternative = (operator_mod.transport_alternative(pid, chosen[pid]) if pid in chosen else None)
                if alternative is not None:
                    name: Any = alternative.secret_name
                else:
                    ref = ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref")
                    name = ref.removeprefix("env:") if isinstance(ref, str) else None
                if name in names:
                    found.add(pid)
            found.update(pid for pid, own in ctx.layer.providers.items() if own.secret_name in names)
    return frozenset(found)


def _profile_item(runtime: Any, profile: str, chain: _ChoicesChain,
                  assume_keys: frozenset[str], planning: Any = None) -> tuple[Item, dict[str, str]]:
    """The ``profile`` answer, planned now: a starter's name, slots, spend
    notes and files (built from the providers connected now plus those this
    document connects with keys), or the profile named; either becomes the
    default. When it runs, a starter is built again from what is connected
    then and written only while it equals the preview and the choices are
    still as previewed. ``planning`` is the runtime the preview is planned
    with (:func:`_projected_transports`; default ``runtime``). Also returns
    ``{name: digest}`` of the profile it writes, for a ``default_profile``
    naming it."""

    from claude_multi.setup import defaults, profiles as setup_profiles

    if profile == "starter":
        try:
            name = _starter_name(runtime)
            starter = setup_profiles.plan_new_starter(planning if planning is not None else runtime, name,
                                                      assume_keys=assume_keys)
            written = _starter_digest(starter)
            default = defaults.planned_default_plan(runtime, name, written)
        except (model.SetupError, errors.ClaudeMultiError, OSError, ValueError) as exc:
            return _failing("profile", "profile", False, (texts.ANSWERS_STARTER,), _problem(exc)), {}
        shown = reviewed(starter)

        def run_starter() -> model.Applied:
            from claude_multi import choices

            if choices.digest(runtime.environ) != chain.expected:
                raise _stale_item("profile")
            try:
                fresh = setup_profiles.plan_new_starter(runtime, name)
            except model.Refused as exc:  # nothing can be built from what is connected now
                raise _stale_item("profile") from exc
            if reviewed(fresh) != shown:
                raise _stale_item("profile")
            setup_profiles.apply_new_starter(runtime, fresh, model.Confirmation.given(fresh))
            try:
                chain.expected = defaults.apply_default(runtime, default, model.Confirmation.given(default),
                                                        expected_choices=chain.expected)
            except model.Stale as exc:
                raise model.Refused(texts.ANSWERS_STARTER_DEFAULT_STALE.format(name=name)) from exc
            return model.Applied("profile", name, "not-needed", (texts.SETUP_STARTER_SAVED.format(name=name),))

        lines = (texts.ANSWERS_STARTER_PLAN.format(name=name), *(f"  {line}" for line in starter.lines),
                 *_choices_writes(runtime, *starter.writes))
        return Item("profile", name, False, lines, run_starter), {name: written}
    try:
        chosen = defaults.set_default_plan(runtime, profile)
    except (model.SetupError, errors.ClaudeMultiError, OSError) as exc:
        return _failing("profile", profile, False, (texts.ANSWERS_PROFILE.format(name=profile),), _problem(exc)), {}

    def run() -> model.Applied:
        try:
            chain.expected = defaults.apply_default(runtime, chosen, model.Confirmation.given(chosen),
                                                    expected_choices=chain.expected)
        except model.Stale as exc:
            raise _stale_item("profile") from exc
        return model.Applied("profile", profile, "not-needed", chosen.lines)

    lines = (texts.ANSWERS_PROFILE.format(name=profile), *(f"  {line}" for line in chosen.lines[1:]),
             *_choices_writes(runtime))
    return Item("profile", profile, False, lines, run), {}


def _default_item(runtime: Any, name: str | None, chain: _ChoicesChain, planned: Mapping[str, str]) -> Item:
    """The ``default_profile`` answer, planned now (with the choices it
    read); it runs only while the choices are still those (or what an
    earlier item of this run wrote), so a default chosen elsewhere while
    the run waits is kept and the item refuses as stale."""

    from claude_multi.setup import defaults

    head = texts.ANSWERS_DEFAULT.format(name=name or "automatic")
    try:
        if name is not None and name in planned:
            plan = defaults.planned_default_plan(runtime, name, planned[name])
        else:
            plan = defaults.set_default_plan(runtime, name)
    except (model.SetupError, errors.ClaudeMultiError, OSError) as exc:
        return _failing("default", "default_profile", False, (head,), _problem(exc))

    def run() -> model.Applied:
        try:
            chain.expected = defaults.apply_default(runtime, plan, model.Confirmation.given(plan),
                                                    expected_choices=chain.expected)
        except model.Stale as exc:
            raise _stale_item("default_profile") from exc
        return model.Applied("default", str(name), "not-needed", plan.lines)

    lines = (head, *(f"  {line}" for line in plan.lines[1:]), *_choices_writes(runtime))
    return Item("default", "default_profile", False, lines, run)


# ------------------------------------------------------------------ items that depend on each other


def _known_providers(runtime: Any) -> frozenset[str]:
    import claude_multi.cli.commands.providers as providers_cmd

    try:
        ctx = providers_cmd.operator_context(runtime)
    except (errors.ClaudeMultiError, OSError):
        return frozenset()
    return frozenset(ctx.docs["providers"]["providers"]) | frozenset(ctx.layer.providers)


def check_dependencies(runtime: Any, document: Mapping[str, Any]) -> None:
    """Refuse (:class:`AnswersError`, before anything is shown) a document
    with an item that needs a provider only another item of the same
    document declares: every item is planned before any runs, so it would
    be planned without that provider and fail. The declaration's own
    ``api_key_file`` is the one way to give such a provider its key."""

    entries = document.get("providers", [])
    declared: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if "endpoint" in entry or "server" in entry:
            declared.setdefault(entry["id"], index)
    if not declared:
        return
    known = _known_providers(runtime)
    for index, entry in enumerate(entries):
        pid = entry["id"]
        first = declared.get(pid)
        if first is None or pid in known:
            continue
        if index == first:
            if "enabled" in entry:
                raise AnswersError(texts.ANSWERS_DEPENDS_TOGGLE.format(index=index, id=pid))
            continue
        declaration = entries[first]
        only_key = ("api_key_file" in entry and not entry.get("transport") and "enabled" not in entry
                    and "endpoint" not in entry and "server" not in entry)
        if only_key and "server" in declaration:
            raise AnswersError(texts.ANSWERS_DEPENDS_SERVER_KEY.format(index=index, id=pid, first=first))
        if only_key:
            raise AnswersError(texts.ANSWERS_DEPENDS_KEY.format(index=index, id=pid, first=first))
        raise AnswersError(texts.ANSWERS_DEPENDS.format(index=index, id=pid, first=first))


def build_items(runtime: Any, document: Mapping[str, Any], *,
                progress: Callable[[str], None] = lambda _line: None) -> list[Item]:
    """The items of an answers document, in the order they run, each with
    its preview made from its plan now (nothing is written). A document
    whose items depend on each other is refused first
    (:func:`check_dependencies`)."""

    from claude_multi import choices

    check_dependencies(runtime, document)
    items: list[Item] = []
    claude = document.get("claude")
    if isinstance(claude, Mapping):
        items.append(_claude_item(runtime, claude, progress))
    gateway = document.get("gateway")
    if isinstance(gateway, Mapping) and gateway.get("start"):
        items.append(_gateway_item(runtime))
    keys_file = document.get("keys_file")
    if isinstance(keys_file, str):
        items.append(_keys_file_item(runtime, keys_file))
    view = _planning_runtime(runtime, document)
    for entry in document.get("providers", []):
        pid = entry["id"]
        if "enabled" in entry:
            items.append(_toggle_item(runtime, pid, bool(entry["enabled"])))
        if "server" in entry:
            items.append(_server_item(runtime, pid, entry["server"]))
        elif "endpoint" in entry:
            items.append(_endpoint_item(runtime, view, pid, entry["endpoint"], entry.get("api_key_file")))
        elif entry.get("transport"):
            items.append(_transport_item(runtime, view, pid, entry["transport"], entry.get("api_key_file")))
        elif entry.get("api_key_file"):
            items.append(_key_item(runtime, view, pid, entry["api_key_file"]))
    chain = _ChoicesChain(choices.digest(runtime.environ))
    planned: dict[str, str] = {}
    profile = document.get("profile")
    if isinstance(profile, str) and profile != "auto":
        item, planned = _profile_item(runtime, profile, chain, _keyed_providers(runtime, document),
                                      _projected_transports(runtime, document))
        items.append(item)
    if "default_profile" in document:
        items.append(_default_item(runtime, document["default_profile"], chain, planned))
    return items
