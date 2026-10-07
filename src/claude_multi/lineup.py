"""`claude-multi lineup` and propagation.

The pure rendering helpers come first: the one launch path shows the
one-time profile diff and the line-mode confirm with them; ``cat`` there is
a ``profile.LineupCatalog`` (merged catalog ∪ custom).

The rest of the module:

- :func:`parse_cli` / :func:`parse_request` — the ``/cm`` argv contract and
  the request grammar;
- :func:`apply` — one request for one session: phase A runs
  under the shared migration hold and the record's lifecycle lock and
  either shows, writes a record-only change, applies live (agent files →
  ``lineup.md`` → ``lineup.gen`` → lineup log → record, never
  ``settings.json``/``lead-set.json``), records a ``pending`` relaunch
  change, or samples the record and releases every lock before the
  caller's relaunch executor runs;
- :func:`classify` — LIVE vs RELAUNCH;
- :func:`followers`, :func:`profiles_for_binding`, :func:`on_saved`,
  :func:`on_removed` — propagation, deciding membership under each
  record's lock.

This module never imports ``cli`` (the relaunch executor is a callback).
"""

from __future__ import annotations

import copy
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

from . import catalog as catalog_mod
from . import compiler, errors, hooks, lineup_files, lineup_log, profile, scope, sessions, state, strict_json
from . import settings as settings_mod

UNBOUND = "(unbound)"
# The one /cm grammar line, from the verb table (``lineup_files.CM_VERBS``).
HELP_LINE = lineup_files.cm_help_line()
# Appended after every HELP_LINE (the /model behaviour is documented in /cm help).
# The second line is byte-identical to the /model line of cli.QUICK_HELP and
# cli.DIRECT_HELP.
MODEL_HELP_LINES = (
    "/model: press s to switch for this session only — Enter saves the choice "
    "into ~/.claude/settings.json, which plain claude then inherits",
    "/model lists only the lead set; another provider family asks first "
    "(Alt+P and /config block instead); a model outside the set is refused — "
    "relaunch with a profile whose lead is that model.",
)


def _entry(cat: Any, key: str) -> Mapping[str, Any] | None:
    for table in (getattr(cat, "lines", {}), getattr(cat, "retired", {})):
        entry = table.get(key) if isinstance(table, Mapping) else None
        if isinstance(entry, Mapping):
            return entry
    return None


def binding_label(entry_or_retired: Mapping[str, Any] | None, effort: str, *, key: str = "?") -> str:
    """``"{display} · {effort}"``; an entry unknown to the catalog -> ``"{key} · {effort}"``."""

    display = None
    if isinstance(entry_or_retired, Mapping):
        display = entry_or_retired.get("display")
    return f"{display or key} · {effort}"


def _label_of(cat: Any, binding: Mapping[str, Any] | None) -> str:
    if not binding:
        return UNBOUND
    return binding_label(_entry(cat, binding["key"]), binding["effort"], key=binding["key"])


def _lineup_binding(binding: profile.ResolvedBinding) -> dict[str, str]:
    return {"key": binding.key, "effort": binding.effort}


def diff_rows(
    old_applied: Mapping[str, Any],
    lineup: profile.ResolvedLineup,
    cat: Any,
    *,
    old_lead_class: str | None = None,
) -> list[tuple[str, str, str]]:
    """Rows ``(label, old, new)`` between a record's ``applied`` and a lineup (§7.8).

    The lead when its key or effort differs; then each differing agent id in
    ``AGENT_ROLE_IDS`` order; then the relaunch-only fields of §7.5 reasons
    2-6 (lead class, native agents per field, workflows, lead providers,
    compaction override). ``old_lead_class`` is the record's top-level
    ``lead_class`` (``applied`` does not carry it; None skips that row).
    """

    rows: list[tuple[str, str, str]] = []
    old_lead = old_applied.get("lead") or {}
    new_lead = _lineup_binding(lineup.lead.binding)
    if (old_lead.get("key"), old_lead.get("effort")) != (new_lead["key"], new_lead["effort"]):
        rows.append(("lead", _label_of(cat, old_lead), _label_of(cat, new_lead)))
    old_agents = old_applied.get("agents") or {}
    for rid in catalog_mod.AGENT_ROLE_IDS:
        old = old_agents.get(rid)
        agent = lineup.agents.get(rid)
        new = _lineup_binding(agent.binding) if agent is not None else None
        old_pair = None if old is None else (old.get("key"), old.get("effort"))
        new_pair = None if new is None else (new["key"], new["effort"])
        if old_pair != new_pair:
            rows.append((profile.label(rid), _label_of(cat, old), _label_of(cat, new)))
        elif agent is not None and isinstance(old.get("selector"), str):
            # The same binding in another context class (200K <-> 1M) is a
            # visible change at resume.
            before = profile.agent_class_window(old["selector"])
            after = profile.agent_class_window(agent.binding.selector)
            if before != after:
                rows.append((f"{profile.label(rid)} agent class", profile.format_tokens(before),
                             profile.format_tokens(after)))
    fields = lineup.relaunch_fields()
    if old_lead_class is not None and old_lead_class != fields["lead_class"]:
        rows.append(("lead class", str(old_lead_class), str(fields["lead_class"])))
    old_native = old_applied.get("native_agents") or {}
    for field in ("explore", "plan", "general_purpose"):
        a, b = old_native.get(field), fields["native_agents"].get(field)
        if a != b:
            rows.append((f"native agents: {field}", str(a), str(b)))
    if old_applied.get("workflows") != fields["workflows"]:
        rows.append(("workflows", str(old_applied.get("workflows")), str(fields["workflows"])))
    old_providers = old_applied.get("lead_providers")
    new_providers = fields["lead_providers"]
    if (sorted(old_providers) if old_providers else None) != (
        sorted(new_providers) if new_providers else None
    ):
        rows.append(
            (
                "lead providers",
                ",".join(old_providers) if old_providers else "any",
                ",".join(new_providers) if new_providers else "any",
            )
        )
    old_pct = (old_applied.get("settings_overrides") or {}).get("compaction_percent")
    new_pct = (fields["settings_overrides"] or {}).get("compaction_percent")
    if old_pct != new_pct:
        rows.append(
            (
                "compaction override",
                "none" if old_pct is None else str(old_pct),
                "none" if new_pct is None else str(new_pct),
            )
        )
    return rows


def window_diff_rows(record: Mapping[str, Any], lineup: profile.ResolvedLineup) -> list[tuple[str, str, str]]:
    """The session window a resume moves to (the window ceiling or the lead
    set's bounds changed since the session launched): one row, or none."""

    old = sessions.recorded_window(record)
    new = lineup.policy.window if lineup.policy is not None else None
    if old is None or new is None or old == new:
        return []
    return [("context window", profile.format_tokens(old), profile.format_tokens(new))]


def window_summary(lineup: profile.ResolvedLineup, workflow_default: profile.RoleWindow | None = None) -> str:
    """Each role's effective window in one line (the lead, every agent, the
    workflow default): roles that share a window are grouped."""

    roles = [(profile.label(slot), window) for slot, window in profile.lineup_windows(lineup)]
    if workflow_default is not None:
        roles.append(("workflow default", workflow_default))
    percent = (lineup.policy.percent if lineup.policy is not None
               else settings_mod.COMPACTION_PERCENT_DEFAULT)
    groups: dict[tuple[int, int], list[str]] = {}
    for name, window in roles:
        groups.setdefault((window.window, window.trigger), []).append(name)
    if len(groups) == 1:
        (window, trigger), = groups
        who = "the lead"
        if lineup.agents:
            who += " and every agent" if workflow_default is None else ", every agent"
        if workflow_default is not None:
            who += " and the workflow default"
        return (f"context window {profile.format_tokens(window)} for {who} · compacts at "
                f"~{profile.format_tokens(trigger)} ({percent} %)")
    parts = [f"{profile.format_tokens(window)} (compacts at ~{profile.format_tokens(trigger)}) {', '.join(names)}"
             for (window, trigger), names in groups.items()]
    return f"context windows ({percent} %): " + " · ".join(parts)


def render_diff(rows: Sequence[tuple[str, str, str]]) -> list[str]:
    """One ``"  {label:<20} {old} → {new}"`` line per row."""

    return [f"  {label:<20} {old} → {new}" for label, old, new in rows]


def _function_lines(lineup: profile.ResolvedLineup) -> list[str]:
    by_function: dict[str, list[str]] = {}
    for rid, agent in lineup.agents.items():
        binding = agent.binding
        by_function.setdefault(agent.role.function, []).append(
            f"{profile.label(rid)} {binding.display} · {binding.effort}"
        )
    return [
        " · ".join(by_function[function])
        for function in catalog_mod.ROLE_FUNCTIONS
        if function in by_function
    ]


def render_text(
    lineup: profile.ResolvedLineup,
    *,
    header: str,
    generation: int | None = None,
    follow: bool | None = None,
    pending: Mapping[str, Any] | None = None,
    lead_target: Mapping[str, Any] | None = None,
    needs_choice: str | None = None,
    drift: Sequence[str] = (),
    managed_id: str | None = None,
    profile_view: bool = False,
    cat: Any = None,
    before_help: Sequence[str] = (),
    workflow_default: profile.RoleWindow | None = None,
) -> str:
    """The ``lineup show`` / ``profile show`` / confirm view (§7.8; T §7 shape).

    ``profile_view`` omits the generation and the ``/cm`` help line
    (``profile show``). ``follow`` is informational (the caller's ``header``
    already says ``(follows)``/``(pinned)``). ``before_help`` (the ``/cm``
    profiles block) goes right above the help line. Each role's
    effective window follows the agents (:func:`window_summary`, with
    ``workflow_default`` when the Settings workflow default is set). Lines
    end with ``\\n``.
    """

    del follow
    lead = lineup.lead.binding
    head = f"claude-multi · {header}"
    if not profile_view:
        head += f" · lineup gen {generation if generation else '-'}"
    head += (
        f" · lead {lead.display} · {lead.effort} · class {lineup.lead_class} "
        f"({profile.format_tokens(lead.client_context_tokens)})"
    )
    lines = [head]
    lines += _function_lines(lineup)
    if not lineup.agents:
        lines.append("none: this session has no cm-* agents")
    lines.append(window_summary(lineup, workflow_default))
    lines += [f"! {finding.message}" for finding in lineup.warnings]
    lines += [finding.message for finding in lineup.notices]
    mid = managed_id or "<id>"
    if pending:
        reasons = "; ".join(pending.get("reasons") or ())
        lines.append(
            f"pending relaunch change: {reasons} — applies at the next resume "
            f"(claude-multi -r {mid})"
        )
    if lead_target:
        label = binding_label(
            _entry(cat, lead_target["key"]) if cat is not None else None,
            lead_target["effort"],
            key=lead_target["key"],
        )
        lines.append(
            f"requested lead: {label} ({lead_target.get('selector')}) — switch with /model "
            "(press s: this session only), or it applies at the next resume"
        )
    if needs_choice:
        lines.append(
            f"needs a lead choice: {needs_choice} — claude-multi -r {mid} (interactive) "
            "or /cm profile <name>"
        )
    for line in drift:
        lines.append(f"settings changed since launch: {line} — applies at the next resume")
    if not profile_view:
        lines.extend(before_help)
        lines.append(HELP_LINE)
        lines.extend(MODEL_HELP_LINES)
    return "\n".join(lines) + "\n"


# ============================================================= request parsing
# §7.1 argv, §7.2-§7.7 apply, §7.9 outputs, §8 propagation.

# ------------------------------------------------------------------ texts

PREFIX = "claude-multi: "
R1 = "lineup: unknown option {opt!r} (options: --session ID, --relaunch, --its-exited, --preview)"
R2 = "lineup needs --session <runtime-id> (or run /cm inside a claude-multi session)"
R3 = "lineup: session id {value!r} is not a UUID"
R4 = (
    "/cm received {n} arguments; write the request without quotes (balanced "
    "apostrophes split it), e.g. /cm set implementer=opus:high"
)
R5 = "/cm arguments must not start with '!'"
R6 = "/cm could not split {text!r}: {exc}"
R7 = "unknown lineup request {text!r}; use: " + " · ".join(verb.spelled() for verb in lineup_files.CM_VERBS)
R26 = "lineup: --preview applies only to a fallback request (lineup fallback <provider> --preview)"
R8 = "{rid} is an unadopted native fork: {message}"
R9 = (
    "session {m8} is a legacy record (version {v}); a lineup change needs one resume with this "
    "launcher first: claude-multi -r {mid} (or claude-multi migrate), or relaunch it now from "
    "outside with claude-multi lineup --session {rid} --relaunch …"
)
R11 = "profile {name!r} does not exist"
R12 = (
    "session {m8} has no profile to follow (ad-hoc lineup); choose one with /cm profile <name>"
)
R13 = (
    "session {m8} follows {p!r}, which no longer exists; choose one with /cm profile "
    "<name> or keep it pinned"
)
R14 = "the requested lineup is invalid:"
R15 = "cannot load named bindings: {detail}"
R16 = (
    "project agent(s) would shadow newly bound ids: {paths}; rename them or keep {ids} unbound"
)
R17 = (
    "the lineup change failed after partially updating the scope ({exc}); the record "
    "still holds lineup generation {G}; run claude-multi doctor --repair {mid}, then retry"
)
R18 = "{label} → {key}: provider {provider} has no credential ({hint}); connect it first — nothing changed"
R19 = sessions.MIGRATION_BUSY_TEXT
# /cm and the TUI lineup apply wait for the gateway barrier as long as a
# launch does; then they refuse neutrally.
R27 = state.BARRIER_BUSY_TEXT + " — nothing changed; retry in a moment"
R25 = (
    "session {m8} is a legacy record (version {v}) and cannot hold a pending change; rerun "
    "from outside with --its-exited after it exits (claude-multi lineup --session {rid} "
    "--relaunch --its-exited {request}), or run claude-multi migrate first"
)

RELOAD_LINES = (
    "Running agents keep their current model; do not SendMessage-continue an agent "
    "spawned before this change — spawn it fresh.",
    "Forks sharing this scope see the change too.",
)

