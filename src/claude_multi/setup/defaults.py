"""The default profile, the ready-first order of profiles and the spend notes.

A launch that names no profile uses, in order: this directory's most
recently used profile (always, ready or not); the default profile the person
chose, when it is connected here; the most recently used connected profile;
the first connected shipped profile; the first connected profile of the
person's own; else the shipped default with a notice. "Connected" is local
readiness: every bound model is configured and served here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from claude_multi import account_pools, catalog, choices, errors, profile as profile_mod, readiness
from claude_multi.setup import model

# Notices (the full-screen form; ``notice_line`` swaps the key paths for commands).
DEFAULT_NOT_READY = "! your default profile {name} is not connected here ({why}); using {used} — P picks another"
DEFAULT_MISSING = "! your default profile {name} no longer exists; using {used} — P → D sets another"
DEFAULT_UNLOADABLE = "! your default profile {name} cannot be loaded; using {used} — P shows why"
NONE_CONNECTED = "! nothing is connected yet — W opens Get started"
NONE_READY = ("! no profile is connected here ({why}); using {used} — P picks another, "
              "or P → N builds one from your connected providers")
LINE_FORMS = {
    "P picks another": "claude-multi --profile NAME",
    "W opens Get started": "claude-multi setup",
    "P → D sets another": "claude-multi profile default NAME",
    "P → N builds one from your connected providers": "claude-multi profile starter --apply",
    "P shows why": "claude-multi profile list",
}

# Why a profile was chosen.
REASONS = ("here", "default", "recent", "seed", "yours", "fallback")
REASON_TEXT = {
    "here": "this directory's most recent",
    "default": "your default",
    "recent": "most recently used",
    "seed": "first connected shipped profile",
    "yours": "first connected profile of yours",
    "fallback": "nothing is connected",
}

# ``claude-multi profile default`` and its plan.
DEFAULT_SHOW_SET = "default profile: {name} (set by you)"
DEFAULT_SHOW_AUTO = "default profile: automatic"
DEFAULT_SHOW_HERE = "here: {resolved} ({reason_text})"
DEFAULT_SET_LINE = "Default profile: {name}."
DEFAULT_SET_NOTE = "note: {name} is not connected here ({why}); it is used wherever it is connected."
DEFAULT_CLEARED = "The default profile is automatic again."
DEFAULT_UNKNOWN = "profile {name!r} does not exist — claude-multi profile list"
DEFAULT_UNLOADABLE_REFUSAL = "{error} — fix it first: claude-multi profile edit {name} (or rm; a copy is kept)"
# The fix of a profile file that cannot be loaded.
UNLOADABLE_FIX = "claude-multi profile edit {name} (or rm; a copy is kept)"

# Pay-per-token providers that back several slots of a lineup.
SPEND_NOTE = "{who} bill per token to {display} — set a spend limit in your {display} account"

# The short readiness texts of a profile (``needs …``).
ACCOUNT_NAMES = {name: pool.display for name, pool in account_pools.pools().items()}
STATE_CONNECTED = "connected"
STATE_INVALID = "invalid"
STATE_UNLOADABLE = "cannot load"
STATE_UNCHECKED = "not checked"
NEEDS = "needs {what}"

_DISABLED = re.compile(r"provider '([^']+)' is disabled in Settings")
_ROUTE = re.compile(r"the route of provider (\S+) is (?:unapproved|changed)")


@dataclass(frozen=True)
class ProfileReadiness:
    """A profile's readiness in short: ``state_text`` for lists, ``reason``
    (a key of the Profiles reasons) and the fields its line needs."""

    ready: bool
    state_text: str
    reason: str
    fields: Mapping[str, str] = field(default_factory=dict)


def _display(providers: Mapping[str, Any], provider_id: str) -> str:
    provider = providers.get(provider_id)
    display = provider.get("display") if isinstance(provider, Mapping) else None
    return str(display) if isinstance(display, str) and display else provider_id


def summarize(rows: Sequence[readiness.SlotReadiness], *, lines: Mapping[str, Any],
              providers: Mapping[str, Any]) -> ProfileReadiness:
    """The short form of a profile's per-slot readiness: the first fix it needs.

    ``lines`` maps line keys to their entries (for the provider of a slot),
    ``providers`` the effective providers (display names, account pools).
    """

    if readiness.satisfiable(rows):
        return ProfileReadiness(True, STATE_CONNECTED, "connected")
    bound = [row for row in rows if row.state not in (readiness.READY, readiness.UNBOUND)]
    blocked = [row for row in bound if any(reason.blocking for reason in row.reasons)]
    row = (blocked or bound or [None])[0]
    if row is None or row.first is None:
        return ProfileReadiness(False, STATE_INVALID, "invalid", {"first_error": "no model is bound"})
    reason = next((r for r in row.reasons if r.blocking), row.first)
    entry = lines.get(row.key) if row.key is not None else None
    provider_id = str(entry.get("provider")) if isinstance(entry, Mapping) else ""
    if reason.code == "evaluation":
        disabled = _DISABLED.search(reason.text)
        if disabled is not None:
            display = _display(providers, disabled.group(1))
            return ProfileReadiness(False, NEEDS.format(what=f"{display} on"), "needs-on", {"display": display})
        route = _ROUTE.search(reason.text)
        if route is not None:
            return ProfileReadiness(False, NEEDS.format(what="approval"), "needs-approval", {"id": route.group(1)})
        return ProfileReadiness(False, STATE_INVALID, "invalid", {"first_error": reason.text})
    if not reason.blocking:
        return ProfileReadiness(False, STATE_UNCHECKED, "unchecked",
                                {"text": reason.text, "remedy": reason.remedy})
    if reason.code == "credential":
        found = re.search(r" \(([a-z0-9][a-z0-9-]*)\): ", reason.text)
        display = _display(providers, found.group(1) if found else provider_id)
        return ProfileReadiness(False, NEEDS.format(what=f"{display} key"), "needs-key", {"display": display})
    if reason.code == "oauth-records":
        provider = providers.get(provider_id)
        transport = provider.get("transport") if isinstance(provider, Mapping) else None
        pool = transport.get("pool") if isinstance(transport, Mapping) else None
        kind = ACCOUNT_NAMES.get(str(pool), f"{pool} account")
        return ProfileReadiness(False, NEEDS.format(what=kind), "needs-signin", {"kind": kind})
    if reason.code == "served":
        return ProfileReadiness(False, NEEDS.format(what="apply"), "needs-apply")
    if reason.code == "lan":
        return ProfileReadiness(False, NEEDS.format(what=f"{provider_id} reachable"), "needs-reachable",
                                {"id": provider_id})
    return ProfileReadiness(False, STATE_INVALID, "invalid", {"first_error": reason.text})


def summarize_for(runtime: Any, rows: Sequence[readiness.SlotReadiness]) -> ProfileReadiness:
    try:
        lines, providers = runtime.lineup_catalog().lines, runtime.readiness_providers()
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
        lines, providers = {}, {}
    return summarize(rows, lines=lines, providers=providers)


def line_form(text: str | None) -> str | None:
    """A notice with each full-screen key path replaced by its command."""

    if text is None:
        return None
    for key, command in LINE_FORMS.items():
        text = text.replace(key, command)
    return text


# ------------------------------------------------------------------ resolution


@dataclass(frozen=True)
class DefaultChoice:
    """The profile a launch uses when none is named, and why."""

    name: str
    reason: str  # here | default | recent | seed | yours | fallback
    ready: bool
    notice: str | None  # full-screen form (a card warning row)
    notice_line: str | None  # line-mode form

    @property
    def reason_text(self) -> str:
        return REASON_TEXT[self.reason]


def chosen_default(runtime: Any) -> str | None:
    """The default profile the person chose (None: automatic, or unreadable choices)."""

    try:
        value = choices.read(runtime.environ).get("default_profile")
    except choices.ChoicesError:
        return None
    return value if isinstance(value, str) else None


def profile_verdicts(runtime: Any, names: Iterable[str]) -> dict[str, tuple[readiness.SlotReadiness, ...]]:
    """``Runtime.profile_readiness``; a setup that cannot be read (corrupt
    Settings, an unreadable catalog view) leaves every profile not connected
    rather than failing the caller."""

    try:
        return runtime.profile_readiness(list(names))
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
        return {}


def split_loadable(store: profile_mod.ProfileStore, names: Iterable[str]) -> tuple[list[str], dict[str, profile_mod.ProfileError]]:
    """Names that load, and the load error of every other one."""

    loadable: list[str] = []
    failed: dict[str, profile_mod.ProfileError] = {}
    for name in names:
        try:
            store.load(name)
        except profile_mod.ProfileError as exc:
            failed[name] = exc
            continue
        loadable.append(name)
    return loadable, failed


def _recency(runtime: Any) -> dict[str, str]:
    from claude_multi.cli import selection

    here, elsewhere = selection._profile_recency(runtime)
    stamps = dict(elsewhere)
    for name, stamp in here.items():
        if stamp > stamps.get(name, ""):
            stamps[name] = stamp
    return stamps


def _choice(name: str, reason: str, ready: bool, notice: str | None = None) -> DefaultChoice:
    return DefaultChoice(name, reason, ready, notice, line_form(notice))


def _nothing_connected(runtime: Any) -> bool:
    """No provider has an API key, a sign-in or a reachable keyless server
    (the setup layer's connections; an observation that fails counts as
    something connected, so the notice never claims otherwise)."""

    from claude_multi.setup import status

    try:
        return not status.connected(runtime)
    except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
        return False


def resolve_default(runtime: Any) -> DefaultChoice:
    """The profile a launch uses when none is named (see the module docstring).

    Nothing is written and no request goes upstream; every readiness fact
    comes from one local observation refresh."""

    from claude_multi.cli import selection

    store = runtime.profiles
    here = selection._cwd_recent_profile(runtime)
    if here is not None:
        rows = profile_verdicts(runtime, [here]).get(here, ())
        return _choice(here, "here", readiness.satisfiable(rows))
    names = store.names()
    loadable, failed = split_loadable(store, names)
    verdicts = profile_verdicts(runtime, loadable)
    ready = {name: readiness.satisfiable(rows) for name, rows in verdicts.items()}
    # The notice about a skipped default: its template and fields stay apart
    # until the profile used is known, then it is formatted once (a field
    # such as a provider's display name may hold braces).
    skipped: tuple[str, dict[str, str]] | None = None
    chosen = chosen_default(runtime)
    if chosen is not None:
        if not store.contains(chosen):
            skipped = DEFAULT_MISSING, {"name": chosen}
        elif chosen in failed:
            skipped = DEFAULT_UNLOADABLE, {"name": chosen}
        elif ready.get(chosen):
            return _choice(chosen, "default", True)
        else:
            why = summarize_for(runtime, verdicts.get(chosen, ())).state_text
            skipped = DEFAULT_NOT_READY, {"name": chosen, "why": why}

    def skipped_notice(used: str) -> str | None:
        if skipped is None:
            return None
        template, fields = skipped
        return template.format(**fields, used=used)

    def won(name: str, reason: str) -> DefaultChoice:
        return _choice(name, reason, True, skipped_notice(name))

    stamps = _recency(runtime)
    for name in sorted(sorted(stamps), key=lambda n: stamps[n], reverse=True):
        if ready.get(name):
            return won(name, "recent")
    for name in readiness.SEED_ORDER:
        if store.is_seed(name) and ready.get(name):
            return won(name, "seed")
    for name in sorted(n for n in loadable if not store.is_seed(n)):
        if ready.get(name):
            return won(name, "yours")
    used = catalog.DEFAULT_SEED
    if skipped is not None:
        return _choice(used, "fallback", False, skipped_notice(used))
    if _nothing_connected(runtime):
        return _choice(used, "fallback", False, NONE_CONNECTED)
    if used in verdicts:
        why = summarize_for(runtime, verdicts[used]).state_text
    else:
        why = STATE_UNLOADABLE if used in failed else STATE_UNCHECKED
    return _choice(used, "fallback", False, NONE_READY.format(why=why, used=used))


# ------------------------------------------------------------------ ordering


@dataclass(frozen=True)
class ProfileChoice:
    name: str
    loadable: bool
    ready: bool | None  # None when the profile cannot be loaded
    last_used: str | None
    is_default: bool


def profile_choices(runtime: Any, *, current: str | None = None,
                    verdicts: Mapping[str, Sequence[readiness.SlotReadiness]] | None = None) -> list[ProfileChoice]:
    """Every profile, connected ones first, then the others, then those that
    cannot be loaded (by name); within a group this directory's most
    recently used first, then those used elsewhere, then by name. ``current``
    goes first. ``verdicts`` reuses a readiness refresh the caller made."""

    from claude_multi.cli import selection

    store = runtime.profiles
    names = store.names()
    loadable, failed = split_loadable(store, names)
    if verdicts is None:
        verdicts = profile_verdicts(runtime, loadable)
    here, elsewhere = selection._profile_recency(runtime)
    order = selection._pick_order_from(names, here, elsewhere)
    default = chosen_default(runtime)

    def last(name: str) -> str | None:
        stamp = max(here.get(name, ""), elsewhere.get(name, ""))
        return stamp or None

    def item(name: str) -> ProfileChoice:
        if name in failed:
            return ProfileChoice(name, False, None, last(name), name == default)
        return ProfileChoice(name, True, readiness.satisfiable(verdicts.get(name, ())), last(name),
                             name == default)

    items = [item(name) for name in order]
    ordered = ([c for c in items if c.loadable and c.ready] + [c for c in items if c.loadable and not c.ready]
               + sorted((c for c in items if not c.loadable), key=lambda c: c.name))
    if current is not None:
        first = [c for c in ordered if c.name == current]
        ordered = first + [c for c in ordered if c.name != current]
    return ordered


# ------------------------------------------------------------------ the default choice


@dataclass(frozen=True)
class DefaultPlan(model.Plan):
    name: str | None  # None: automatic
    ready: bool | None
    why: str | None
    choices_digest: str = "absent"  # the choices document the plan read
    profile_digest: str | None = None  # the profile it names, as read


def set_default_plan(runtime: Any, name: str | None) -> DefaultPlan:
    """Make ``name`` the default profile (None: automatic again). Refused for
    a name that does not exist or cannot be loaded."""

    store = runtime.profiles
    ready: bool | None = None
    why: str | None = None
    profile_digest: str | None = None
    if name is None:
        lines: tuple[str, ...] = (DEFAULT_CLEARED,)
    else:
        try:
            exists = store.contains(name)
        except profile_mod.ProfileError as exc:
            raise model.Refused(str(exc)) from exc
        if not exists:
            raise model.Refused(DEFAULT_UNKNOWN.format(name=name), remedy="claude-multi profile list")
        try:
            store.load(name)
        except profile_mod.ProfileError as exc:
            raise model.Refused(DEFAULT_UNLOADABLE_REFUSAL.format(error=exc, name=name),
                                remedy=f"claude-multi profile edit {name}") from exc
        profile_digest = store.digest(name)
        rows = profile_verdicts(runtime, [name]).get(name, ())
        summary = summarize_for(runtime, rows)
        ready, why = summary.ready, None if summary.ready else summary.state_text
        lines = (DEFAULT_SET_LINE.format(name=name),)
        if not ready:
            lines += (DEFAULT_SET_NOTE.format(name=name, why=why),)
    choices_digest = choices.digest(runtime.environ)
    digest = model.digest_of("default", name, choices_digest, profile_digest)
    return DefaultPlan(op="default", subject=name or "automatic", lines=lines, consent=None, guarded=False,
                       digest=digest, writes=(str(choices.path(runtime.environ)),),
                       name=name, ready=ready, why=why, choices_digest=choices_digest,
                       profile_digest=profile_digest)


def planned_default_plan(runtime: Any, name: str, profile_digest: str) -> DefaultPlan:
    """Make ``name`` the default once it holds ``profile_digest``: the plan
    for a profile the same run writes first (a starter planned with it).
    Applying it refuses unless the profile then holds exactly those bytes."""

    choices_digest = choices.digest(runtime.environ)
    digest = model.digest_of("default", name, choices_digest, profile_digest)
    return DefaultPlan(op="default", subject=name, lines=(DEFAULT_SET_LINE.format(name=name),), consent=None,
                       guarded=False, digest=digest, writes=(str(choices.path(runtime.environ)),),
                       name=name, ready=None, why=None, choices_digest=choices_digest,
                       profile_digest=profile_digest)


def apply_default(runtime: Any, plan: DefaultPlan, confirmation: model.Confirmation, *,
                  expected_choices: str | None = None) -> str:
    """Store the choice; refuses when the choices or the profile changed
    since the plan (the choices are compared under their own lock, in the
    same step as the write). ``expected_choices`` replaces the choices
    digest the plan read when a write of the same run came in between (the
    digest that write produced). Returns the digest of what it wrote."""

    model.check_apply(runtime, plan, confirmation, verb="profile default")
    profile_digest = runtime.profiles.digest(plan.name) if plan.name is not None else None
    if profile_digest != plan.profile_digest:
        raise model.Stale("your profiles or choices")
    expected = expected_choices if expected_choices is not None else plan.choices_digest
    try:
        written = choices.update(runtime.environ, expected=expected, default_profile=plan.name)
    except choices.ChoicesChanged as exc:
        raise model.Stale("your profiles or choices") from exc
    except choices.ChoicesError as exc:
        raise model.Refused(str(exc), remedy=exc.remedy) from exc
    return choices.written_digest(written)


def move_default(runtime: Any, old: str, new: str | None) -> bool:
    """After a rename (``new``) or a removal (None): keep the default
    pointing at the same profile. True when the default changed; it changes
    only while it still names ``old`` (decided under the choices lock), and
    unreadable choices are left as they are."""

    try:
        return choices.replace(runtime.environ, "default_profile", old, new)
    except choices.ChoicesError:
        return False


def seed_not_in_use(runtime: Any, name: str, lineup: profile_mod.ResolvedLineup,
                    missing: set[str] | frozenset[str]) -> bool:
    """A shipped profile none of whose providers is set up here (``missing``:
    the providers without a credential) and that is not the chosen default:
    it is simply not connected, so its missing credentials are no gap to fix."""

    if not runtime.profiles.is_seed(name) or chosen_default(runtime) == name:
        return False
    bound = {lineup.lead.binding.provider, *(agent.binding.provider for agent in lineup.agents.values())}
    return bound <= set(missing)


# ------------------------------------------------------------------ spend notes


def pay_per_token(provider: Mapping[str, Any]) -> bool:
    """A keyed transport bills per token (a catalog or own provider with an
    API key, the Anthropic API key when selected); an account sign-in or a
    keyless server does not."""

    transport = provider.get("transport") if isinstance(provider, Mapping) else None
    if not isinstance(transport, Mapping) or transport.get("kind") == "oauth-pool":
        return False
    auth = transport.get("auth")
    return isinstance(auth, Mapping) and auth.get("kind") not in (None, "none")


def _plural(n: int) -> str:
    return f"{n} agent" if n == 1 else f"{n} agents"


def spend_notes(runtime: Any, lineup: profile_mod.ResolvedLineup) -> tuple[str, ...]:
    """One note per pay-per-token provider that backs at least two slots of
    ``lineup`` (the lead counts as one)."""

    providers = runtime.readiness_providers()
    lead_provider = lineup.lead.binding.provider
    agents: dict[str, int] = {}
    for agent in lineup.agents.values():
        agents[agent.binding.provider] = agents.get(agent.binding.provider, 0) + 1
    notes: list[str] = []
    for provider_id in sorted({lead_provider, *agents}):
        provider = providers.get(provider_id)
        if not isinstance(provider, Mapping) or not pay_per_token(provider):
            continue
        lead = provider_id == lead_provider
        count = agents.get(provider_id, 0)
        if count + (1 if lead else 0) < 2:
            continue
        if lead and count:
            who = f"lead + {_plural(count)}"
        elif lead:
            who = "the lead"
        else:
            who = _plural(count)
        display = _display(providers, provider_id)
        notes.append(SPEND_NOTE.format(who=who, display=display))
    return tuple(notes)
