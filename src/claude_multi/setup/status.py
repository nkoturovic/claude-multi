"""Where setup stands, derived from real state each time (there is no
progress file): the steps of Get started and ``claude-multi setup``, and
each provider's connection.

A snapshot never writes and never calls a provider; it makes at most one
loopback health read and one ``/v1/models`` read, through the runtime's seams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from claude_multi import errors, operator as operator_mod
from claude_multi.setup import texts

STEP_ORDER = ("preflight", "claude", "gateway", "providers", "test", "profile", "check")
CONNECTED = "connected"
CONNECTION_STATES = ("connected", "key-missing", "key-invalid", "not-signed-in", "sign-in-unknown",
                     "route-unapproved", "route-changed", "off")


@dataclass(frozen=True)
class Connection:
    provider_id: str
    display: str
    origin: str  # catalog | yours
    kind: str  # api-key | account | keyless
    state: str  # one of CONNECTION_STATES
    credential: str | None  # the secret NAME or the account pool (names only)
    accounts: tuple[str, ...]
    lines: int
    served: str  # "3/3", "0/2", "unknown", "—"
    pay_per_token: bool
    transport: str | None  # an account provider: account | api-key; else None

    @property
    def label(self) -> str:
        """``OpenRouter (API key)`` or ``Claude account``."""

        if self.kind == "account":
            return texts.ACCOUNT_KINDS.get(str(self.credential), f"{self.display} account")
        if self.kind == "keyless":
            return self.display
        return f"{self.display} (API key)"


def _served(note: str) -> str:
    if note.endswith(" served"):
        return note[: -len(" served")]
    if note.startswith("unknown"):
        return "unknown"
    return note


def connections(runtime: Any, *, facts: Sequence[Mapping[str, Any]] | None = None) -> tuple[Connection, ...]:
    """Every provider's connection, from the provider facts and the
    providers you added (names, counts and states only). A caller that
    already read the provider facts passes them (one gateway observation)."""

    import claude_multi.cli.gateway_facts as gateway_facts
    from claude_multi.setup import defaults, signin

    if facts is None:
        try:
            facts = gateway_facts._provider_facts(runtime).facts
        except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
            return ()
    try:
        observed = gateway_facts.oauth_record_observation(runtime)
    except (OSError, KeyError, ValueError):
        observed = None
    try:
        snapshot = runtime.operator_snapshot()
        layer, ledger = snapshot.layer, snapshot.ledger
    except (errors.ClaudeMultiError, OSError, ValueError):
        layer, ledger = None, None
    choices = ledger.transport_choices if ledger is not None else {}
    try:
        providers = runtime.effective_transport_docs()["providers"]["providers"]
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
        providers = {}
    result: list[Connection] = []
    for fact in facts:
        pid = fact["id"]
        origin = "yours" if fact.get("source") in ("operator", "custom") else "catalog"
        provider = providers.get(pid) or {}
        transport = None
        accounts: tuple[str, ...] = ()
        pay = bool(provider) and defaults.pay_per_token(provider)
        credential: str | None
        if pid in signin.ACCOUNT_POOLS:
            choice = choices.get(pid) or operator_mod.TRANSPORT_POOL
            transport = "api-key" if choice == operator_mod.TRANSPORT_API_KEY else "account"
            # Saved accounts are listed whichever transport is active: they
            # survive a switch to the API key and serve again after one back.
            try:
                accounts = signin.accounts(runtime, pid)
            except errors.ClaudeMultiError:
                accounts = ()
        if fact["kind"] == "oauth-pool":
            pool = str(fact.get("pool"))
            kind, credential = "account", pool
            if observed is None:
                state = "sign-in-unknown"
            elif observed.get(pool, 0) == 0:
                state = "not-signed-in"
            else:
                state = CONNECTED
        elif fact.get("auth") == "none" or fact["credential"] == "keyless LAN":
            kind, credential, state = "keyless", None, CONNECTED
        else:
            kind = "api-key"
            text = str(fact["credential"])
            credential = text.split(" ", 1)[0] if " " in text else None
            route = fact.get("route")
            selection = operator_mod.transport_selections(runtime.catalog.docs, ledger).get(pid) if ledger else None
            if route in ("unapproved", "changed"):
                state = f"route-{route}"
            elif selection is not None and not selection.approved:
                state = "route-unapproved"
            elif text.endswith(" missing"):
                state = "key-missing"
            elif text.endswith(" present"):
                state = CONNECTED
            else:
                state = "key-invalid"
        if not fact.get("enabled", True):
            state = "off"
        result.append(Connection(pid, str(fact["display"]), origin, kind, state, credential, accounts,
                                 int(fact.get("models", 0)), _served(str(fact.get("served_note", "—"))), pay,
                                 transport))
    return tuple(result)


def connected(runtime: Any) -> tuple[Connection, ...]:
    return tuple(item for item in connections(runtime) if item.state == CONNECTED)


def without_models(runtime: Any, provider_ids: Sequence[str]) -> tuple[str, ...]:
    """The providers of ``provider_ids`` that have no model yet (a profile
    can use one only after a model of it is added and admitted)."""

    try:
        lines = runtime.lineup_catalog().lines
    except (errors.ClaudeMultiError, OSError, ValueError):
        return ()
    used = {entry.get("provider") for entry in lines.values()}
    return tuple(pid for pid in provider_ids if pid not in used)


@dataclass(frozen=True)
class Step:
    id: str
    state: str  # done | todo | blocked | waiting | optional
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> bool:
        return self.id not in ("test",)


@dataclass(frozen=True)
class SetupState:
    steps: tuple[Step, ...]
    connections: tuple[Connection, ...]
    default: Any  # setup.defaults.DefaultChoice
    in_session: bool
    writable: bool
    checks: tuple[Any, ...] = ()

    def step(self, step_id: str) -> Step:
        return next(step for step in self.steps if step.id == step_id)

    @property
    def first_unfinished(self) -> str | None:
        return next((step.id for step in self.steps if step.state in ("todo", "setup", "blocked")), None)

    @property
    def ready(self) -> bool:
        """Every required step done (the gateway may still be waiting to start)."""

        for step in self.steps:
            if step.id == "test":
                continue
            if step.id == "gateway":
                if step.state == "blocked":
                    return False
                continue
            if step.state != "done":
                return False
        return True


def snapshot(runtime: Any) -> SetupState:
    from claude_multi.cli import consent
    from claude_multi.setup import defaults, external, firstrun

    conns = connections(runtime)
    links = tuple(item for item in conns if item.state == CONNECTED)
    # One size-and-sha256 check of the owned copy serves the Claude step and
    # the first-run checks alike.
    claude = external.claude_verified(runtime)
    checks = firstrun.checks(runtime, connections=conns, claude=claude)
    steps: list[Step] = []
    failed = [check for check in checks[:3] if check.state == "fail"]
    steps.append(Step("preflight", "blocked" if failed else "done", {"n": len(failed)}))
    if claude.state == "verified":
        steps.append(Step("claude", "done", {"version": claude.version}))
    elif claude.state == "missing":
        steps.append(Step("claude", "todo", {"version": claude.version}))
    else:
        steps.append(Step("claude", "blocked", {"version": claude.version, "problem": claude.detail}))
    gateway = external.gateway_status(runtime)
    steps.append(Step("gateway", {"running": "done"}.get(gateway.state, gateway.state),
                      {"problem": gateway.detail, "port": gateway.port}))
    steps.append(Step("providers", "done" if links else "todo",
                      {"connected": " · ".join(item.label for item in links)}))
    steps.append(Step("test", "optional"))
    try:
        default = defaults.resolve_default(runtime)
    except (errors.ClaudeMultiError, OSError, ValueError):
        default = None
    if not links:
        steps.append(Step("profile", "waiting"))
    elif default is not None and default.ready:
        steps.append(Step("profile", "done", {"name": default.name, "why": default.reason_text}))
    else:
        steps.append(Step("profile", "todo"))
    required_fail = [check for check in checks if check.required and check.state in ("fail", "waiting")]
    steps.append(Step("check", "todo" if required_fail else "done", {"n": len(required_fail)}))
    return SetupState(tuple(steps), conns, default, consent.session_marker(runtime.environ) is not None,
                      bool(getattr(runtime, "allow_state_writes", False)), checks)


def should_open(runtime: Any, *, explicit: bool, resume: bool = False) -> str | None:
    """Which Get started step a launch card opens on its own, or None:
    never for an explicit target or a resume, inside a Claude session, or
    in a read-only run; else the Claude step while the copy is missing,
    else the providers step while nothing is connected."""

    from claude_multi.cli import consent
    from claude_multi.setup import external

    if explicit or resume or consent.session_marker(runtime.environ) is not None:
        return None
    if not getattr(runtime, "allow_state_writes", False):
        return None
    if external.claude_status(runtime).state != "verified":
        return "claude"
    if not connected(runtime):
        return "providers"
    return None


def status_lines(state: SetupState) -> list[str]:
    """The ``claude-multi setup --status`` table."""

    lines = []
    for number, step in enumerate(state.steps, start=1):
        template = texts.STATUS_TEXT.get((step.id, step.state), "")
        fields = {"n": 0, "version": "", "problem": "", "connected": "", "name": "", "why": ""}
        fields.update({key: value for key, value in step.fields.items() if value is not None})
        text = template.format(**fields)
        mark = texts.STATUS_MARKS.get(step.state, "·")
        if step.id in texts.STATUS_MISSING and step.state == "todo":
            mark = texts.STATUS_MISSING[step.id]
        lines.append(texts.STATUS_ROW.format(n=number, step=step.id, mark=mark, text=text).rstrip())
    return lines