# Every /cm mutation ends with exactly one
# next-step line. A model cannot run /reload-plugins, so the line says so.
NEXT_RELOAD = (
    "next: type /reload-plugins (a model cannot run it for you; it also applies any "
    "pending plugin updates)"
)
NEXT_RELOAD_AND_MODEL = "Next: /reload-plugins, then /model (press s) — or resume the session."
NEXT_LEAD_NOW = "lead: {label} — now with /model {row} (press s), or at the next resume"
NEXT_LEAD_RESUME = "lead: {label} — at the next resume: claude-multi -r {mid}"
NEXT_RESUME = "Exit the session, then run: claude-multi -r {mid}"
NEXT_RECORDED = "recorded; applies at the next resume"
NEXT_NONE = "no change"

# The profiles block of bare /cm and /cm profiles.
PROFILES_HEAD = "profiles:"
PROFILES_SWITCH = (
    "switch: /cm profile <name> (agents change now; a different lead applies at the next resume)"
)
_PURPOSE_MAX = 72

# /cm fallback PROVIDER.
FALLBACK_NONE = (
    "fallback {provider}: no complete fallback profile is available.\n"
    'Create or edit a profile with primary_provider "{provider}" and bindings for the current '
    "roles."
)
FALLBACK_MISSING = FALLBACK_NONE + "\nprofile {name} has no binding for the bound role(s): {roles}"
FALLBACK_SEVERAL = (
    'fallback {provider}: several profiles declare primary_provider "{provider}": {names}; '
    "choose one explicitly with /cm profile <name> (nothing changed)"
)
FALLBACK_EXISTING = (
    "Existing agents keep their current task and model. Start fresh agents after the change."
)
FALLBACK_LIVE = (
    "Fallback staged at generation {generation}.",
    "Run /reload-plugins before starting fresh agents; reload is not yet confirmed.",
)
FALLBACK_PENDING = (
    "Fallback requires relaunch; the change is pending for the next resume.",
    "No running agent or lead was moved.",
)
FALLBACK_UNCHANGED = "Fallback: nothing moves in the running scope; the record now states the fallback."
FALLBACK_PREVIEW_ONLY = "Preview only; nothing changed. Apply it with /cm fallback {provider}."

# /cm review — report-only, lineup-routed.
REVIEW_STAKES = ("normal", "high-stakes")
REASON_2X_RECORD = "this session still runs a legacy record (version {v})"
REASON_GEN0 = "this session still runs a legacy scope (lineup generation 0)"
REASON_NO_SCOPE = "this session has no lineup-enabled scope directory"
REASON_LEAD_OUTSIDE = (
    "lead {display} ({selector}) is outside this session's lead set (lead class {cls}); "
    "a lead outside the set needs a relaunch"
)
REASON_LEAD_NEEDS_AGENTS = (
    "lead {display} ({selector}): the requested agents need the requested lead; a "
    "relaunch applies both"
)
REASON_FENCE_DIGEST = (
    "this session's launch-time files (model fence, picker, model, env or "
    "lead-set.json) changed after launch (claude-multi doctor --repair?); the running "
    "process still enforces its launch-time fence, so agent changes need a relaunch"
)
REASON_FENCE_GAP = (
    "{label} → {selector} is outside this session's launch-time model fence (a line "
    "admitted or a provider enabled after launch)"
)
REASON_T2_AGENT = (
    "{label} → {selector}: an operator agent binding is relaunch-class (the model fence is fixed "
    "at launch); it applies at the next resume"
)
REASON_FORCED = "requested (--relaunch)"
REASON_LEAD_SET_UNREADABLE = (
    "the scope's lead-set.json is unreadable; run claude-multi doctor --repair {mid}"
)
REASON_SETTINGS_UNREADABLE = (
    "the scope's settings.json is unreadable; run claude-multi doctor --repair {mid}"
)
LEAD_SWITCH_HINT = (
    "To switch the lead within the lead set without a relaunch, use /model (press s: "
    "this session only)."
)
SHOW_2X_LINE = (
    "this session still runs a legacy scope; changes are recorded as pending and apply at "
    "its next resume: claude-multi -r {mid}"
)
# pending.reasons items are schema-bounded (160 characters, at most 16).
_PENDING_REASON_MAX = 160
_PENDING_REASONS_MAX = 16

_MODEL_KEY = r"[a-z0-9][a-z0-9@._-]{0,63}"
_SET_SPEC = re.compile(rf"^([a-z][a-z0-9-]*)=({_MODEL_KEY})(?::([a-z]+))?$")
_MODEL_SPEC = re.compile(rf"^({_MODEL_KEY})(?::([a-z]+))?$")
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
# A git revision range for /cm review: printed for the lead, never run by the
# launcher; no option-looking, whitespace or shell metacharacters.
_GIT_RANGE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^@{}:-]{0,127}$")
_READ_VERBS = lineup_files.CM_READ_VERBS
_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
_INSIDE_ENV = ("CLAUDE_MULTI_MANAGED_ID", "CLAUDE_MULTI_SESSION_ID")  # T:85-86 sentinels


class LineupRefusal(errors.ClaudeMultiError):
    """A refused request; ``str(exc)`` is the text without the ``claude-multi: `` prefix."""


# ------------------------------------------------------------ §7.1 parse


@dataclass(frozen=True)
class Request:
    """One parsed ``lineup`` request (§7.1 grammar)."""

    verb: str  # show | profiles | set | unset | profile | pin | follow | direct | fallback | review | quota
    agent: str | None = None  # full agent id (set/unset)
    model: str | None = None  # set / direct M
    effort: str | None = None  # set / direct M (None: the default)
    name: str | None = None  # profile N
    text: str = ""  # the words as given (messages)
    provider: str | None = None  # fallback P (the target provider)
    stakes: str | None = None  # review: normal | high-stakes
    range: str | None = None  # review: a git range (None: working tree against HEAD)

    @property
    def writes(self) -> bool:
        return self.verb not in _READ_VERBS


@dataclass(frozen=True)
class LineupArgs:
    skill_mode: bool  # "--session" present (with or without a value)
    session: str | None  # the resolved id string (after the env fallback)
    relaunch: bool
    its_exited: bool
    request: Request
    preview: bool = False  # --preview: a read-only fallback preview

    @property
    def writes(self) -> bool:
        return self.request.writes and not self.preview


def skill_mode_of(argv: Sequence[str]) -> bool:
    """Skill mode = ``--session`` among the options (before ``--``)."""

    for token in argv:
        if token == "--":
            return False
        if token == "--session" or token.startswith("--session="):
            return True
    return False


def _agent_id(value: str) -> str | None:
    if value in catalog_mod.AGENT_ROLE_IDS:
        return value
    candidate = f"cm-{value}"
    return candidate if candidate in catalog_mod.AGENT_ROLE_IDS else None


def _parse_show(rest: list[str], text: str) -> Request | None:
    return Request("show", text=text) if not rest else None


def _parse_bare(verb: str) -> Callable[[list[str], str], Request | None]:
    def parse(rest: list[str], text: str) -> Request | None:
        return Request(verb, text=text) if not rest else None
    return parse


def _parse_fallback(rest: list[str], text: str) -> Request | None:
    if len(rest) != 1 or not _PROVIDER_ID.fullmatch(rest[0]):
        return None
    return Request("fallback", provider=rest[0], text=text)


def _parse_review(rest: list[str], text: str) -> Request | None:
    if len(rest) > 2:
        return None
    remaining = list(rest)
    stakes = "normal"
    if remaining and remaining[0] in REVIEW_STAKES:
        stakes = remaining.pop(0)
    revision = None
    if remaining:
        revision = remaining.pop(0)
        if not _GIT_RANGE.fullmatch(revision) or revision in REVIEW_STAKES:
            return None
    if remaining:
        return None
    return Request("review", stakes=stakes, range=revision, text=text)


def _parse_set(rest: list[str], text: str) -> Request | None:
    if len(rest) != 1 or (match := _SET_SPEC.fullmatch(rest[0])) is None:
        return None
    agent = _agent_id(match[1])
    effort = match[3]
    if agent is None or (effort is not None and effort not in profile.EFFORT_ORDER):
        return None
    return Request("set", agent=agent, model=match[2], effort=effort, text=text)


def _parse_unset(rest: list[str], text: str) -> Request | None:
    agent = _agent_id(rest[0]) if len(rest) == 1 else None
    return Request("unset", agent=agent, text=text) if agent is not None else None


def _parse_profile(rest: list[str], text: str) -> Request | None:
    if len(rest) != 1 or not _PROFILE_NAME.fullmatch(rest[0]):
        return None
    return Request("profile", name=rest[0], text=text)


def _parse_direct(rest: list[str], text: str) -> Request | None:
    if not rest:
        return Request("direct", text=text)
    if len(rest) != 1 or (match := _MODEL_SPEC.fullmatch(rest[0])) is None:
        return None
    effort = match[2]
    if effort is not None and effort not in (*profile.EFFORT_ORDER, profile.ULTRACODE):
        return None
    return Request("direct", model=match[1], effort=effort, text=text)


# One parser per verb of the table (``lineup_files.CM_VERBS``); the import
# check below keeps the two identical.
_VERB_PARSERS: dict[str, Callable[[list[str], str], Request | None]] = {
    "show": _parse_show,
    "profiles": _parse_bare("profiles"),
    "profile": _parse_profile,
    "set": _parse_set,
    "unset": _parse_unset,
    "direct": _parse_direct,
    "pin": _parse_bare("pin"),
    "follow": _parse_bare("follow"),
    "fallback": _parse_fallback,
    "review": _parse_review,
    "quota": _parse_bare("quota"),
}
if tuple(_VERB_PARSERS) != lineup_files.CM_VERB_NAMES:
    raise RuntimeError("every /cm verb of lineup_files.CM_VERBS needs exactly one parser")


def parse_request(words: Sequence[str]) -> Request:
    """The request grammar over the words (empty = ``show``); refuse invalid syntax."""

    words = list(words)
    text = " ".join(words)
    if not words:
        return Request("show", text=text)
    parser = _VERB_PARSERS.get(words[0])
    request = parser(words[1:], text) if parser is not None else None
    if request is None:
        raise LineupRefusal(R7.format(text=text))
    return request


def _split_element(element: str) -> list[str]:
    """The skill's one argument: refuse a leading ``!``, then parse with ``shlex``."""

    if element.startswith("\\!") or element.startswith("!"):
        raise LineupRefusal(R5)
    try:
        return shlex.split(element)
    except ValueError as exc:
        raise LineupRefusal(R6.format(text=element, exc=exc)) from exc


def parse_cli(argv: Sequence[str], environ: Mapping[str, str]) -> LineupArgs:
    """``claude-multi lineup`` argv (the tail after ``lineup``; §7.1 rules 1-4).

    Skill mode (``--session`` given) takes the session per rules 2a-2d
    (an empty ``${CLAUDE_SESSION_ID}`` expansion falls back to
    ``$CLAUDE_CODE_SESSION_ID``); the skill's one argument is re-split with
    ``shlex`` after the ``!`` check. A multi-element tail in skill mode is
    refused when any element is empty or contains whitespace — the
    shape a balanced apostrophe leaves (S §8.2) — and otherwise read as the
    §8.3 CLI words (``lineup --session <id> profile NAME``, the form every
    printed remedy uses; recorded deviation). Outside skill mode the words
    are taken as given and the session comes from the environment.
    """

    args = list(argv)
    n = len(args)
    relaunch = its_exited = preview = False
    skill = False
    session: str | None = None
    from_env = False
    decided: list[str] | None = None  # rules 2a/2d decide the request themselves
    one_element: str | None = None  # rule 2a's non-UUID value (the skill argument)
    ended_on_session = False
    index = 0
    while index < n:
        token = args[index]
        if token == "--":
            index += 1
            break
        if token == "--relaunch":
            relaunch = True
            index += 1
            continue
        if token == "--its-exited":
            its_exited = True
            index += 1
            continue
        if token == "--preview":
            preview = True
            index += 1
            continue
        if token in lineup_files.PRESENTATION_FLAGS:
            index += 1
            continue
        if token.startswith("--session="):
            skill = True
            value = token[len("--session=") :]
            if value == "":
                from_env = True
            elif sessions.UUID4.fullmatch(value):
                session = value
            else:
                raise LineupRefusal(R3.format(value=value))
            index += 1
            ended_on_session = index == n
            continue
        if token == "--session":
            skill = True
            if index + 1 >= n:  # 2d: a bare --session
                from_env = True
                decided = []
                index += 1
                break
            value = args[index + 1]
            if index + 2 >= n:  # 2a: nothing after the value
                if sessions.UUID4.fullmatch(value):
                    session = value
                    decided = []
                else:
                    from_env = True
                    one_element = value
                index += 2
                break
            if value == "":  # 2b
                from_env = True
            elif sessions.UUID4.fullmatch(value):
                session = value
            else:
                raise LineupRefusal(R3.format(value=value))
            index += 2
            continue
        if token.startswith("-") and token != "-":
            raise LineupRefusal(R1.format(opt=token))
        break
    rest = args[index:]
    if from_env or (session is None and not skill):
        value = str(environ.get(_SESSION_ENV) or "")
        if value:
            if not sessions.UUID4.fullmatch(value):
                raise LineupRefusal(R3.format(value=value))
            session = value
    if session is None:
        raise LineupRefusal(R2)
    if skill:
        if decided is not None:
            words = list(decided)
        elif one_element is not None:
            words = _split_element(one_element)
        elif not rest and ended_on_session:
            words = []
        elif len(rest) == 1:
            words = _split_element(rest[0])
        elif len(rest) >= 2 and all(tok and not any(ch.isspace() for ch in tok) for tok in rest):
            if rest[0].startswith("\\!") or rest[0].startswith("!"):
                raise LineupRefusal(R5)
            words = list(rest)
        else:
            raise LineupRefusal(R4.format(n=len(rest)))
    else:
        words = list(rest)
    if len(words) == 3 and words[0] == "fallback" and words[2] == "--preview":
        # The trailing spelling (and that of R26's remedy) —
        # `lineup fallback <provider> --preview`.
        preview, words = True, words[:2]
    request = parse_request(words)
    if preview and request.verb != "fallback":
        raise LineupRefusal(R26)
    return LineupArgs(
        skill_mode=skill,
        session=session,
        relaunch=relaunch,
        its_exited=its_exited,
        request=request,
        preview=preview,
    )


# --------------------------------------------------------- shared helpers


def _now() -> str:
    return sessions._now()


def _m8(mid: str) -> str:
    return mid[:8]


def _pair(binding: Mapping[str, Any] | None) -> tuple[Any, Any] | None:
    if binding is None:
        return None
    return (binding.get("key"), binding.get("effort"))


def _lead_label(lcat: Any, key: str, effort: str) -> str:
    return binding_label(_entry(lcat, key), effort, key=key)


def _applied_matches(lineup: profile.ResolvedLineup, applied: Mapping[str, Any]) -> bool:
    """The lineup restates ``applied`` (lead, agents, relaunch-only fields)."""

    bindings = lineup.applied_bindings()
    agents = {rid: _pair(b) for rid, b in bindings["agents"].items()}
    old_agents = {rid: _pair(b) for rid, b in applied["agents"].items()}
    fields = lineup.relaunch_fields()
    return (
        _pair(bindings["lead"]) == _pair(applied["lead"])
        and agents == old_agents
        and dict(fields["native_agents"]) == dict(applied["native_agents"])
        and fields["workflows"] == applied["workflows"]
        and (sorted(fields["lead_providers"]) if fields["lead_providers"] else None)
        == (sorted(applied["lead_providers"]) if applied["lead_providers"] else None)
        and dict(fields["settings_overrides"] or {}) == dict(applied["settings_overrides"] or {})
    )


def _record_eff(record: Mapping[str, Any]) -> settings_mod.Effective:
    """The record's own Settings snapshot, with the window the session
    launched with (each agent's class is decided against it)."""

    try:
        return settings_mod.effective_from_snapshot(
            record["applied"]["settings"], window_ceiling=sessions.recorded_window(record))
    except settings_mod.SettingsError as exc:
        mid = sessions.managed_id(dict(record))
        raise LineupRefusal(
            f"session {_m8(mid)}: its Settings snapshot is unreadable ({exc}); resume it "
            f"once (claude-multi -r {mid})"
        ) from exc


def _record_gate(lcat: Any, record: Mapping[str, Any], eff: settings_mod.Effective) -> Any:
    """``lcat`` with the record-mode operator agent gate over its current facts."""

    from . import transition

    gate = getattr(lcat, "agent_gate", None)
    facts = gate.facts if gate is not None else {}
    return lcat.with_gate(transition.record_agent_gate(record, eff, facts))


def _bindings_map(runtime: Any) -> dict[str, Any]:
    try:
        return runtime.bindings.bindings()
    except profile.BindingError as exc:
        raise LineupRefusal(R15.format(detail=exc)) from exc


def _evaluate(
    runtime: Any,
    document: Mapping[str, Any],
    eff: settings_mod.Effective,
    lcat: profile.LineupCatalog,
    *,
    profile_name: str | None,
    bindings: Mapping[str, Any] | None = None,
) -> profile.ResolvedLineup:
    """Evaluate a target; ``bindings`` None reads the named bindings, refusing invalid input."""

    if bindings is None:
        bindings = _bindings_map(runtime)
    evaluation = profile.evaluate(
        document, lcat, bindings=bindings, effective=eff, ad_hoc=profile_name is None
    )
    if evaluation.errors:
        raise LineupRefusal(R14 + "".join(f"\n- {error}" for error in evaluation.errors))
    return evaluation.lineup


def _load_profile(runtime: Any, name: str) -> dict[str, Any]:
    profiles = runtime.profiles
    try:
        if not profiles.contains(name):
            raise LineupRefusal(R11.format(name=name))
        return profiles.load(name)
    except profile.ProfileError as exc:
        if "does not exist" in str(exc):
            raise LineupRefusal(R11.format(name=name)) from exc
        raise LineupRefusal(str(exc)) from exc


def _is_live(runtime: Any, record: Mapping[str, Any]) -> bool:
    """§8: live = last event not ``end`` (null included) or a ● daemon prefix."""

    if record.get("last_event_source") != "end":
        return True
    prefixes = getattr(runtime, "_live_prefixes", lambda: frozenset())()
    if not prefixes:
        return False
    candidates = [sessions.managed_id(dict(record)), record.get("runtime_session_id", "")]
    candidates.extend(item.get("session_id", "") for item in record.get("runtime_aliases", []))
    return any(c.startswith(p) for c in candidates for p in prefixes)


def _truncate_reasons(reasons: Sequence[str]) -> list[str]:
    out = []
    for reason in list(reasons)[:_PENDING_REASONS_MAX]:
        text = reason if len(reason) <= _PENDING_REASON_MAX else reason[: _PENDING_REASON_MAX - 1] + "…"
        out.append(text)
    return out


# ----------------------------------------------------------- §7.3 target


@dataclass(frozen=True)
class Target:
    """What a request asks for: a document plus the record's profile/follow after it."""

    document: dict[str, Any] | None  # None: pin (record-only) or a v1-3 ``profile N``
    profile: str | None
    follow: bool
    kind: str  # "profile" (profile/follow) | "lineup" | "pin"
    recipe: str | None = None  # fallback: the primary_provider profile used


def build_target(record: Mapping[str, Any], request: Request, runtime: Any) -> Target:
    """§7.3: the target document of a request on a v4 record (profile files read lock-free)."""

    applied = record["applied"]
    mid = record["managed_id"]
    verb = request.verb
    if verb == "pin":
        return Target(None, record["profile"], False, "pin")
    if verb in ("set", "unset"):
        document = sessions.applied_document(dict(record))
        if verb == "set":
            effort = request.effort
            if effort is None:
                effort = _default_effort(runtime, request.model)
            document["agents"][request.agent] = {"model": request.model, "effort": effort}
        else:
            document["agents"].pop(request.agent, None)
        return Target(document, record["profile"], False, "lineup")
    if verb == "profile":
        return Target(_load_profile(runtime, str(request.name)), request.name, True, "profile")
    if verb == "follow":
        name = record["profile"]
        if name is None:
            raise LineupRefusal(R12.format(m8=_m8(mid)))
        try:
            exists = runtime.profiles.contains(name)
        except profile.ProfileError:
            exists = False
        if not exists:
            raise LineupRefusal(R13.format(m8=_m8(mid), p=name))
        return Target(_load_profile(runtime, name), name, True, "profile")
    if verb == "direct":
        if request.model is None:
            lead = applied["lead"]
            document = profile.ad_hoc_direct(lead["key"], lead["effort"])
        else:
            document = profile.ad_hoc_direct(
                request.model, request.effort or profile.ULTRACODE
            )
        return Target(document, None, False, "lineup")
    if verb == "fallback":
        name, recipe = fallback_recipe(runtime, str(request.provider))
        document = fallback_document(record, str(request.provider), name, recipe)
        # The session keeps its profile label and is pinned (follow off);
        # the source profile and the named bindings are never modified.
        return Target(document, record["profile"], False, "lineup", recipe=name)
    raise LineupRefusal(R7.format(text=request.text))


def fallback_recipe(runtime: Any, provider: str) -> tuple[str, dict[str, Any]]:
    """The one profile declaring ``primary_provider: provider``.

    Profiles are read lock-free (as ``profile N`` does); an unreadable one is
    skipped. None refuses with the exact remedy; several refuse by name
    (never a silent choice).
    """

    matches: list[tuple[str, dict[str, Any]]] = []
    for name in runtime.profiles.names():
        try:
            document = runtime.profiles.load(name)
        except profile.ProfileError:
            continue
        if document.get("primary_provider") == provider:
            matches.append((name, document))
    if not matches:
        raise LineupRefusal(FALLBACK_NONE.format(provider=provider))
    if len(matches) > 1:
        raise LineupRefusal(
            FALLBACK_SEVERAL.format(provider=provider, names=", ".join(name for name, _ in matches))
        )
    return matches[0]


def fallback_providers(runtime: Any) -> list[str]:
    """Every provider some profile declares as its ``primary_provider``:
    the destinations ``/cm fallback`` has a recipe for, whatever the session
    runs now (read lock-free, as :func:`fallback_recipe` reads them; an
    unreadable profile is skipped). Sorted; several recipes for one provider
    still list it once (the preview names them)."""

    found: set[str] = set()
    for name in runtime.profiles.names():
        try:
            document = runtime.profiles.load(name)
        except profile.ProfileError:
            continue
        primary = document.get("primary_provider")
        if isinstance(primary, str) and primary:
            found.add(primary)
    return sorted(found)


def fallback_document(
    record: Mapping[str, Any], provider: str, name: str, recipe: Mapping[str, Any]
) -> dict[str, Any]:
    """The candidate lineup of ``/cm fallback``.

    The lead and every currently bound role come from the recipe by exact
    role id (grade included); a bound role the recipe does not bind refuses
    (never an implicit unbind), an unbound role stays unbound.
    ``primary_provider`` becomes ``provider`` and ``lead_providers`` the
    recipe's; native agents, workflows and settings overrides are kept.
    """

    current = sessions.applied_document(dict(record))
    bound = current.get("agents") or {}
    recipe_agents = recipe.get("agents") or {}
    missing = [rid for rid in catalog_mod.AGENT_ROLE_IDS if rid in bound and rid not in recipe_agents]
    if missing:
        raise LineupRefusal(
            FALLBACK_MISSING.format(provider=provider, name=name, roles=", ".join(missing))
        )
    document = copy.deepcopy(current)
    document["lead"] = copy.deepcopy(dict(recipe["lead"]))
    document["agents"] = {
        rid: copy.deepcopy(dict(recipe_agents[rid]))
        for rid in catalog_mod.AGENT_ROLE_IDS
        if rid in bound
    }
    document["primary_provider"] = provider
    if recipe.get("lead_providers"):
        document["lead_providers"] = list(recipe["lead_providers"])
    else:
        document.pop("lead_providers", None)
    return document


def _default_effort(runtime: Any, key: str | None) -> str:
    """``set`` without ``:effort``: the line's ``default_effort`` (retired → successor)."""

    lcat = runtime.lineup_catalog()
    entry = lcat.lines.get(key) if key else None
    if entry is None and key:
        try:
            resolved = lcat.resolve_key(key).key
        except catalog_mod.CatalogError:
            resolved = None
        entry = lcat.lines.get(resolved) if resolved else None
    if isinstance(entry, Mapping) and isinstance(entry.get("default_effort"), str):
        return entry["default_effort"]
    return "high"  # an unknown key: evaluation reports it


# ------------------------------------------------------- §7.5 classify


@dataclass(frozen=True)
class Classification:
    kind: str  # "live" | "relaunch"
    reasons: tuple[str, ...]
    lead_switch: Mapping[str, Any] | None = None  # the lead-set row to /model onto
    effort_only: bool = False  # same lead selector, another effort
    lead_set: Mapping[str, Any] | None = None


def _read_scope_launch_files(
    scope_dir: Path,
) -> tuple[dict[str, Any] | None, bytes | None, dict[str, Any] | None, str | None]:
    """``(lead_set, lead_set_bytes, settings, problem)`` of a live scope."""

    try:
        lead_set = hooks.load_lead_set(scope_dir)
        lead_bytes = state.read_private(Path(scope_dir) / lineup_files.LEAD_SET_JSON)
    except (hooks.HookInputError, OSError, state.StateError):
        return None, None, None, "lead-set"
    try:
        disk_settings = strict_json.loads(
            state.read_private(Path(scope_dir) / scope.SETTINGS_RELPATH)
        )
    except (OSError, state.StateError, strict_json.StrictJSONError):
        disk_settings = None
    if not isinstance(disk_settings, dict):
        return lead_set, lead_bytes, None, "settings"
    return lead_set, lead_bytes, disk_settings, None


def classify(
    record: Mapping[str, Any],
    lineup: profile.ResolvedLineup,
    scope_dir: Path | str,
    *,
    forced: bool,
    lcat: Any = None,
) -> Classification:
    """LIVE vs RELAUNCH for a v4 gen >= 1 record with a live scope.

    Reads ``lead-set.json`` and ``settings.json`` once. Reasons in a fixed
    order; none -> ``live``. A target lead that differs from the running one
    but whose normalised selector is a lead-set row sets ``lead_switch``;
    the same selector at another effort sets ``effort_only``.
    """

    mid = record["managed_id"]
    applied = record["applied"]
    lead_set, lead_bytes, disk_settings, problem = _read_scope_launch_files(Path(scope_dir))
    if problem == "lead-set":
        return Classification("relaunch", (REASON_LEAD_SET_UNREADABLE.format(mid=mid),))
    if problem == "settings":
        return Classification(
            "relaunch", (REASON_SETTINGS_UNREADABLE.format(mid=mid),), lead_set=lead_set
        )
    assert lead_set is not None and disk_settings is not None and lead_bytes is not None
    reasons: list[str] = []
    lead = lineup.lead.binding
    target_norm = lineup_files.normalize_model(lead.selector)
    running = applied["lead"]
    running_norm = lineup_files.normalize_model(running["selector"] or "")
    row = next(
        (r for r in lead_set["rows"] if lineup_files.normalize_model(r["selector"]) == target_norm),
        None,
    )
    lead_switch = None
    effort_only = False
    if row is None:
        reasons.append(
            REASON_LEAD_OUTSIDE.format(
                display=lead.display, selector=lead.selector, cls=lead_set["lead_class"]
            )
        )
    elif (lead.key, lead.effort) != (running["key"], running["effort"]):
        if target_norm != running_norm:
            lead_switch = row
        else:
            effort_only = True
    fields = lineup.relaunch_fields()
    if record.get("lead_class") != fields["lead_class"]:
        reasons.append(f"lead class {record.get('lead_class')} → {fields['lead_class']}")
    for name in ("explore", "plan", "general_purpose"):
        a = applied["native_agents"].get(name)
        b = fields["native_agents"].get(name)
        if a != b:
            reasons.append(f"native agents: {name} {a} → {b}")
    if applied["workflows"] != fields["workflows"]:
        reasons.append(f"workflows {applied['workflows']} → {fields['workflows']}")
    old_p = sorted(applied["lead_providers"]) if applied["lead_providers"] else None
    new_p = sorted(fields["lead_providers"]) if fields["lead_providers"] else None
    if old_p != new_p:
        reasons.append(
            f"lead providers {','.join(old_p) if old_p else 'any'} → "
            f"{','.join(new_p) if new_p else 'any'}"
        )
    old_pct = (applied["settings_overrides"] or {}).get("compaction_percent")
    new_pct = (fields["settings_overrides"] or {}).get("compaction_percent")
    if old_pct != new_pct:
        reasons.append(
            f"compaction override {'none' if old_pct is None else old_pct} → "
            f"{'none' if new_pct is None else new_pct}"
        )
    new_agents = {rid: _pair(b) for rid, b in lineup.applied_bindings()["agents"].items()}
    old_agents = {rid: _pair(b) for rid, b in applied["agents"].items()}
    agents_change = new_agents != old_agents
    if agents_change:
        def operator_agent(key: str) -> bool:
            return lcat is not None and getattr(lcat, "origin", None) is not None and (
                lcat.origin(key) in profile.OPERATOR_ORIGINS)

        if sessions.launch_digest(disk_settings, lead_bytes) != record.get("launch_fence"):
            reasons.append(REASON_FENCE_DIGEST)
        else:
            available = disk_settings.get("availableModels")
            available = available if isinstance(available, list) else []
            # Only new or changed slots can open a fence gap; an unchanged
            # slot runs on what the launch admitted.
            moved = {rid: agent for rid, agent in lineup.agents.items()
                     if new_agents.get(rid) != old_agents.get(rid)}
            gaps = set(
                scope.fence_gaps(available, [a.binding.selector for a in moved.values()])
            )
            for rid, agent in moved.items():
                if agent.binding.selector in gaps:
                    operator = operator_agent(agent.binding.key)
                    reasons.append(
                        (REASON_T2_AGENT if operator else REASON_FENCE_GAP).format(
                            label=profile.label(rid), selector=agent.binding.selector
                        )
                    )
    if forced:
        reasons.append(REASON_FORCED)
    kind = "relaunch" if reasons else "live"
    return Classification(kind, tuple(reasons), lead_switch, effort_only, lead_set)


# --------------------------------------------------------- phase A (§7.6)


@dataclass
class Decision:
    """The outcome of phase A for one record (the caller renders or execs)."""

    kind: str  # "show" | "noop" | "record" | "live" | "pending" | "exec"
    text: str = ""
    managed_id: str = ""
    generation: int | None = None
    reasons: tuple[str, ...] = ()
    lead_switch: Mapping[str, Any] | None = None
    sample: tuple[Any, Any, Any, Any] | None = None
    target: Target | None = None
    pending_dropped: bool = False
    # What a preview decided on beyond the record identity (the resolved
    # target and its consequences, :func:`_target_fingerprint`).
    fingerprint: str | None = None


def _checkpoint(step: str, detail: str | None = None) -> None:
    """Crash-injection seam of the live-apply write order (§16.5); a no-op."""

    del step, detail


def _agent_relpaths(scope_dir: Path) -> dict[str, Path]:
    """Regular ``cm-*.md`` files of the live scope's ``.claude/agents``."""

    directory = scope_dir / ".claude" / "agents"
    found: dict[str, Path] = {}
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return found
    for entry in entries:
        if (
            entry.name.startswith("cm-")
            and entry.name.endswith(".md")
            and entry.is_file(follow_symlinks=False)
        ):
            found[f".claude/agents/{entry.name}"] = Path(entry.path)
    return found


def _secret_problem(runtime: Any, key: str) -> str | None:
    """The credential problem of one agent line's provider, or None."""

    from . import proxy as proxy_mod  # local: proxy is heavy and only needed here

    docs = runtime.ordinary_docs
    lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
    if key not in lines:
        return None

    @dataclass(frozen=True)
    class _Model:
        model: str

    @dataclass(frozen=True)
    class _Adapter:
        lead: _Model
        variants: tuple = ()

    problems = proxy_mod.selected_secret_problems(
        _Adapter(_Model(key)),
        {key: {"provider": lines[key]["provider"]}},
        docs["providers"]["providers"],
        environ=runtime.environ,
    )
    return problems[0] if problems else None


def _compile_live(
    runtime: Any, new_record: dict[str, Any], live_dir: Path
) -> tuple[scope.ScopePlan | None, str | None]:
    """The non-launch-time files of ``new_record`` exactly as converge expects them (§12).

    ``expected_plan`` runs against the live scope, the same call doctor and
    converge make: when the launch files are proven (``launch_fence``)
    ``lineup.md``/``lineup.gen`` state the launch-time lead set, not the
    installed catalog's, so a live apply after the catalog gained a
    lead-class line never writes bytes doctor then reads as drift (§16.6).
    Agent files are the catalog compile's in both cases.
    """

    from . import transition  # local: transition imports launch

    ep = transition.expected_plan(
        new_record,
        docs=runtime.ordinary_docs,
        prompt_bodies=runtime.catalog.prompt_bodies,
        state_root=runtime.session_store.root,
        hook_command=runtime.hook3_command,
        token_helper_command=runtime.token_helper_command,
        live=live_dir,
        environ=runtime.environ,
        managed_root=runtime.managed_root,
        feedback_drafts=runtime.feedback_drafts(),
    )
    if ep.catalog_plan is None:
        return None, ep.reason
    if not ep.launch_files_kept or ep.plan is None:
        return ep.catalog_plan, None
    other = dict(ep.catalog_plan.other_files)
    for rel in (lineup_files.LINEUP_MD, lineup_files.LINEUP_GEN):
        if rel in ep.plan.other_files:
            other[rel] = ep.plan.other_files[rel]
    return (
        scope.ScopePlan(
            agent_files=dict(ep.catalog_plan.agent_files),
            settings=dict(ep.catalog_plan.settings),
            other_files=other,
        ),
        None,
    )


def _pending_document(target: Target) -> dict[str, Any] | None:
    if target.kind == "profile":
        return None
    return copy.deepcopy(target.document)


def _output_live(
    lcat: Any,
    record: Mapping[str, Any],
    *,
    generation: int,
    changes: Sequence[tuple[str, str, str]],
    extra: Sequence[str],
    next_line: str = NEXT_RELOAD,
) -> str:
    if not changes:
        # only lineup.md moved (a profile name, or the lead the scope states)
        lines = [f"lineup gen {generation}: lineup.md restated (no agent changes)"]
    elif len(changes) == 1:
        label, old, new = changes[0]
        lines = [f"lineup gen {generation}: {label} {old} → {new}"]
    else:
        lines = [f"lineup gen {generation}:"]
        lines += [f"  {label} {old} → {new}" for label, old, new in changes]
    lines += list(extra)
    lines += list(RELOAD_LINES)
    lines.append(next_line)
    return "\n".join(lines) + "\n"


def _output_pending(mid: str, reasons: Sequence[str], *, lead_label: str | None = None) -> str:
    """A recorded RELAUNCH change; the last line is the one next step."""

    lines = [
        f"relaunch needed: {'; '.join(reasons)}",
        f"Recorded as a pending change for session {_m8(mid)}; nothing in the running "
        "session changed.",
    ]
    if any(reason.startswith("lead ") and "outside this session's lead set" in reason for reason in reasons):
        lines.append(LEAD_SWITCH_HINT)
    if lead_label is not None:
        lines.append(NEXT_LEAD_RESUME.format(label=lead_label, mid=mid))
    else:
        lines.append(NEXT_RESUME.format(mid=mid))
    return "\n".join(lines) + "\n"


def _save_record_only(
    store: sessions.SessionStore,
    record: dict[str, Any],
    *,
    follow: bool,
    drop_lead_target: bool = False,
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(record)
    updated["follow"] = bool(follow)
    dropped = updated.pop("pending", None) is not None
    if drop_lead_target:
        updated.pop("lead_target", None)
    updated["mutation_token"] = sessions.new_mutation_token()
    store.save(updated)
    return updated, dropped


def decide_locked(
    runtime: Any,
    record: dict[str, Any],
    request: Request,
    *,
    forced: bool,
    exec_ok: bool,
    origin: str = "request",
    now: str | None = None,
    preview_only: bool = False,
    expected_target: str | None = None,
) -> Decision:
    """Phase A body on a record loaded under the shared hold + lifecycle lock.

    Never confirms, prepares or performs (cf[1]). ``exec_ok``: a RELAUNCH
    runs the caller's relaunch executor (sampled here, run after release);
    otherwise it becomes ``pending`` (v4) or is refused for a legacy record. ``preview_only``
    (a fallback ``--preview``) returns the preview before any write;
    the caller may then run without a lock. ``expected_target`` is the
    fingerprint a confirmed preview showed: the target is evaluated once
    here, and when that evaluation's fingerprint differs nothing is written
    and the decision is ``stale`` with the current preview; otherwise the
    evaluation that passed the comparison is the one applied.
    """

    store = runtime.session_store
    mid = sessions.managed_id(record)
    m8 = _m8(mid)
    rid = sessions.runtime_session_id(record)
    version = record.get("version")
    if version != sessions.RECORD_VERSION:
        # v1-3 branch (§7.2): profile N / direct M only, never evaluated here.
        if request.verb == "profile":
            _load_profile(runtime, str(request.name))  # refuse an unknown profile before anything else
            target = Target(None, request.name, True, "profile")
        elif request.verb == "direct" and request.model is not None:
            target = Target(
                profile.ad_hoc_direct(request.model, request.effort or profile.ULTRACODE),
                None,
                False,
                "lineup",
            )
        else:
            raise LineupRefusal(R9.format(m8=m8, v=version, mid=mid, rid=rid))
        reasons = (REASON_2X_RECORD.format(v=version),)
        if exec_ok:
            return Decision(
                "exec",
                managed_id=mid,
                reasons=reasons,
                sample=(record.get("launch_epoch", 0), record.get("mutation_token"), None, None),
                target=target,
            )
        raise LineupRefusal(
            R25.format(m8=m8, v=version, rid=rid, request=request.text or request.verb)
        )

    applied = record["applied"]
    generation = int(record["lineup_generation"])
    lcat = runtime.lineup_catalog()
    now = now or _now()
    if request.verb == "pin":
        if not record["follow"] and "pending" not in record:
            return Decision(
                "noop",
                text=f"session {m8} is already pinned (follow off); lineup gen {generation} unchanged\n"
                f"{NEXT_NONE}\n",
                managed_id=mid,
                generation=generation,
            )
        pinned = Target(None, record["profile"], False, "pin")
        fingerprint = _target_fingerprint(pinned, None, (EFFECT_RECORD,))
        if preview_only or (expected_target is not None and fingerprint != expected_target):
            shown = _preview_decision(record, request, [], effect=EFFECT_RECORD, follow_after=False,
                                      profile_after=record["profile"])
            return _shown_or_stale(shown, fingerprint, preview_only)
        _, dropped = _save_record_only(store, record, follow=False)
        lines = [f"session {m8} is now pinned (follow off); lineup gen {generation} unchanged"]
        if dropped:
            lines.append(_dropped_line(record))
        lines.append(NEXT_RECORDED)
        return Decision("record", text="\n".join(lines) + "\n", managed_id=mid, generation=generation)

    target = build_target(record, request, runtime)
    eff = _record_eff(record)
    # Unchanged agent slots keep the record's grant (its Settings
    # snapshot); a new or changed operator agent slot is evaluated
    # with the current gate (admission, route, evidence) and, when it passes,
    # is relaunch-class by design (the fence is a launch-time key).
    lcat = _record_gate(lcat, record, eff)
    bindings = _bindings_map(runtime)
    lineup = _evaluate(
        runtime, target.document or {}, eff, lcat, profile_name=target.profile, bindings=bindings
    )
    # An unchanged (key, effort) agent keeps its recorded context class
    # until resume; only new or changed slots take the class the installed
    # catalog and the session window decide.
    lineup = profile.keep_recorded_agent_class(lineup, applied["agents"], lcat)
    if request.verb == "follow" and _applied_matches(lineup, applied):
        fingerprint = _target_fingerprint(target, lineup, (EFFECT_RECORD,))
        if preview_only or (expected_target is not None and fingerprint != expected_target):
            shown = _preview_decision(record, request, [], effect=EFFECT_RECORD, follow_after=True,
                                      profile_after=target.profile)
            return _shown_or_stale(shown, fingerprint, preview_only)
        _, dropped = _save_record_only(store, record, follow=True, drop_lead_target=True)
        lines = [
            f"session {m8} now follows profile {target.profile}; its lineup already matches "
            f"(gen {generation})"
        ]
        if dropped:
            lines.append(_dropped_line(record))
        lines.append(NEXT_RECORDED)
        return Decision("record", text="\n".join(lines) + "\n", managed_id=mid, generation=generation)

    live_dir = scope.scope_dir(store.root, mid)
    classification: Classification | None = None
    if generation == 0:
        reasons: tuple[str, ...] = (REASON_GEN0,)
    elif not live_dir.is_dir():
        reasons = (REASON_NO_SCOPE,)
    else:
        classification = classify(record, lineup, live_dir, forced=forced, lcat=lcat)
        reasons = classification.reasons

    lineup_live = lineup
    lead_switch = None
    effort_only = False
    if classification is not None and classification.kind == "live" and (
        classification.lead_switch is not None or classification.effort_only
    ):
        # §7.5: agents apply live on the running lead (the scope states what runs).
        running = applied["lead"]
        document = copy.deepcopy(target.document or {})
        document["lead"] = {"model": running["key"], "effort": running["effort"]}
        evaluation = profile.evaluate(
            document, lcat, bindings=bindings, effective=eff, ad_hoc=target.profile is None
        )
        if evaluation.errors:
            reasons = (
                REASON_LEAD_NEEDS_AGENTS.format(
                    display=lineup.lead.binding.display, selector=lineup.lead.binding.selector
                ),
            )
        else:
            lineup_live = profile.keep_recorded_agent_class(evaluation.lineup, applied["agents"], lcat)
            lead_switch = classification.lead_switch
            effort_only = classification.effort_only

    new_lead = lineup.lead.binding
    lead_label = None
    if (new_lead.key, new_lead.effort) != (applied["lead"]["key"], applied["lead"]["effort"]):
        lead_label = _lead_label(lcat, new_lead.key, new_lead.effort)
    preview: list[str] = []
    if request.verb == "fallback":
        preview = _fallback_preview(lcat, record, request, target, lineup, reasons)
    fingerprint = _target_fingerprint(target, lineup, reasons or (EFFECT_LIVE,))
    if preview_only or (expected_target is not None and fingerprint != expected_target):
        if request.verb == "fallback":
            text = "\n".join([*preview, FALLBACK_PREVIEW_ONLY.format(provider=request.provider)])
            shown = Decision("show", text=text + "\n", managed_id=mid, generation=generation,
                             reasons=tuple(reasons))
        else:
            rows = [f"  {label}: {old} → {new}" for label, old, new in _binding_changes(lcat, applied, lineup)]
            rows += [f"! {finding.message}" for finding in lineup.warnings]
            shown = _preview_decision(record, request, rows or ["  no binding changes"],
                                      effect=EFFECT_RELAUNCH if reasons else EFFECT_LIVE,
                                      follow_after=target.follow, profile_after=target.profile, reasons=reasons)
        return _shown_or_stale(shown, fingerprint, preview_only)
    if reasons:
        decision = _relaunch(
            runtime,
            record,
            request,
            target,
            reasons,
            exec_ok=exec_ok,
            now=now,
            lead_label=lead_label,
        )
    else:
        assert classification is not None
        decision = _live_apply(
            runtime,
            record,
            target,
            lineup,
            lineup_live,
            lcat=lcat,
            lead_switch=lead_switch,
            effort_only=effort_only,
            origin=origin,
            now=now,
            verb=request.verb,
        )
    if request.verb == "fallback" and decision.kind != "exec":
        decision.text = _fallback_text(preview, decision, record)
    return decision


def _binding_changes(
    lcat: Any, applied: Mapping[str, Any], lineup: profile.ResolvedLineup,
) -> list[tuple[str, str, str]]:
    """``(slot, before, after)`` for every lead, agent and lead-provider change."""

    new_lead = {"key": lineup.lead.binding.key, "effort": lineup.lead.binding.effort}
    rows: list[tuple[str, str, str]] = []
    if _pair(applied["lead"]) != _pair(new_lead):
        rows.append(("lead", _label_of(lcat, applied["lead"]), _label_of(lcat, new_lead)))
    new_agents = lineup.applied_bindings()["agents"]
    for rid in catalog_mod.AGENT_ROLE_IDS:
        old, new = applied["agents"].get(rid), new_agents.get(rid)
        if _pair(old) != _pair(new):
            rows.append((rid, _label_of(lcat, old), _label_of(lcat, new)))
    fields = lineup.relaunch_fields()
    old_p = sorted(applied["lead_providers"]) if applied["lead_providers"] else None
    new_p = sorted(fields["lead_providers"]) if fields["lead_providers"] else None
    if old_p != new_p:
        rows.append(("lead providers", ",".join(old_p) if old_p else "any",
                     ",".join(new_p) if new_p else "any"))
    return rows


# ------------------------------------------------------------ previews

EFFECT_RECORD = "Effect: the record only — the applied lineup, the scope and the generation stay as they are"
EFFECT_LIVE = "Effect: LIVE — the agents change now in the running session"
EFFECT_RELAUNCH = "Effect: RELAUNCH — recorded as pending; it applies at the next resume"
PREVIEW_ONLY = "Preview only; nothing changed."
PREVIEW_STALE = "The session changed since this preview; nothing was applied. The current preview:"
DISCARD_PENDING = "Keep the current lineup — discard the pending change"


def _follow_label(follow: bool, profile_name: str | None) -> str:
    return f"on (profile {profile_name})" if follow and profile_name else "off"


def _preview_decision(
    record: Mapping[str, Any], request: Request, rows: Sequence[str], *, effect: str,
    follow_after: bool, profile_after: str | None, reasons: Sequence[str] = (),
) -> Decision:
    """The read-only preview of a write: binding changes, the follow state
    before and after, the pending change it drops or replaces, the effect
    (record only, LIVE or RELAUNCH) and its next step. Nothing is written."""

    title = DISCARD_PENDING if request.verb == "pin" and "pending" in record else f"/cm {request.text or request.verb}"
    lines = [f"Preview: {title}", *rows]
    before = _follow_label(bool(record["follow"]), record["profile"])
    after = _follow_label(follow_after, profile_after)
    if before != after:
        lines.append(f"  follow: {before} → {after}")
    pending = record.get("pending")
    if pending:
        why = "; ".join(pending.get("reasons") or ()) or "a relaunch change"
        fate = "replaced by this change" if effect == EFFECT_RELAUNCH else "dropped"
        lines.append(f"  pending change ({why}): {fate}")
    lines.append(effect)
    if reasons:
        lines.append(f"  why: {'; '.join(reasons)}")
    lines.append(PREVIEW_ONLY)
    return Decision("show", text="\n".join(lines) + "\n", managed_id=sessions.managed_id(record),
                    generation=int(record.get("lineup_generation", 0)), reasons=tuple(reasons))


def _target_fingerprint(target: Target, lineup: profile.ResolvedLineup | None, effect: Sequence[str]) -> str:
    """What a preview shows beyond the record identity: the resolved target
    (its document as read now, the profile and follow after it, a
    fallback's recipe), the lineup it evaluates to and its effect (record
    only, LIVE, or the relaunch reasons). A profile or binding edited while
    a preview waits for its confirmation changes it."""

    evaluated = None
    if lineup is not None:
        evaluated = {
            "applied": lineup.applied_bindings(), "native_agents": dict(lineup.native_agents),
            "workflows": lineup.workflows, "lead_providers": list(lineup.lead_providers or ()),
            "settings_overrides": dict(lineup.settings_overrides),
        }
    return strict_json.bundle_digest({
        "kind": target.kind, "profile": target.profile, "follow": target.follow, "recipe": target.recipe,
        "document": target.document, "lineup": evaluated, "effect": list(effect),
    })


def _shown_or_stale(shown: Decision, fingerprint: str, preview_only: bool) -> Decision:
    """A preview decision carrying its fingerprint, or (an apply whose
    evaluation differs from the confirmed preview) the stale refusal with
    that preview."""

    shown.fingerprint = fingerprint
    if preview_only:
        return shown
    return Decision("stale", text=f"{PREVIEW_STALE}\n{shown.text}", managed_id=shown.managed_id,
                    generation=shown.generation, reasons=shown.reasons, fingerprint=fingerprint)


def record_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """What a preview was decided on: an apply whose record no longer has
    this identity is refused with a refreshed preview, never applied
    silently."""

    pending = record.get("pending")
    return (
        record.get("applied_hash"), record.get("lineup_generation"), bool(record.get("follow")),
        record.get("profile"), strict_json.bundle_digest(pending) if pending else None,
        record.get("launch_epoch"), record.get("mutation_token"),
    )


@dataclass(frozen=True)
class Preview:
    """A read-only preview of one request and the record identity it saw."""

    managed_id: str
    request: Request
    text: str
    identity: tuple[Any, ...]
    # The resolved target and consequences shown (:func:`_target_fingerprint`).
    fingerprint: str | None = None


def preview(runtime: Any, record: Mapping[str, Any], request: Request, *, now: str | None = None) -> Preview:
    """The read-only preview of ``request`` on ``record`` (no lock, no
    write): what a T, F, pin or fallback action shows before it applies."""

    if request.verb not in lineup_files.CM_VERB_NAMES or not request.writes:
        raise LineupRefusal(f"lineup: {request.verb} changes nothing to preview")
    decision = decide_locked(runtime, dict(record), request, forced=False, exec_ok=False, now=now,
                             preview_only=True)
    return Preview(sessions.managed_id(record), request, decision.text, record_identity(record),
                   decision.fingerprint)


def apply_preview(
    runtime: Any, shown: Preview, *, interactive: bool = False, relaunch_exec: RelaunchExec | None = None,
    now: str | None = None,
) -> ApplyResult:
    """Apply what ``shown`` previewed: re-decided under the served-change
    phase and the session's lifecycle lock. When the record changed since
    the preview (another apply, a resume, a hook), or the target it
    resolves to now differs from the one shown (a profile or binding edited
    meanwhile, another effect), nothing is written and the result carries
    the refreshed preview (exit 1); otherwise the evaluation that passed the
    comparison is applied."""

    mid = shown.managed_id
    exec_ok = interactive and relaunch_exec is not None and not inside_session(runtime.environ, mid)

    def body(current: dict[str, Any]) -> Decision:
        if record_identity(current) != shown.identity:
            fresh = decide_locked(runtime, current, shown.request, forced=False, exec_ok=False, now=now,
                                  preview_only=True)
            return Decision("stale", text=f"{PREVIEW_STALE}\n{fresh.text}", managed_id=mid)
        return decide_locked(runtime, current, shown.request, forced=False, exec_ok=exec_ok, now=now,
                             expected_target=shown.fingerprint)

    decision = locked_decision(runtime, mid, body)
    if decision.kind == "stale":
        return ApplyResult(decision.text, 1, decision)
    if decision.kind == "exec":
        assert relaunch_exec is not None and decision.target is not None and decision.sample is not None
        return ApplyResult("", int(relaunch_exec(mid, decision.target, decision.sample) or 0), decision)
    return ApplyResult(decision.text, 0, decision)


def _fallback_preview(
    lcat: Any,
    record: Mapping[str, Any],
    request: Request,
    target: Target,
    lineup: profile.ResolvedLineup,
    reasons: Sequence[str],
) -> list[str]:
    """The fallback preview: target, per-function changes, readiness warnings, effect."""

    applied = record["applied"]
    lines = [f"Fallback target: {request.provider} · profile {target.recipe}"]
    rows = _binding_changes(lcat, applied, lineup)
    lines += [f"  {label}: {old} → {new}" for label, old, new in rows] or ["  no binding changes"]
    lines += [f"! {finding.message}" for finding in lineup.warnings]
    lines.append(f"Effect: {'RELAUNCH' if reasons else 'LIVE'}")
    if reasons:
        lines.append(f"  why: {'; '.join(reasons)}")
    lines.append(FALLBACK_EXISTING)
    return lines


def _fallback_text(preview: Sequence[str], decision: "Decision", record: Mapping[str, Any]) -> str:
    """Preview, one of the three outcomes, the follow remedy, then the one next step."""

    lines = list(preview)
    if decision.kind == "live":
        lines += [FALLBACK_LIVE[0].format(generation=decision.generation), FALLBACK_LIVE[1]]
    elif decision.kind == "pending":
        lines += list(FALLBACK_PENDING)
    else:
        lines.append(FALLBACK_UNCHANGED)
    if record["follow"] and record["profile"] is not None:
        when = "now" if decision.kind != "pending" else "when the change applies"
        lines.append(
            f"pinned {when}: the session stops following profile {record['profile']} — "
            f"/cm follow returns to it"
        )
    rendered = decision.text.rstrip("\n").splitlines()
    lines.append(rendered[-1] if rendered else NEXT_NONE)
    return "\n".join(lines) + "\n"


def _dropped_line(record: Mapping[str, Any]) -> str:
    reasons = "; ".join((record.get("pending") or {}).get("reasons") or ())
    return f"dropped the pending relaunch change ({reasons})"


def _relaunch(
    runtime: Any,
    record: dict[str, Any],
    request: Request,
    target: Target,
    reasons: Sequence[str],
    *,
    exec_ok: bool,
    now: str,
    lead_label: str | None = None,
) -> Decision:
    """§7.7: ``pending`` inside phase A, or a sample for the exec branch.

    ``lead_label`` (the requested lead when it differs from the running one)
    makes the next-step line name it.
    """

    mid = sessions.managed_id(record)
    if exec_ok:
        return Decision(
            "exec",
            managed_id=mid,
            reasons=tuple(reasons),
            sample=(
                record.get("launch_epoch", 0),
                record.get("mutation_token"),
                record.get("applied_hash"),
                record.get("lineup_generation"),
            ),
            target=target,
        )
    pending = {
        "requested_at": now,
        "kind": "profile" if target.kind == "profile" else "lineup",
        "profile": target.profile,
        "follow": bool(target.follow),
        "document": _pending_document(target),
        "reasons": _truncate_reasons(reasons),
    }
    updated = copy.deepcopy(record)
    updated["pending"] = pending
    updated["mutation_token"] = sessions.new_mutation_token()
    runtime.session_store.save(updated)
    return Decision(
        "pending",
        text=_output_pending(mid, reasons, lead_label=lead_label),
        managed_id=mid,
        generation=record.get("lineup_generation"),
        reasons=tuple(reasons),
    )


def _live_apply(
    runtime: Any,
    record: dict[str, Any],
    target: Target,
    lineup: profile.ResolvedLineup,
    lineup_live: profile.ResolvedLineup,
    *,
    lcat: Any,
    lead_switch: Mapping[str, Any] | None,
    effort_only: bool,
    origin: str,
    now: str,
    verb: str,
) -> Decision:
    """§7.6 steps 5-9 (the classification is LIVE)."""

    store = runtime.session_store
    mid = sessions.managed_id(record)
    m8 = _m8(mid)
    applied = record["applied"]
    live_dir = scope.scope_dir(store.root, mid)
    new_bindings = lineup_live.applied_bindings()["agents"]
    old_agents = applied["agents"]

    # 5. The roster gate for newly bound ids (launch-time skill collisions are not re-checked).
    new_ids = set(new_bindings) - set(old_agents)
    if new_ids:
        hits = scope.find_cm_collisions(record["cwd"], (), frozenset(new_ids))
        if hits:
            raise LineupRefusal(
                R16.format(
                    paths=", ".join(f"{path} ({name})" for path, name in hits),
                    ids=", ".join(sorted({name for _path, name in hits})),
                )
            )
    # 5b. credentials of newly bound or rebound agents.
    problems = []
    for rid in catalog_mod.AGENT_ROLE_IDS:
        binding = new_bindings.get(rid)
        if binding is None:
            continue
        old = old_agents.get(rid)
        if old is not None and old.get("key") == binding["key"]:
            continue
        problem = _secret_problem(runtime, binding["key"])
        if problem is not None:
            entry = lcat.lines.get(binding["key"]) or {}
            hint = problem.split("): ", 1)[1] if "): " in problem else problem
            problems.append(
                R18.format(
                    label=profile.label(rid),
                    key=binding["key"],
                    provider=entry.get("provider", "?"),
                    hint=hint,
                )
            )
    if problems:
        raise LineupRefusal("\n".join(problems))

    # 6. generation fencing.
    from .launch import disk_generation  # local: launch is heavy

    generation = int(record["lineup_generation"])
    disk_n = disk_generation(live_dir)
    new_generation = max(generation, disk_n) + 1

    applied2 = {**copy.deepcopy(applied), "agents": copy.deepcopy(new_bindings)}
    if lineup.primary_provider is not None:
        applied2["primary_provider"] = lineup.primary_provider
    else:
        applied2.pop("primary_provider", None)
    new_record = sessions.with_applied(record, applied2)
    new_record["profile"] = target.profile
    new_record["follow"] = bool(target.follow)
    target_lead = lineup.lead.binding
    if lead_switch is not None or effort_only:
        new_record["lead_target"] = {
            "key": target_lead.key,
            "effort": target_lead.effort,
            "selector": target_lead.selector,
        }
    elif verb not in ("set", "unset"):
        # profile/follow/direct whose lead equals the running one: nothing left to
        # switch; set/unset keep an existing one.
        new_record.pop("lead_target", None)
    new_record["scope_lead"] = sessions.lead_ref(applied["lead"])
    had_pending = "pending" in new_record
    new_record.pop("pending", None)

    # 7. compile the non-launch-time files exactly as converge would:
    # first at the current generation (does anything in the scope move?),
    # then at the fenced next one.
    same, why = _compile_live(
        runtime, {**new_record, "lineup_generation": generation}, live_dir
    )
    if same is None:
        return _relaunch(
            runtime, record, Request("follow"), target, (str(why),), exec_ok=False, now=now
        )
    live_agents = _agent_relpaths(live_dir)
    changes = _change_rows(lcat, old_agents, new_bindings)
    extra = _extra_lines(record, new_record, lcat, lead_switch, effort_only, mid, had_pending)
    unchanged_scope = set(live_agents) == set(same.agent_files) and all(
        _read_or_none(live_agents[rel]) == data for rel, data in same.agent_files.items()
    ) and _read_or_none(live_dir / lineup_files.LINEUP_MD) == same.other_files[
        lineup_files.LINEUP_MD
    ]
    if unchanged_scope:
        # Nothing in the scope moves: a record-only change (or nothing at all).
        record_changed = {
            k: new_record.get(k) for k in ("profile", "follow", "lead_target", "pending")
        } != {k: record.get(k) for k in ("profile", "follow", "lead_target", "pending")}
        if record_changed or applied2 != applied:
            unchanged = {**new_record, "lineup_generation": generation}
            unchanged["scope_lead"] = record.get("scope_lead", new_record["scope_lead"])
            unchanged["mutation_token"] = sessions.new_mutation_token()
            store.save(unchanged)
        lines = [f"session {m8}: the lineup already matches (gen {generation}); no scope change"]
        lines += _extra_lines(record, new_record, lcat, None, False, mid, had_pending)
        label = _lead_label(lcat, target_lead.key, target_lead.effort)
        if lead_switch is not None:
            row = lead_switch.get("label") or lead_switch.get("display") or target_lead.key
            lines.append(NEXT_LEAD_NOW.format(label=label, row=row))
        elif effort_only:
            lines.append(NEXT_LEAD_RESUME.format(label=label, mid=mid))
        elif record_changed or applied2 != applied:
            lines.append(NEXT_RECORDED)
        else:
            lines.append(NEXT_NONE)
        return Decision("record", text="\n".join(lines) + "\n", managed_id=mid, generation=generation)

    plan, why = _compile_live(
        runtime, {**new_record, "lineup_generation": new_generation}, live_dir
    )
    if plan is None:
        return _relaunch(
            runtime, record, Request("follow"), target, (str(why),), exec_ok=False, now=now
        )
    # 8. file diff against the live scope.
    new_files = sorted(rel for rel in plan.agent_files if rel not in live_agents)
    changed_files = sorted(
        rel
        for rel in plan.agent_files
        if rel in live_agents and _read_or_none(live_agents[rel]) != plan.agent_files[rel]
    )
    removed_files = sorted(rel for rel in live_agents if rel not in plan.agent_files)
    lineup_md = plan.other_files[lineup_files.LINEUP_MD]
    gen_bytes = plan.other_files[lineup_files.LINEUP_GEN]
    gen_line = gen_bytes.decode("ascii").rstrip("\n")
    try:
        agents_dir = live_dir / ".claude" / "agents"
        if new_files:
            state.ensure_private_dir(live_dir / ".claude")
            state.ensure_private_dir(agents_dir)
        for rel in new_files:  # 9.1
            state.atomic_write(live_dir / rel, plan.agent_files[rel])
            _checkpoint("9.1", rel)
        _checkpoint("9.1-done")
        for rel in changed_files:  # 9.2
            state.atomic_write(live_dir / rel, plan.agent_files[rel])
        _checkpoint("9.2")
        for rel in removed_files:  # 9.3
            state.remove_private(live_dir / rel)
        if removed_files and not plan.agent_files:
            _remove_empty_dir(agents_dir)
        _checkpoint("9.3")
        state.atomic_write(live_dir / lineup_files.LINEUP_MD, lineup_md)  # 9.4
        _checkpoint("9.4")
        state.atomic_write(live_dir / lineup_files.LINEUP_GEN, gen_bytes)  # 9.5
        _checkpoint("9.5")
        try:  # 9.6
            hooks.append_log_line(
                store.root,
                mid,
                {
                    "event": "apply",
                    "lineup_gen": gen_line,
                    "time": now,
                    "label": lineup_log.binding_label(new_generation),
                    "changes": {
                        rid: {
                            "from": (old_agents.get(rid) or {}).get("selector"),
                            "to": (new_bindings.get(rid) or {}).get("selector"),
                        }
                        for rid in catalog_mod.AGENT_ROLE_IDS
                        if _pair(old_agents.get(rid)) != _pair(new_bindings.get(rid))
                    },
                },
            )
        except FileNotFoundError:
            pass
        _checkpoint("9.6")
        committed = {
            **new_record,
            "lineup_generation": new_generation,
            "mutation_token": sessions.new_mutation_token(),
        }
        _checkpoint("9.7-before")
        store.save(committed)  # 9.7: the record last
    except LineupRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - every failed apply needs repair (no rollback)
        raise LineupRefusal(R17.format(exc=exc, G=generation, mid=mid)) from exc
    # Agents changed → /reload-plugins (and /model when the lead
    # switches too); only lineup.md restated → the lead line, or recorded.
    if changes:
        next_line = NEXT_RELOAD_AND_MODEL if lead_switch is not None else NEXT_RELOAD
    elif lead_switch is not None:
        extra = _extra_lines(record, new_record, lcat, None, effort_only, mid, had_pending)
        row = lead_switch.get("label") or lead_switch.get("display") or target_lead.key
        next_line = NEXT_LEAD_NOW.format(
            label=_lead_label(lcat, target_lead.key, target_lead.effort), row=row
        )
    else:
        next_line = NEXT_RECORDED
    return Decision(
        "live",
        text=_output_live(
            lcat, record, generation=new_generation, changes=changes, extra=extra,
            next_line=next_line,
        ),
        managed_id=mid,
        generation=new_generation,
        lead_switch=lead_switch,
        pending_dropped=had_pending,
    )


def _read_or_none(path: Path) -> bytes | None:
    try:
        return state.read_private(path)
    except (OSError, state.StateError):
        return None


def _remove_empty_dir(path: Path) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _change_rows(
    lcat: Any, old_agents: Mapping[str, Any], new_agents: Mapping[str, Any]
) -> list[tuple[str, str, str]]:
    rows = []
    for rid in catalog_mod.AGENT_ROLE_IDS:
        old, new = old_agents.get(rid), new_agents.get(rid)
        if _pair(old) != _pair(new):
            rows.append((profile.label(rid), _label_of(lcat, old), _label_of(lcat, new)))
    return rows


def _extra_lines(
    record: Mapping[str, Any],
    new_record: Mapping[str, Any],
    lcat: Any,
    lead_switch: Mapping[str, Any] | None,
    effort_only: bool,
    mid: str,
    had_pending: bool,
) -> list[str]:
    lines: list[str] = []
    if new_record["follow"] and (
        not record["follow"] or record["profile"] != new_record["profile"]
    ):
        lines.append(f"session now follows profile {new_record['profile']}")
    elif record["follow"] and not new_record["follow"]:
        lines.append("session is now pinned (follow off)")
    if had_pending:
        lines.append(_dropped_line(record))
    target = new_record.get("lead_target")
    if lead_switch is not None and target is not None:
        lines.append(
            f"switch the lead with /model → {lead_switch.get('display') or target['key']} · "
            f"{target['effort']} ({target['selector']}); press s (this session only)"
        )
    elif effort_only and target is not None:
        lines.append(
            f"lead effort {record['applied']['lead']['effort']} → {target['effort']} applies "
            f"at the next resume (claude-multi -r {mid})"
        )
    return lines


# ------------------------------------------------------------- §7.9 show


def workflow_role(cat: Any, eff: settings_mod.Effective,
                  lineup: profile.ResolvedLineup) -> profile.RoleWindow | None:
    """The workflow default's window under the lineup's policy (None: off or unresolvable)."""

    try:
        return scope.workflow_default_window(cat, eff, policy=lineup.policy or profile.default_policy(eff))
    except scope.ScopeError:
        return None


def show_text(runtime: Any, record: Mapping[str, Any]) -> str:
    """``lineup show`` (§7.8/§7.9); never raises on a lead unknown to the catalog."""

    mid = sessions.managed_id(dict(record))
    lcat = runtime.lineup_catalog()
    if record.get("version") != sessions.RECORD_VERSION:
        key, _selector = sessions.lead_identity(dict(record))
        summary = sessions.record_summary(dict(record))
        lines = [
            f"claude-multi · legacy record (version {record.get('version')}) · "
            f"{summary.profile_label} · lead {key}",
            SHOW_2X_LINE.format(mid=mid),
            *profiles_block(runtime, lcat, current=None),
            HELP_LINE,
            *MODEL_HELP_LINES,
        ]
        return "\n".join(lines) + "\n"
    block = profiles_block(runtime, lcat, current=record["profile"])
    if record["profile"] is None:
        header = "profile (ad-hoc direct)"
    else:
        header = f"profile {record['profile']} ({'follows' if record['follow'] else 'pinned'})"
    generation = record["lineup_generation"]
    needs_choice = None
    if sessions.lead_needs_choice(dict(record), lcat):
        needs_choice = sessions.lead_choice_notice(dict(record), lcat)
    drift: list[str] = []
    try:
        drift = settings_mod.drift(record["applied"]["settings"], runtime.current_effective())
    except Exception:  # noqa: BLE001 - drift is informational only
        drift = []
    text: str
    lineup = None
    workflow = None
    errors: tuple[str, ...] = ()
    try:
        eff = settings_mod.effective_from_snapshot(
            record["applied"]["settings"], window_ceiling=sessions.recorded_window(record))
        gate = _record_gate(lcat, record, eff)
        evaluation = profile.evaluate(
            sessions.applied_document(dict(record)),
            gate,
            bindings=None,
            effective=eff,
            ad_hoc=record["profile"] is None,
        )
        lineup = evaluation.lineup
        if lineup is not None:
            # The running session's agent classes.
            lineup = profile.keep_recorded_agent_class(lineup, record["applied"]["agents"], lcat)
            workflow = workflow_role(gate, eff, lineup)
        errors = evaluation.errors
    except (settings_mod.SettingsError, profile.ProfileError) as exc:
        errors = (str(exc),)
    if lineup is not None:
        text = render_text(
            lineup,
            header=header,
            generation=generation,
            pending=record.get("pending"),
            lead_target=record.get("lead_target"),
            needs_choice=needs_choice,
            drift=drift,
            managed_id=mid,
            cat=lcat,
            before_help=block,
            workflow_default=workflow,
        )
    else:
        lead = record["applied"]["lead"]
        lines = [
            f"claude-multi · {header} · lineup gen {generation or '-'} · lead "
            f"{_lead_label(lcat, lead['key'], lead['effort'])}"
        ]
        lines += [f"! {error}" for error in errors]
        if record.get("pending"):
            reasons = "; ".join(record["pending"].get("reasons") or ())
            lines.append(
                f"pending relaunch change: {reasons} — applies at the next resume "
                f"(claude-multi -r {mid})"
            )
        if needs_choice:
            lines.append(
                f"needs a lead choice: {needs_choice} — claude-multi -r {mid} (interactive) "
                "or /cm profile <name>"
            )
        for line in drift:
            lines.append(f"settings changed since launch: {line} — applies at the next resume")
        lines.extend(block)
        lines.append(HELP_LINE)
        lines.extend(MODEL_HELP_LINES)
        text = "\n".join(lines) + "\n"
    if generation == 0:
        text += SHOW_2X_LINE.format(mid=mid) + "\n"
    return text


def gateway_health_line(runtime: Any) -> str:
    """One token-free loopback health line, with the backend's start command
    when the gateway does not answer (``service.hint``)."""

    from . import endpoint, service

    if getattr(runtime, "endpoint_error", None) is not None:
        error = runtime.endpoint_error
        return f"gateway: {error} — {error.remedy}" if error.remedy else f"gateway: {error}"
    configured = endpoint.gateway_endpoint(runtime.catalog.docs["gateway"])
    status: int | None = None
    # A runtime with an injected served set but no health seam is a fixture:
    # it never reaches a live loopback port.
    if runtime.health_get is not None or getattr(runtime, "served_models_callback", None) is None:
        getter = runtime.health_get or service._health_get
        try:
            status = getter(configured.base_url, configured.health_path)
        except Exception:  # noqa: BLE001 - a refused or failed probe is "not answering"
            status = None
    if status == 200:
        return f"gateway: answering at {configured.base_url}"
    state_text = "not answering" if status is None else f"answered HTTP {status}"
    return (f"gateway: {state_text} at {configured.base_url} — start it: "
            f"{service.hint('start', home=runtime.home)} (details: {service.hint('status', home=runtime.home)})")


PROVIDER_ERRORS_HOURS = 24
PROVIDER_ERRORS_CODES = 3  # status codes named per provider; the rest are counted only
PROVIDER_ERRORS_SCOPE = "gateway-wide, not only this session"
PROVIDER_ERRORS_NONE = "provider errors, last {hours} h: none observed for {providers} ({scope}; {coverage})"
PROVIDER_ERRORS_HEAD = "provider errors, last {hours} h ({scope}; {coverage}):"
PROVIDER_ERRORS_LINE = "  {provider} ({slots}): {count} failed — {codes}; last {last} — {remedy}"
PROVIDER_ERRORS_REST = "  none observed for {providers}"
# One size whatever the profiles are: the providers with a fallback profile
# are never listed into the row, so none can be cut into a command.
PROVIDER_ERRORS_FALLBACK = "/cm fallback <provider> (see /cm profiles)"
PROVIDER_ERRORS_UNAVAILABLE = ("provider errors: unavailable — the gateway's log cannot be read here, so "
                               "nothing is counted (claude-multi doctor checks the gateway)")


def _log_coverage(events: Any) -> str:
    """The bounded log read the counts come from: a request outside it is
    never counted, so a partial read says how far back it reached."""

    text = f"log read {events.source_coverage}"
    if events.timed_out:
        text += ", timed out"
    if events.source_coverage != "bounded" and events.oldest_at is not None:
        text += f", back to {events.oldest_at.astimezone():%Y-%m-%d %H:%M}"
    return text


def provider_error_lines(runtime: Any, record: Mapping[str, Any], events: Any, *, start: Any, end: Any) -> list[str]:
    """``/cm show``: the failed requests of the last day per provider this
    session binds (``events`` the bounded gateway-log observation doctor and
    Sessions V read, None or ``unavailable`` when it cannot be read: then
    nothing is counted, never zero). A request counts for a provider when its
    selector is one this session binds; the counts are gateway-wide. Each
    provider with errors names the fitting remedy: ``/cm fallback
    <provider>`` (with ``/cm profiles``) when a profile is a fallback for
    another provider, ``/cm set`` when agents run on it, else the profiles
    to switch to. Bounded: the providers this session binds, three status
    codes each, and a remedy of one size."""

    if record.get("version") != sessions.RECORD_VERSION:
        return []
    if events is None or getattr(events, "coverage", "unavailable") == "unavailable":
        return [PROVIDER_ERRORS_UNAVAILABLE]
    lcat = runtime.lineup_catalog()
    applied = record.get("applied") or {}
    bound = applied.get("agents") or {}
    slots: dict[str, list[str]] = {}
    with_agents: set[str] = set()
    by_selector: dict[str, str] = {}
    for slot, binding in [(profile.LEAD_ROLE, applied.get("lead")),
                          *((rid, bound[rid]) for rid in catalog_mod.AGENT_ROLE_IDS if rid in bound)]:
        if not isinstance(binding, Mapping):
            continue
        entry = lcat.lines.get(binding.get("key")) or lcat.retired.get(binding.get("key"))
        provider = entry.get("provider") if isinstance(entry, Mapping) else None
        if not isinstance(provider, str):
            continue
        slots.setdefault(provider, []).append(profile.label(slot))
        if slot != profile.LEAD_ROLE:
            with_agents.add(provider)
        if isinstance(binding.get("selector"), str):
            by_selector[lineup_files.normalize_model(binding["selector"])] = provider
    if not slots:
        return []  # no binding names a known model (show says what needs a choice)
    failed: dict[str, dict[int, int]] = {}
    latest: dict[str, Any] = {}
    for request in events.requests:
        if request.endpoint != "inference" or request.status < 400 or request.timestamp is None:
            continue
        if not start <= request.timestamp <= end or not request.selector:
            continue
        provider = by_selector.get(lineup_files.normalize_model(request.selector))
        if provider is None:
            continue
        codes = failed.setdefault(provider, {})
        codes[request.status] = codes.get(request.status, 0) + 1
        latest[provider] = max(latest.get(provider, request.timestamp), request.timestamp)
    hours, coverage = PROVIDER_ERRORS_HOURS, _log_coverage(events)
    quiet = [provider for provider in sorted(slots) if provider not in failed]
    if not failed:
        return [PROVIDER_ERRORS_NONE.format(hours=hours, providers=", ".join(quiet), scope=PROVIDER_ERRORS_SCOPE,
                                            coverage=coverage)]
    fallbacks = fallback_providers(runtime)
    lines = [PROVIDER_ERRORS_HEAD.format(hours=hours, scope=PROVIDER_ERRORS_SCOPE, coverage=coverage)]
    for provider in sorted(failed):
        codes = failed[provider]
        ranked = sorted(codes.items(), key=lambda item: (-item[1], item[0]))
        named = [f"HTTP {code} ×{count}" for code, count in ranked[:PROVIDER_ERRORS_CODES]]
        if len(ranked) > PROVIDER_ERRORS_CODES:
            named.append(f"{sum(count for _code, count in ranked[PROVIDER_ERRORS_CODES:])} other")
        remedies = []
        if any(name != provider for name in fallbacks):
            remedies.append(PROVIDER_ERRORS_FALLBACK)
        if provider in with_agents:
            remedies.append("/cm set <agent>=<model>")
        if not remedies:
            remedies.append("/cm profiles, then /cm profile <name>")
        lines.append(PROVIDER_ERRORS_LINE.format(
            provider=provider, slots=", ".join(slots[provider]), count=sum(codes.values()),
            codes=", ".join(named), last=f"{latest[provider].astimezone():%H:%M}", remedy=" or ".join(remedies)))
    if quiet:
        lines.append(PROVIDER_ERRORS_REST.format(providers=", ".join(quiet)))
    return lines


def _with_gateway_line(text: str, line: str) -> str:
    """``text`` with ``line`` after its first line (the help block stays last)."""

    head, separator, rest = text.partition("\n")
    return f"{head}\n{line}\n{rest}" if separator else f"{text}\n{line}\n"


# ------------------------------------------------- profiles, review


def _spec_label(lcat: Any, spec: Any, bindings: Mapping[str, Any]) -> str:
    """``display · effort`` of a profile slot ``{"model", "effort"}`` or ``{"use"}``."""

    if not isinstance(spec, Mapping):
        return "?"
    if isinstance(spec.get("use"), str):
        named = bindings.get(spec["use"])
        if isinstance(named, Mapping) and isinstance(named.get("model"), str):
            return _lead_label(lcat, named["model"], str(named.get("effort") or "?"))
        return f"binding {spec['use']}"
    if isinstance(spec.get("model"), str):
        return _lead_label(lcat, spec["model"], str(spec.get("effort") or "?"))
    return "?"


def _one_line_purpose(text: Any) -> str:
    purpose = " ".join(str(text or "").split())
    if len(purpose) > _PURPOSE_MAX:
        purpose = purpose[: _PURPOSE_MAX - 1].rstrip() + "…"
    return purpose


def profiles_block(runtime: Any, lcat: Any, *, current: str | None) -> list[str]:
    """One line per available profile, then the switch line.

    Name, lead model and effort, the one-line purpose (the profile's
    ``description``) and ``*`` on the session's current profile. Profile
    files are read lock-free (as ``profile N`` does); an unreadable one is
    listed as such. Read-only.
    """

    try:
        names = runtime.profiles.names()
    except (profile.ProfileError, OSError):
        names = []
    try:
        bindings = runtime.bindings.bindings()
    except (profile.BindingError, OSError):
        bindings = {}
    width = min(max((len(name) for name in names), default=0), 24)
    lines = [PROFILES_HEAD]
    for name in names:
        mark = "*" if name == current else " "
        try:
            document = runtime.profiles.load(name)
        except profile.ProfileError:
            lines.append(f"{mark} {name:<{width}}  (unreadable; claude-multi doctor)")
            continue
        lead = _spec_label(lcat, document.get("lead"), bindings)
        purpose = _one_line_purpose(document.get("description"))
        lines.append(f"{mark} {name:<{width}}  {lead}" + (f" — {purpose}" if purpose else ""))
    lines.append(PROFILES_SWITCH)
    return lines


def profiles_text(runtime: Any, record: Mapping[str, Any]) -> str:
    """``/cm profiles``: the profiles block alone."""

    current = record.get("profile") if record.get("version") == sessions.RECORD_VERSION else None
    return "\n".join(profiles_block(runtime, runtime.lineup_catalog(), current=current)) + "\n"


def _record_lineup(runtime: Any, record: Mapping[str, Any]) -> profile.ResolvedLineup:
    """The running session's lineup as ``show`` evaluates it (read-only)."""

    lcat = runtime.lineup_catalog()
    eff = _record_eff(record)
    evaluation = profile.evaluate(
        sessions.applied_document(dict(record)),
        _record_gate(lcat, record, eff),
        bindings=None,
        effective=eff,
        ad_hoc=record["profile"] is None,
    )
    if evaluation.lineup is None:
        raise LineupRefusal(R14 + "".join(f"\n- {error}" for error in evaluation.errors))
    return profile.keep_recorded_agent_class(evaluation.lineup, record["applied"]["agents"], lcat)


def review_text(runtime: Any, record: Mapping[str, Any], request: Request) -> str:
    """``/cm review [normal|high-stakes] [<range>]``.

    Read-only: it writes nothing and runs nothing. The text is the lead's
    brief (the skill's dynamic context reaches only the lead): the diff,
    the reviewers chosen by this session's routing table (the exact rows of
    ``lineup.md``), report-only rules and ranked, verified findings. High
    stakes run both reviewer grades in parallel and the lead arbitrates.
    """

    mid = sessions.managed_id(dict(record))
    if record.get("version") != sessions.RECORD_VERSION:
        rid = sessions.runtime_session_id(dict(record))
        raise LineupRefusal(R9.format(m8=_m8(mid), v=record.get("version"), mid=mid, rid=rid))
    lineup = _record_lineup(runtime, record)
    stakes = request.stakes or "normal"
    diff = f"git diff {request.range}" if request.range else "git diff HEAD (the working tree against HEAD)"
    lead_row = next((row for row in lineup.routing.rows if row.author == profile.LEAD_ROLE), None)
    bound = [rid for rid in profile.REVIEWER_IDS if rid in lineup.agents]
    lines = [
        f"claude-multi · /cm review {stakes} · lineup gen {record['lineup_generation']} · "
        f"range: {request.range or 'working tree against HEAD'}",
        "Review routing (this session's lineup.md):",
        *routing_rows(lineup),
        "Lead brief — a report-only review; the operator decides on fixes:",
        f"1. Diff: {diff}. Read it yourself first; do not change the tree.",
    ]
    if not bound:
        lines.append(
            "2. No cm reviewer is bound in this lineup: review the diff yourself, read-only, "
            "and say that no independent reviewer ran."
        )
    elif stakes == "high-stakes":
        names = " and ".join(bound)
        both = "both reviewer grades" if len(bound) == 2 else "the only bound reviewer grade"
        lines.append(
            f"2. High stakes: spawn {names} in parallel ({both}), each with a self-contained "
            "brief naming the diff; then arbitrate their findings yourself."
        )
    else:
        cell = lead_row.normal if lead_row is not None else None
        chosen = cell.reviewer if cell is not None and cell.reviewer in bound else bound[0]
        lines.append(
            f"2. Normal: spawn {chosen} (the lead's normal-change cell of the routing table) "
            "with a self-contained brief naming the diff."
        )
    lines += [
        "3. Reviewers are report-only: no edits, no fix pass, no commits. Do not edit either.",
        "4. Return ranked, verified findings, most severe first, each with file:line, the "
        "failure scenario and a confidence; drop what you could not verify.",
        "5. Never use /code-review, /simplify or another skill that launches agents outside "
        "the lineup (they are denied to the model in managed sessions); never substitute a "
        "generic agent for a missing cm reviewer.",
    ]
    return "\n".join(lines) + "\n"


def routing_rows(lineup: profile.ResolvedLineup) -> list[str]:
    """The routing table rows ``lineup.md`` states (``scope.routing_table_lines``)."""

    return ["  " + line for line in scope.routing_table_lines(lineup)]


# ------------------------------------------------------------- §7.6 apply


@dataclass(frozen=True)
class ApplyResult:
    text: str
    exit_code: int = 0
    decision: Decision | None = None


RelaunchExec = Callable[[str, Target, tuple[Any, Any, Any, Any]], int]


def _runtime_home(runtime: Any) -> Path:
    home = getattr(runtime, "home", None)
    if home is not None:
        return Path(home)
    return Path(runtime.environ["HOME"])


def served_phase(runtime: Any) -> Any:
    """One served-change phase for ``runtime`` (migration guard -> barrier),
    with the bounded wait launch uses."""

    return sessions.served_change_phase(
        runtime.session_store.root, _runtime_home(runtime),
        timeout=getattr(runtime, "served_barrier_timeout", state.SERVED_BARRIER_TIMEOUT),
    )


def locked_decision(
    runtime: Any,
    mid: str,
    body: Callable[[dict[str, Any]], Decision],
    *,
    barrier: state.BarrierToken | None = None,
) -> Decision:
    """Run ``body`` on ``mid`` under one served-change phase + lifecycle lock.

    The phase is the shared migration hold then the
    gateway barrier (bounded wait; R27 on timeout), taken here once and
    released on return — before any relaunch confirm or exec. With a held
    ``barrier`` (propagation inside an outer phase) it is asserted and only
    the lifecycle lock is taken.
    """

    store = runtime.session_store
    if barrier is not None:
        state.require_barrier(barrier, root=store.root)
        lock = store.lifecycle_lock(mid)
        lock.acquire(blocking=True)
        try:
            return body(store.load(mid))
        finally:
            lock.release()
    try:
        with served_phase(runtime):
            lock = store.lifecycle_lock(mid)
            lock.acquire(blocking=True)
            try:
                return body(store.load(mid))
            finally:
                lock.release()
    except state.BarrierBusyError as exc:
        raise LineupRefusal(R27) from exc


def inside_session(environ: Mapping[str, str], mid: str) -> bool:
    """Whether the request runs inside the target session (the launcher's in-session sentinels)."""

    return any(environ.get(name) == mid for name in _INSIDE_ENV)


def apply(
    runtime: Any,
    args: LineupArgs,
    *,
    interactive: bool,
    relaunch_exec: RelaunchExec | None = None,
    now: str | None = None,
    quota_text: Callable[[], tuple[int, str]] | None = None,
    provider_errors: Callable[[Mapping[str, Any]], Sequence[str]] | None = None,
) -> ApplyResult:
    """One ``lineup`` request (§7.2-§7.7); refusals raise :class:`LineupRefusal`.

    ``quota`` is a read-only wrapper: ``quota_text`` is the
    command adapter's call into the one quota collector and formatter (no
    lock, no write, no session state). ``provider_errors(record)`` is the
    adapter's read of the recent provider errors ``show`` lists below the
    gateway line (:func:`provider_error_lines`)."""

    if args.request.verb == "quota":
        if quota_text is None:
            raise LineupRefusal("lineup: quota is not available on this surface; run claude-multi quota")
        code, text = quota_text()
        return ApplyResult(text, 0 if args.skill_mode else code)  # skill mode exits 0
    store = runtime.session_store
    try:
        record = store.resolve_runtime(str(args.session))
    except sessions.PendingForkError as exc:
        raise LineupRefusal(
            R8.format(rid=args.session, message=sessions.pending_fork_message(exc.record))
        ) from exc
    mid = sessions.managed_id(record)
    verb = args.request.verb
    if verb == "show":
        gateway = [gateway_health_line(runtime), *(provider_errors(record) if provider_errors else ())]
        return ApplyResult(_with_gateway_line(show_text(runtime, record), "\n".join(gateway)))
    if verb == "profiles":
        return ApplyResult(profiles_text(runtime, record))
    if verb == "review":
        return ApplyResult(review_text(runtime, record, args.request))
    if args.preview:
        # A read-only fallback preview — no lock, no write (the apply
        # re-evaluates under the lock).
        decision = decide_locked(
            runtime, dict(record), args.request, forced=False, exec_ok=False, now=now,
            preview_only=True,
        )
        return ApplyResult(decision.text, 0, decision)
    inside = inside_session(runtime.environ, mid)
    exec_ok = (
        args.relaunch
        and not inside
        and (args.its_exited or interactive)
        and relaunch_exec is not None
    )
    decision = locked_decision(
        runtime,
        mid,
        lambda current: decide_locked(
            runtime,
            current,
            args.request,
            forced=args.relaunch,
            exec_ok=exec_ok,
            now=now,
        ),
    )
    if decision.kind == "exec":
        assert relaunch_exec is not None and decision.target is not None
        assert decision.sample is not None
        code = relaunch_exec(mid, decision.target, decision.sample)
        return ApplyResult("", int(code or 0), decision)
    return ApplyResult(decision.text, 0, decision)


# ------------------------------------------------------ §8 propagation


@dataclass(frozen=True)
class Follower:
    managed_id: str
    runtime_id: str
    profile: str
    live: bool
    cwd: str
    last_seen_at: str
    generation: int
    has_pending: bool


def _records(runtime: Any) -> list[tuple[str, dict[str, Any] | None]]:
    store = runtime.session_store
    out: list[tuple[str, dict[str, Any] | None]] = []
    for path in store.scan_uuid_records():
        try:
            out.append((path.stem, store.load(path.stem)))
        except sessions.SessionError:
            out.append((path.stem, None))
    return out


def followers(runtime: Any, profiles: Iterable[str]) -> tuple[list[Follower], list[str]]:
    """Lock-free PREVIEW of the v4 records following one of ``profiles`` (§8).

    Never decides who is acted on (:func:`on_saved` re-checks under each
    record's lock). Unreadable records are returned by managed id.
    """

    names = set(profiles)
    found: list[Follower] = []
    unreadable: list[str] = []
    for mid, record in _records(runtime):
        if record is None:
            unreadable.append(mid)
            continue
        if record.get("version") != sessions.RECORD_VERSION:
            continue
        if not record["follow"] or record["profile"] not in names:
            continue
        found.append(
            Follower(
                managed_id=mid,
                runtime_id=record["runtime_session_id"],
                profile=record["profile"],
                live=_is_live(runtime, record),
                cwd=record["cwd"],
                last_seen_at=record["last_seen_at"],
                generation=record["lineup_generation"],
                has_pending="pending" in record,
            )
        )
    return found, unreadable


def profiles_for_binding(runtime: Any, name: str) -> list[str]:
    """Profiles referencing the named binding ``name`` (their followers get §8 saved)."""

    return sorted({profile_name for profile_name, _field in runtime.profiles.referencing(name)})


def _cwd_name(record: Mapping[str, Any]) -> str:
    return Path(str(record.get("cwd", ""))).name or str(record.get("cwd", ""))


def on_saved(
    runtime: Any,
    profiles: Iterable[str],
    *,
    apply_live: bool,
    out: TextIO,
    now: str | None = None,
    barrier: state.BarrierToken | None = None,
    served: bool = False,
) -> int:
    """§8 saved: re-apply the saved profile(s) to their followers, each under its lock.

    The store operation has released its leaf lock already. Membership is
    decided on the record re-loaded under its lifecycle lock (sc[9]); a
    pending relaunch change is never dropped (sc[8]); never an exec.
    Returns the number of records written.

    Propagation is one served-change phase — the caller's
    held ``barrier`` (asserted, never re-acquired: an import or a served
    mutation propagates inside its own phase) or one taken here for the
    whole pass. ``served`` (a served mutation saved the profile): a live
    follower is never applied live; it gets a pending relaunch change.
    """

    if barrier is None:
        try:
            with served_phase(runtime) as token:
                return _on_saved_in_phase(runtime, profiles, apply_live=apply_live, out=out, now=now,
                                          barrier=token, served=served)
        except sessions.MigrationBusyError:
            out.write("? migration or restore in progress; not applied (rerun the save or "
                      "/cm follow later)\n")
            out.flush()
            return 0
        except state.BarrierBusyError:
            out.write(f"? {state.BARRIER_BUSY_TEXT}; not applied (rerun the save or /cm follow later)\n")
            out.flush()
            return 0
    return _on_saved_in_phase(runtime, profiles, apply_live=apply_live, out=out, now=now,
                              barrier=barrier, served=served)


def _on_saved_in_phase(
    runtime: Any,
    profiles: Iterable[str],
    *,
    apply_live: bool,
    out: TextIO,
    now: str | None,
    barrier: state.BarrierToken,
    served: bool,
) -> int:
    """The :func:`on_saved` pass inside its (asserted) served-change phase."""

    state.require_barrier(barrier, root=runtime.session_store.root)
    names = set(profiles)
    written = 0
    store = runtime.session_store
    for path in store.scan_uuid_records():
        mid = path.stem
        m8 = _m8(mid)

        def body(record: dict[str, Any]) -> Decision:
            if record.get("version") != sessions.RECORD_VERSION:
                return Decision("skip")
            if not record["follow"] or record["profile"] not in names:
                return Decision("skip")
            live = _is_live(runtime, record)
            dot = "●" if live else "○"
            where = f"{m8} {_cwd_name(record)}"
            rid = record["runtime_session_id"]
            if "pending" in record:
                if live:
                    return Decision("line", text=f"● {where}: has a pending relaunch change; not applied")
                return Decision(
                    "line",
                    text=f"○ {where}: has a pending relaunch change; it applies at the next resume",
                )
            generation = record["lineup_generation"]
            if generation == 0 or not scope.scope_dir(store.root, mid).is_dir():
                return Decision("line", text=f"○ {where}: no lineup-enabled scope yet; applies at the next resume")
            if not live:
                return Decision("line", text=f"○ {where}: applies at the next resume")
            if not apply_live and not served:
                return Decision(
                    "line",
                    text=f"● {where}: not applied — apply with: claude-multi lineup --session {rid} follow",
                )
            try:
                decision = decide_locked(
                    runtime,
                    record,
                    Request("follow", text="follow"),
                    forced=served,
                    exec_ok=False,
                    origin="propagation",
                    now=now,
                )
            except LineupRefusal as exc:
                return Decision("line", text=f"● {where}: not applied ({_one_line(str(exc))})")
            if decision.kind == "live":
                line = (
                    f"● {where}: applied live (lineup gen {decision.generation}) — run "
                    "/reload-plugins in that session"
                )
                if decision.lead_switch is not None:
                    target = (store.load(mid).get("lead_target") or {})
                    line += (
                        f"; then switch the lead with /model → "
                        f"{decision.lead_switch.get('display') or target.get('key')}"
                    )
                return Decision("written", text=line)
            if decision.kind == "pending":
                return Decision(
                    "written",
                    text=(
                        f"● {where}: relaunch needed ({'; '.join(decision.reasons)}); recorded as "
                        f"pending — exit it, then claude-multi -r {mid}"
                    ),
                )
            if decision.kind == "record":
                return Decision(
                    "written",
                    text=f"● {where}: already matches (lineup gen {decision.generation})",
                )
            return Decision("line", text=f"● {where}: already matches (lineup gen {generation})")

        try:
            decision = locked_decision(runtime, mid, body, barrier=barrier)
        except sessions.MigrationBusyError:
            out.write(
                f"? {m8}: migration or restore in progress; not applied (rerun the save or "
                "/cm follow later)\n"
            )
            continue
        except sessions.SessionError:
            out.write(f"? {m8}: record unreadable; skipped\n")
            continue
        if decision.kind == "skip":
            continue
        if decision.kind == "written":
            written += 1
        out.write(decision.text + "\n")
    out.flush()
    return written


def _one_line(text: str) -> str:
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


# A removal or rename that could not update its followers now: they still
# follow the old name, which no longer resolves, so their next resume pins
# them to the lineup they run (doctor names them meanwhile).
MIGRATION_BUSY_STATE = "migration or restore in progress"
REMOVED_BUSY = ("? {state}; not updated now — they keep their lineup and are pinned at their "
                "next resume (claude-multi doctor lists them)")


def on_removed(
    runtime: Any,
    name: str,
    *,
    renamed_to: str | None,
    out: TextIO,
    barrier: state.BarrierToken | None = None,
) -> int:
    """§8 removed/renamed: pin every follower, rewrite or clear pending (sc[8]).

    Each record re-checked under its lock. ``profile`` keeps the old name (a
    label); never rebinds. ``lead_target`` is kept on a rename (the pinned
    session applies it at resume) and dropped on a removal. Returns the
    number of records written.
    """

    if barrier is None:
        # One served-change phase for the whole pass.
        try:
            with served_phase(runtime) as token:
                return _on_removed_in_phase(runtime, name, renamed_to=renamed_to, out=out, barrier=token)
        except sessions.MigrationBusyError:
            out.write(REMOVED_BUSY.format(state=MIGRATION_BUSY_STATE) + "\n")
            out.flush()
            return 0
        except state.BarrierBusyError:
            out.write(REMOVED_BUSY.format(state=state.BARRIER_BUSY_TEXT) + "\n")
            out.flush()
            return 0
    return _on_removed_in_phase(runtime, name, renamed_to=renamed_to, out=out, barrier=barrier)


def _on_removed_in_phase(
    runtime: Any,
    name: str,
    *,
    renamed_to: str | None,
    out: TextIO,
    barrier: state.BarrierToken,
) -> int:
    """The :func:`on_removed` pass inside its (asserted) served-change phase."""

    state.require_barrier(barrier, root=runtime.session_store.root)
    written = 0
    store = runtime.session_store
    what = "removed" if renamed_to is None else f"renamed to {renamed_to!r}"
    for path in store.scan_uuid_records():
        mid = path.stem
        m8 = _m8(mid)

        def body(record: dict[str, Any]) -> Decision:
            if record.get("version") != sessions.RECORD_VERSION:
                return Decision("skip")
            lines: list[str] = []
            updated = copy.deepcopy(record)
            if record["follow"] and record["profile"] == name:
                updated["follow"] = False
                if renamed_to is None:
                    updated.pop("lead_target", None)
                lines.append(
                    f"{m8}: profile {name!r} was {what}; the session keeps its applied "
                    "lineup and is now pinned"
                )
            pending = record.get("pending")
            if pending is not None and pending.get("profile") == name:
                if renamed_to is not None:
                    updated["pending"] = {**pending, "profile": renamed_to, "follow": False}
                    lines.append(f"{m8}: pending relaunch change now uses {renamed_to!r} (pinned)")
                elif pending.get("kind") == "profile":
                    updated.pop("pending")
                    lines.append(
                        f"{m8}: pending relaunch change dropped: profile {name!r} was removed"
                    )
                else:
                    updated["pending"] = {**pending, "follow": False}
            if updated == record:
                return Decision("skip")
            updated["mutation_token"] = sessions.new_mutation_token()
            store.save(updated)
            return Decision("written", text="\n".join(lines))

        try:
            decision = locked_decision(runtime, mid, body, barrier=barrier)
        except sessions.MigrationBusyError:
            out.write(
                f"? {m8}: migration or restore in progress; not updated (rerun the command "
                "later)\n"
            )
            continue
        except sessions.SessionError:
            out.write(f"? {m8}: record unreadable; skipped\n")
            continue
        if decision.kind == "written":
            written += 1
            if decision.text:
                out.write(decision.text + "\n")
    out.flush()
    return written
