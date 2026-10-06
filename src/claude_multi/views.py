"""Pure view builders for the TUI.

Every row, label and sentence a screen shows is built here from data:
no curses, no I/O, no store writes.  Curses screens, line mode, tests and
goldens all render from this module.  It imports only ``catalog``,
``compiler``, ``profile``, ``scope``, ``sessions``, ``settings`` and
``quota``, ``strict_json`` — never ``cli``, ``tui``, ``launch``, ``lineup`` or
``curses`` (a static test enforces this; ``tui`` imports ``views``,
so the reverse import would be a cycle).  The too-small texts live in
``tui`` only.

Text that comes from state or config (profile names and descriptions,
binding names, titles, cwd names, custom displays, journal model aliases)
is returned raw: the curses screens draw it only through ``tui.safe_add``
and line mode passes it through ``tui.visible_text``, which sanitise it.

Layout conventions: content starts at column 2; a card/screen label
is 10 cells wide at column 2 and its text starts at column 12; rules are
``"─" * min(width - 3, 78)``.  Displays are shown through
:func:`short_label` except where §4 says the full display.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace, field
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, Mapping, Sequence

from . import catalog, compiler, profile, quota, scope, sessions, settings

__all__ = [
    "COUNT_TOKENS_UPSTREAM_ADAPTERS",
    "CardModel",
    "CardRow",
    "DialogModel",
    "DirectRow",
    "EditorRow",
    "LineRow",
    "ModelsModel",
    "OPERATING_WINDOW_CEILING",
    "OUTPUT_RESERVE",
    "PickerItem",
    "PickerModel",
    "ProviderRow",
    "RoleWindowRow",
    "ScreenFloor",
    "SessionRow",
    "SessionsModel",
    "SettingsRow",
    "ages",
    "card_fit",
    "card_model",
    "card_row_text",
    "card_text",
    "card_widths",
    "clip",
    "direct_detail",
    "direct_rows",
    "editor_rows",
    "effective_context",
    "efforts_text",
    "line_rows",
    "lineup_dialog_model",
    "models_model",
    "native_summary",
    "picker_rows",
    "project_text",
    "propagation_rows",
    "provider_rows",
    "review_sentence",
    "role_window_rows",
    "routing_rows",
    "rule",
    "session_actions",
    "session_rows",
    "settings_rows",
    "short_label",
    "table_widths",
    "used_by",
    "workflow_text",
    "workflow_window",
    "zero_model_providers",
]

# Adapters whose gateway forwards ``count_tokens`` upstream; every
# other lead provider gets the "context meter approximate" card note.
COUNT_TOKENS_UPSTREAM_ADAPTERS = frozenset({"cliproxy-oauth-claude-v1"})

# Settings read-only rows: never literals.  ``compiler`` re-exports
# composition's context constants and ``views`` never imports
# ``composition``: the ceiling is the value ``operating_window`` caps
# every window at.
OPERATING_WINDOW_CEILING = compiler.operating_window(sys.maxsize)
OUTPUT_RESERVE = compiler.AUTO_COMPACT_OUTPUT_RESERVE

_STATE_MARKS = {"live": "● running", "unknown": "◐ unknown", "ended": "○ ended"}
_STATE_WORDS = {"live": "running", "unknown": "unknown", "ended": "ended"}
_QUOTA_WORDS = {"429": "rate limit / quota", "402": "payment required", "403": "forbidden"}
_QUOTA_WINDOWS = {"429": "in the last hour", "402": "in the last 24 h", "403": "in the last 24 h"}


# ================================================================ small helpers


def short_label(display: str) -> str:
    """A catalog display up to its first `` · ``."""

    return display.split(" · ", 1)[0]


def efforts_text(efforts: Sequence[str]) -> str:
    """Compact effort list: a run of ≥ 3 consecutive efforts reads ``first…last``.

    Consecutive means adjacent in :data:`profile.EFFORT_ORDER`
    (``low medium high xhigh`` → ``low…xhigh``; ``high max`` stays
    ``high max``).  Values outside the order (``ultracode``, unknown) break
    a run and are shown as they are, in the given order.
    """

    order = profile.EFFORT_ORDER
    pieces: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 3:
            pieces.append(f"{run[0]}…{run[-1]}")
        else:
            pieces.extend(run)
        run.clear()

    for effort in efforts:
        if effort not in order:
            flush()
            pieces.append(effort)
            continue
        if run and order.index(effort) != order.index(run[-1]) + 1:
            flush()
        run.append(effort)
    flush()
    return " ".join(pieces)


def clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, ending in ``…`` when it was cut."""

    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def clip_keeping_remedy(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells; a sentence that ends in a remedy
    (the part after its last `` — ``) loses its middle, never the remedy."""

    if len(text) <= width:
        return text
    head, sep, remedy = text.rpartition(" — ")
    room = width - len(sep) - len(remedy)
    if sep and head and room >= 12:
        return clip(head, room) + sep + remedy
    return clip(text, width)


def fit_text(texts: Sequence[str], width: int) -> str:
    """The first of ``texts`` (the full sentence first, then shorter forms)
    that fits ``width`` cells, else the last one cut by
    :func:`clip_keeping_remedy`."""

    for text in texts:
        if len(text) <= width:
            return text
    return clip_keeping_remedy(texts[-1], width) if texts else ""


def rule(width: int) -> str:
    """The rule text (drawn at column 2)."""

    return "─" * max(0, min(width - 3, 78))


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def _labelled(label: str, text: str) -> str:
    """``"  {label:<10}{text}"``: a label at column 2, its text at column 12."""

    return f"  {label:<10}{text}"


def table_widths(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    total: int,
    *,
    min_widths: Sequence[int],
    gap: int = 3,
    max_col_width: int = 36,
) -> list[int]:
    """``tui.Table``'s width algorithm (TU ``Table._widths``), for text renders.

    Natural width = the longest cell (header included), capped at
    ``max_col_width``; while the block is wider than ``total`` the widest
    column still above its minimum loses one cell.
    """

    natural = []
    for index, column in enumerate(columns):
        cells = [len(row[index]) for row in rows if index < len(row)]
        natural.append(min(max_col_width, max([len(column), *cells, 1])))
    over = sum(natural) + gap * (len(natural) - 1) - total
    while over > 0:
        candidates = [i for i in range(len(natural)) if natural[i] > min_widths[i]]
        if not candidates:
            break
        widest = max(candidates, key=lambda i: natural[i])
        natural[widest] -= 1
        over -= 1
    return natural


def _table_line(cells: Sequence[str], widths: Sequence[int], gap: int) -> str:
    """One table row; a cell cut to its column ends in ``…`` (visibly cut)."""

    return (" " * gap).join(
        clip(cells[i] if i < len(cells) else "", widths[i]).ljust(widths[i])
        for i in range(len(widths))
    ).rstrip()


@dataclass(frozen=True)
class ScreenFloor:
    """A screen's minimum size: ``rows`` × ``cols``.

    ``rows`` = fixed chrome rows + ``min(list_rows, 3)`` + the worst-case
    detail reserve + the rows of the longest key bar the screen can show.
    Screens return :meth:`as_tuple` from ``min_size``; tests size windows
    from it and never hard-code a height.
    """

    rows: int
    cols: int

    @classmethod
    def compute(
        cls,
        *,
        chrome: int,
        list_rows: int,
        detail_reserve: int,
        bar_rows: int,
        cols: int,
    ) -> "ScreenFloor":
        return cls(chrome + min(max(list_rows, 0), 3) + detail_reserve + bar_rows, cols)

    def as_tuple(self) -> tuple[int, int]:
        """``(rows, cols)``, the ``min_size`` return shape."""

        return (self.rows, self.cols)

    def fits(self, height: int, width: int) -> bool:
        return height >= self.rows and width >= self.cols


# ================================================================ ages


def _parse_iso(iso: str) -> datetime:
    text = iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def ages(iso: str | None, *, now: datetime, compact: bool) -> str:
    """Relative age of an ISO timestamp at ``now`` (§3.5; the sessions ``last seen``).

    Full: ``now`` (under a minute, or in the future) / ``N min ago`` / ``N h
    ago`` / ``yesterday`` (24–47 h) / ``N days ago`` (under 14 days) / ``N
    weeks ago`` (under 8 weeks) / ``YYYY-MM-DD``.  Compact (≤ 10 cells):
    ``now`` / ``N min`` / ``N h`` / ``yesterday`` / ``N days`` / ``N wks`` /
    ``YYYY-MM-DD``.  None → ``—``; an unparseable value is returned as it is.
    """

    if iso is None:
        return "—"
    try:
        stamp = _parse_iso(iso)
    except ValueError:
        return iso
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    seconds = int((current - stamp).total_seconds())
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min" if compact else f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h" if compact else f"{hours} h ago"
    if hours < 48:
        return "yesterday"
    days = hours // 24
    if days < 14:
        return f"{days} days" if compact else f"{days} days ago"
    weeks = days // 7
    if weeks < 8:
        return f"{weeks} wks" if compact else f"{weeks} weeks ago"
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ================================================================ lines


@dataclass(frozen=True)
class LineRow:
    """One line of the merged view: Models, Direct, Providers, pickers."""

    key: str
    source: str  # "catalog" | "custom" (scope.line_view's rule)
    provider: str
    provider_display: str
    family: str
    display: str
    short: str  # short_label(display)
    status: str  # "active" | "new"
    admitted: bool  # key in eff.admitted_lines
    provider_enabled: bool  # settings.provider_enabled(eff, provider)
    offered: bool  # membership in scope.line_view(lcat, eff).lines
    lead_capable: bool
    agents_capable: bool
    roles: str | tuple[str, ...]  # "all" or the role-id tuple
    lead_class: str | None  # context.ordinary_profile
    client_tokens: int
    class_label: str  # profile.format_tokens(client_tokens)
    efforts: tuple[str, ...]  # profile.declared_efforts(entry)
    default_effort: str
    mode: str  # "client" (list-shaped efforts) | "gateway"
    # catalog | legacy-custom | operator | operator-migrated.
    origin: str = "catalog"
    provider_tokens: int = 0
    unavailable_reason: str = ""
    lead_usable: bool = True
    family_recognized: bool = True
    qualification: str = "reviewed with the catalog"

    def admits(self, slot: str) -> bool:
        """Whether the declaration recommends ``slot`` (not a use restriction)."""

        if slot == catalog.LEAD_ROLE:
            return self.lead_capable
        return self.agents_capable and (self.roles == "all" or slot in self.roles)


def line_rows(lcat: Any, eff: settings.Effective, *, custom_ids: frozenset[str]) -> tuple[LineRow, ...]:
    """Every line of ``lcat`` (the merged ``LineupCatalog``), New included, in line order.

    ``source`` follows :func:`scope.line_view`'s rule for every line,
    offered or not: ``"custom"`` iff the line's explicit origin is
    ``legacy-custom`` (``origin`` carries the full enum).  ``custom_ids`` is the caller's custom-registry id set;
    a registry id the merge dropped (it shadows a catalog line) keeps its
    catalog row, so the rule never consults it — it is accepted so every
    caller passes the same facts the Models screen shows.  ``offered`` is
    membership in ``scope.line_view(lcat, eff).lines`` (the merged-view seam).
    """

    del custom_ids  # the seam's rule decides ``source`` (see the docstring)
    offered = {line.key for line in scope.line_view(lcat, eff).lines}
    rows: list[LineRow] = []
    for key, entry in lcat.lines.items():
        provider_id = entry["provider"]
        provider = lcat.providers.get(provider_id, {})
        origin = lcat.origin(key) if hasattr(lcat, "origin") else (
            "legacy-custom" if catalog.is_legacy_custom_entry(entry) else "catalog")
        capabilities = entry.get("capabilities", [])
        lead_capable = "lead" in capabilities and isinstance(entry.get("lead"), Mapping)
        raw_roles = entry.get("roles", [])
        roles: str | tuple[str, ...] = raw_roles if raw_roles == "all" else tuple(raw_roles)
        context = entry.get("context", {})
        client_tokens = int(context.get("client_tokens", 0))
        display = entry.get("display", key)
        qualification = "reviewed with the catalog" if origin == "catalog" else "not run"
        gate = getattr(lcat, "agent_gate", None)
        facts = gate.facts.get(key) if gate is not None else None
        if facts is not None:
            qualification = {"missing": "not run", "current": "passed", "failed": "failed",
                             "definition-stale": "stale definition", "contract-stale": "stale contract"}.get(
                                 facts.evidence, facts.evidence)
            if facts.evidence_gaps:
                qualification += " (" + ", ".join(facts.evidence_gaps) + ")"
        rows.append(
            LineRow(
                key=key,
                source="custom" if origin == "legacy-custom" else "catalog",
                provider=provider_id,
                provider_display=provider.get("display", provider_id),
                family=lcat.line_family(key, entry) if hasattr(lcat, "line_family") else catalog.line_family(entry, lcat.providers),
                display=display,
                short=short_label(display),
                status=entry.get("status", "active"),
                admitted=key in eff.admitted_lines,
                provider_enabled=settings.provider_enabled(eff, provider_id),
                offered=key in offered,
                lead_capable=lead_capable,
                agents_capable="agents" in capabilities,
                roles=roles,
                lead_class=context.get("ordinary_profile"),
                client_tokens=client_tokens,
                class_label=profile.format_tokens(client_tokens),
                efforts=profile.declared_efforts(entry),
                default_effort=entry.get("default_effort", ""),
                mode="client" if isinstance(entry.get("efforts"), (list, tuple)) else "gateway",
                origin=origin,
                provider_tokens=int(context.get("provider_tokens", 0)),
                unavailable_reason=("provider off — G → Space enables it" if not settings.provider_enabled(eff, provider_id)
                                    else eff.unavailable_lines.get(key, "")),
                lead_usable=isinstance(entry.get("lead"), Mapping) and bool(context.get("ordinary_profile")),
                family_recognized=profile.recognized_family(lcat.line_family(key, entry), lcat.known_families) is not None,
                qualification=qualification,
            )
        )
    return tuple(rows)


def zero_model_providers(lcat: Any) -> tuple[str, ...]:
    """Provider ids with no line at all (T:84), sorted."""

    used = {entry["provider"] for entry in lcat.lines.values()}
    return tuple(sorted(pid for pid in lcat.providers if pid not in used))


def used_by(
    profiles: Mapping[str, Mapping], bindings: Mapping[str, Mapping], lcat: Any
) -> dict[str, int]:
    """Per live line key: how many profiles bind it (lead or any agent).

    A slot binds a key directly or through ``{"use": N}`` → ``bindings[N]
    ["model"]``; the key goes through ``lcat.resolve_key`` (a retired key
    counts for its successor; a null successor, an unknown key or an
    unknown binding name counts for nothing).  A profile counts once per
    key.  Unreadable profiles are the caller's to skip.
    """

    counts = {key: 0 for key in lcat.lines}
    for document in profiles.values():
        keys: set[str] = set()
        slots = [document.get("lead")]
        agents = document.get("agents")
        if isinstance(agents, Mapping):
            slots.extend(agents.values())
        for slot in slots:
            if not isinstance(slot, Mapping):
                continue
            model = slot.get("model")
            if isinstance(slot.get("use"), str):
                named = bindings.get(slot["use"])
                model = named.get("model") if isinstance(named, Mapping) else None
            if not isinstance(model, str):
                continue
            try:
                resolved = lcat.resolve_key(model).key
            except catalog.CatalogError:
                continue
            if resolved is not None and resolved in counts:
                keys.add(resolved)
        for key in keys:
            counts[key] += 1
    return counts


def _explore_text(explore: str) -> str:
    return {
        "replace": "Explore→cm-explorer",
        "native": "Explore native",
        "off": "Explore off",
    }.get(explore, f"Explore {explore}")


def native_summary(native_agents: Mapping[str, str]) -> str:
    """``Explore→cm-explorer`` / ``Explore native`` / ``Explore off``, `` · Plan off``
    only when Plan is off, then `` · general-purpose {on|off}``."""

    parts = [_explore_text(native_agents.get("explore", "native"))]
    if native_agents.get("plan") == "off":
        parts.append("Plan off")
    parts.append(f"GP {native_agents.get('general_purpose', 'on')}")
    return " · ".join(parts)


def project_text(count: int, collisions: Any) -> str:
    """The project row (§4.1 step 5): the 2.x ``_project_summary`` wording (C:1634-1639)."""

    noun = "project agent" if count == 1 else "project agents"
    discovered = f"{count} {noun} discovered" if count else "no project agents discovered"
    status = "collision blocks launch" if collisions else "none colliding"
    return f"{discovered} (native precedence; {status})"


# ================================================================ review / context


def review_sentence(lineup: profile.ResolvedLineup) -> str:
    """The card's review sentence, a pure function of ``lineup.routing``."""

    if lineup.is_direct:
        return "none (direct session)"
    rows = lineup.routing.rows
    if all(row.normal.reason == "no reviewer bound" for row in rows):
        return "no reviewer bound"
    lbl = profile.label
    writers = [row for row in rows if row.author != catalog.LEAD_ROLE]
    targets = [(row.author, row.normal.reviewer) for row in writers if row.normal.reviewer]
    parts: list[str] = []
    if targets:
        reviewers = {reviewer for _author, reviewer in targets}
        if len(reviewers) == 1:
            families = {row.family for row in writers if row.normal.reviewer}
            fam = f"({next(iter(families))})" if len(families) == 1 else ""
            parts.append(f"writers{fam}→{lbl(reviewers.pop())}")
        else:
            parts += [f"{lbl(author)}→{lbl(reviewer)}" for author, reviewer in targets]
    lead = rows[-1]
    parts.append(f"lead→{lbl(lead.normal.reviewer)}" if lead.normal.reviewer else "lead→—")
    routing = lineup.routing
    if routing.independence_unknown:
        tail = "independence unknown for " + ", ".join(lbl(a) for a in routing.independence_unknown_authors)
        if routing.same_family_authors:
            tail += "; ≈ same-family for " + ", ".join(lbl(a) for a in routing.same_family_authors)
    elif not routing.same_family_authors:
        tail = "all cross-family ✓"
    elif routing.single_family:
        tail = "same-family (reduced independence) ≈"
    else:
        tail = "≈ same-family for " + ", ".join(lbl(a) for a in routing.same_family_authors)
    return " · ".join(parts) + "   " + tail


def effective_context(
    runtime_like: Any, lineup: profile.ResolvedLineup, eff: settings.Effective
) -> compiler.LeadContext:
    """The one resolver: exactly the two calls ``Runtime.prepare`` makes.

    ``compiler.lead_set_context(scope.compile_fence(lineup, lcat, eff),
    settings.compaction_percent_for(eff, lineup.settings_overrides))`` with
    ``lcat = runtime_like.lineup_catalog()``.  Raises what they raise
    (``scope.ScopeError``, ``settings.SettingsError``,
    ``compiler.CompilerError``); screens render ``as_document()``.
    """

    fence = scope.compile_fence(lineup, runtime_like.lineup_catalog(), eff)
    percent = settings.compaction_percent_for(eff, lineup.settings_overrides)
    return compiler.lead_set_context(fence, percent, ceiling=profile.window_ceiling(eff))


@dataclass(frozen=True)
class RoleWindowRow:
    """One role's context window for display (lead, agent or workflow default)."""

    role: str  # "lead", an agent label ("analyst"), or "workflow default"
    selector: str
    class_label: str  # "1M" / "200K"
    window: int  # the effective compaction window
    trigger: int


def role_window_rows(
    lineup: profile.ResolvedLineup, workflow_default: profile.RoleWindow | None = None
) -> tuple[RoleWindowRow, ...]:
    """Each role's effective window under the lineup's policy
    (``profile.lineup_windows``): the lead, every bound agent in lineup
    order, then the workflow default when one is set
    (``scope.workflow_default_window``). Pure data for the card, the lineup
    summary and the launch plan."""

    rows = [
        RoleWindowRow(profile.label(slot), window.selector, profile.format_tokens(window.client_class),
                      window.window, window.trigger)
        for slot, window in profile.lineup_windows(lineup)
    ]
    if workflow_default is not None:
        rows.append(RoleWindowRow("workflow default", workflow_default.selector,
                                  profile.format_tokens(workflow_default.client_class),
                                  workflow_default.window, workflow_default.trigger))
    return tuple(rows)


def workflow_window(
    lcat: Any, eff: settings.Effective | None, lineup: profile.ResolvedLineup | None,
) -> tuple[str, profile.RoleWindow] | None:
    """The Settings workflow default under the lineup's window policy: its
    label (``<model> · <effort>``) and window, or None when it is off or
    does not resolve (the compile names that problem)."""

    if eff is None or lineup is None or eff.workflow_default_binding is None:
        return None
    try:
        role = scope.workflow_default_window(lcat, eff, policy=lineup.policy or profile.default_policy(eff))
        key = lcat.resolve_key(eff.workflow_default_binding["model"]).key
    except (scope.ScopeError, catalog.CatalogError):
        return None
    if role is None or key is None:
        return None
    label = f"{short_label(lcat.lines[key].get('display', key))} · {eff.workflow_default_binding['effort']}"
    return label, role


def workflow_text(label: str, role: profile.RoleWindow) -> str:
    """The card's workflow-default row: model and effort, then its window."""

    return f"{label} · window {profile.format_tokens(role.window)} (agents without a cm-* type)"


def _diamond(lineup: profile.ResolvedLineup | None, eff: settings.Effective | None) -> bool:
    """A profile ``compaction_percent`` override that differs from Settings."""

    if lineup is None or eff is None:
        return False
    value = lineup.settings_overrides.get(settings.COMPACTION_PERCENT_KEY)
    return value is not None and value != eff.compaction_percent


# ================================================================ launch card (§4.1)


@dataclass(frozen=True)
class CardRow:
    """One card row (§4.1.1).

    ``kind`` names the canonical row; ``label`` is drawn at column 2 (10
    wide) and ``text`` at ``col``; ``badge`` (with ``badge_role``) follows
    the text after three spaces; ``cells`` holds a table row (header or
    agent).  ``priority`` is the fit algorithm's ``P`` (lower survives).
    """

    kind: str
    label: str
    text: str
    role: str = "normal"
    priority: int = 0
    col: int = 12
    label_role: str = "dim"
    badge: str = ""
    badge_role: str = ""
    cells: tuple[str, ...] = ()
    # Shorter forms of ``text`` for a narrower card, longest first: the
    # drawn row is the first that fits (``fit_text``).
    short_forms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CardModel:
    rows: tuple[CardRow, ...]  # canonical order; the fit algorithm picks from these
    ready: bool
    errors: tuple[str, ...]  # evaluation errors, secret problems, collisions
    remedy: str | None  # "set a key: G (providers) → K" when a secret problem exists
    keys: tuple[tuple[str, str], ...]  # the §4.1.2 keybar
    resume: bool
    update: bool  # the U badge shows
    agent_widths_min: tuple[int, ...] = (12, 10, 6, 7, 0)


CARD_TABLE_COLUMNS = ("agent", "model", "effort", "provider", "")
# With agents in another window than the lead (a line whose provider bound
# is below the session window keeps the 200K class), the table gains each
# agent's effective window; otherwise the context row names them all.
CARD_WINDOW_TABLE_COLUMNS = ("agent", "model", "effort", "provider", "window", "")
CARD_WINDOW_WIDTHS_MIN = (12, 10, 6, 7, 4, 0)
SECRET_REMEDY = "set a key: G (providers) → K"
# A resume card has no G: ``{way}`` is its way back to a launch card
# (Esc once per screen, or out of claude-multi and in again).
SECRET_REMEDY_RESUME = "set a key: {way}, G → K, then S to resume"
# A resume card that claude-multi -r opened: Esc ends claude-multi.
RESUME_WAY_DIRECT = "Esc, run claude-multi"
CARD_NOT_CONNECTED = "! nothing is connected yet — W opens Get started"
CARD_SEED_UPDATES = "! {n} shipped profile(s) have updates: {names} — P → U (your edits are kept as a backup)"
CARD_UNLOADABLE = "profile {name} cannot be loaded: {path} line {line}: {detail}"
CARD_UNLOADABLE_NO_LINE = "profile {name} cannot be loaded: {path}: {detail}"
CARD_UNLOADABLE_FIX = "P opens Profiles: E edits the file, X deletes it (a copy is kept) · Tab picks another"
CARD_DEFAULT_MARK = " (default)"
EDITOR_SEED_NAG = "a newer shipped version exists — P → U shows the changes (your version is kept as a backup)"
CARD_KEYS_NOT_CONNECTED = (
    ("Enter", "launch"), ("W", "get started"), ("P", "profiles"), ("D", "direct"), ("V", "details"),
    ("S", "sessions"), ("G", "providers"), ("M", "models"), ("O", "settings"), ("H", "doctor"), ("?", "help"),
    ("Esc", ""),
)
# The resume card's bar after Enter (and U when an update applies).
CARD_KEYS_RESUME = (("V", "details"), ("S", "sessions"), ("H", "doctor"), ("?", "help"), ("Esc", "cancel"))
CARD_KEYS_FRESH = (
    ("Enter", "launch"), ("E", "edit"), ("Tab", "next"), ("P", "profiles"), ("D", "direct"), ("V", "details"),
    ("S", "sessions"), ("G", "providers"), ("M", "models"), ("O", "settings"), ("H", "doctor"), ("?", "help"),
    ("Esc", ""),
)


def provider_cell(provider_id: str, family: str, providers: Mapping[str, Mapping] | None) -> str:
    """The provider of a binding, with the model's family when it differs
    from the provider's own (an aggregator line): ``openrouter·google``."""

    provider = (providers or {}).get(provider_id)
    own = provider.get("independence_family") if isinstance(provider, Mapping) else None
    if family and own is not None and family != own:
        return f"{provider_id}·{family}"
    return provider_id


def retired_lead_note(
    lcat: Any, key: str, *, timing: str = "applies at the next resume", named: str | None = None,
) -> str | None:
    """Doctor/card retirement text, comparing the original and final wire route.

    A renamed key can resolve through several retirements. Only an unchanged
    provider and wire is a pure rename; selectors and display names can change.
    """

    resolution = lcat.resolve_key(key)
    if resolution.notice is None or resolution.key is None:
        return None
    old, new = lcat.retired[key], lcat.lines[resolution.key]
    if (old["provider"], old["last_wire"]) == (new["provider"], new["wire_model"]):
        return (
            f"lead {key} renamed → {resolution.key} "
            f"(same model; {timing}){profile._notice_suffix(named)}"
        )
    return (
        f"lead {key} retired → {resolution.key} ({timing}; "
        f"thinking continuity is not carried across generations){profile._notice_suffix(named)}"
    )


def _card_name(target: Any, record: Mapping | None) -> str:
    if getattr(target, "profile", None):
        return str(target.profile)
    kind = getattr(target, "kind", None)
    if kind == "profile-file":
        source = str(getattr(target, "source", ""))
        prefix = "Profile file "
        name = PurePath(source[len(prefix):]).name if source.startswith(prefix) else "stdin"
        return f"(file {name})"
    if record is not None and record.get("profile"):
        return str(record["profile"])
    return "(ad-hoc)"


def _card_action(action: str, record: Mapping | None) -> str:
    if action == "fresh" or record is None:
        return "Fresh"
    word = "Relaunch" if action == "relaunch" else "Resume"
    return f"{word} {str(record.get('managed_id', '?'))[:8]} · lineup gen {record.get('lineup_generation', 0)}"


def card_model(
    *,
    target: Any,
    action: str = "fresh",
    record: Mapping | None = None,
    lineup: profile.ResolvedLineup | None = None,
    context: Mapping[str, Any] | None = None,
    eff: settings.Effective | None = None,
    errors: Sequence[str] = (),
    secret_problems: Sequence[str] = (),
    collisions: Sequence[str] = (),
    health: Sequence[Any] | None = None,
    quota_row: tuple[str, str] | None = None,
    start_hint: str = "",
    update_hint: tuple[str, str] | None = None,
    seed_updates: Sequence[tuple[str, int | None, int]] = (),
    providers: Mapping[str, Mapping] | None = None,
    radar_hits: int = 0,
    project: tuple[int, Sequence[str]] | None = None,
    diff: Sequence[str] = (),
    notices: Sequence[str] = (),
    lead_origin: str | None = None,
    claude_row: str | None = None,
    description: str | None = None,
    is_default: bool = False,
    spend: Sequence[str] = (),
    not_connected: bool = False,
    load_error: Any = None,
    load_path: str | None = None,
    workflow_default: tuple[str, profile.RoleWindow] | None = None,
    resume_way: str = RESUME_WAY_DIRECT,
) -> CardModel:
    """The card's rows in canonical order (§4.1.1) and its keybar (§4.1.2).

    ``context`` is the dict of ``_plan()`` step 6 (``window``/``trigger``/
    ``percent``/``scalar``), never a ``LeadContext``.  ``health`` is
    ``(gateway_problem, gateway_checked, pin, contract_source,
    catalog_version)`` or None (line mode: no loopback call); a down row
    names ``start_hint`` (the caller's ``cli.gateway_service_hint("start")``).
    ``update_hint`` is the installed release's ``(version,
    note)`` (``release_update.card_hint``); a note about an old release
    warns.  ``project`` is ``(project agent count, collision
    texts)``; ``collisions`` are the BLOCKED collision lines.
    ``claude_row`` is the pinned Claude Code's fact line or None.  With
    ``lineup`` None the lineup-derived rows (3–10, 13–15, 17, 18) are
    omitted; with ``context`` None the context and note rows are.

    ``description`` is the profile's description (a dim row under the
    title, the first row the fit drops); ``is_default`` marks the title
    ``(default)``; ``spend`` holds the pay-per-token notes; ``not_connected``
    replaces every per-slot readiness row with one Get started notice and
    selects the bar that offers W; ``load_error`` (a ``ProfileLoadError``,
    ``load_path`` its file for display) makes the card BLOCKED with the
    file, line and the Profiles remedy. ``workflow_default`` is
    :func:`workflow_window` (the Settings workflow default's label and
    window), a row under the agent table. Each role's effective window: the
    context row is the lead's and names every agent when they share it;
    otherwise the agent table gains a ``window`` column. ``resume_way`` is a
    resume card's way back to a launch card (its key remedy names it).
    """

    rows: list[CardRow] = []
    # Resume renders the prepared record: pending changes may have replaced
    # the target's old profile (including a switch to an ad-hoc lineup).
    resuming = action != "fresh" and record is not None
    name = str(record.get("profile") or "(ad-hoc)") if resuming else _card_name(target, record)
    following = record.get("follow") if resuming else getattr(target, "follow", False)
    follow = "follows profile" if following else "pinned"
    mark = CARD_DEFAULT_MARK if is_default else ""
    title = f"— profile: {name}{mark} · {follow} · {_card_action(action, record)}"
    rows.append(CardRow("title", "claude-multi", title, "normal", 0, col=15, label_role="accent"))
    if description:
        rows.append(CardRow("description", "", description, "dim", 9, col=15))
    rows.append(CardRow("rule", "", "", "dim", 0, col=2))
    if lineup is not None:
        binding = lineup.lead.binding
        windows = dict(profile.lineup_windows(lineup))
        mixed = len({window.window for window in windows.values()}) > 1
        lead_text = (
            f"{short_label(binding.display)} · effort {binding.effort} · "
            f"{profile.format_tokens(binding.client_context_tokens)} class"
        )
        if lead_origin in profile.OPERATOR_ORIGINS:
            # A lead you declared is tagged.
            lead_text += " op"
        rows.append(
            CardRow(
                "lead", "lead", lead_text, "normal", 0,
                badge=f"workflows: {lineup.workflows}", badge_role="accent",
            )
        )
        if context is not None:
            roles = (" (lead)" if mixed else " (lead and agents)") if lineup.agents else ""
            ctx_text = (
                f"window {profile.format_tokens(context['window'])}{roles} · compacts at "
                f"~{profile.format_tokens(context['trigger'])} ({context['percent']} %)"
            )
            if _diamond(lineup, eff):
                ctx_text += " ◆ override"
            rows.append(CardRow("context", "context", ctx_text, "dim", 2))
            provider = (providers or {}).get(binding.provider) if providers is not None else None
            if provider is not None and provider.get("adapter") not in COUNT_TOKENS_UPSTREAM_ADAPTERS:
                rows.append(
                    CardRow(
                        "note", "note",
                        f"context meter approximate: {binding.provider} token counts are "
                        "local gateway estimates",
                        "dim", 9,
                    )
                )
        count = len(lineup.agents)
        head = "no agents (direct)" if lineup.is_direct else _plural(count, "agent")
        rows.append(
            CardRow(
                "lineup", "lineup", f"{head} · ",
                "normal", 0, badge="durable scope", badge_role="ok",
                cells=(native_summary(lineup.native_agents),),
            )
        )
        if lineup.agents:
            columns = CARD_WINDOW_TABLE_COLUMNS if mixed else CARD_TABLE_COLUMNS
            rows.append(CardRow("header", "", "", "dim", 0, col=2, cells=columns))
            for index, (rid, agent) in enumerate(lineup.agents.items()):
                ab = agent.binding
                if agent.role.isolation == "worktree":
                    note = "(worktree)"
                elif agent.role.disallowed_tools:
                    note = "read-only"
                else:
                    note = ""
                rows.append(
                    CardRow(
                        "agent", "", "", "normal", 0 if index == 0 else 5, col=2,
                        cells=(profile.label(rid), short_label(ab.display), ab.effort,
                               provider_cell(ab.provider, ab.family, providers),
                               *((profile.format_tokens(windows[rid].window),) if mixed else ()), note),
                    )
                )
        if workflow_default is not None:
            rows.append(CardRow("workflow", "workflow", workflow_text(*workflow_default), "dim", 6))
        sentence = review_sentence(lineup)
        rows.append(CardRow("review", "review", sentence, "warn" if "≈" in sentence else "normal", 3))
        rows.append(CardRow("spread", "spread", lineup.spread_text(), "dim", 6))
        for note in spend:
            rows.append(CardRow("spend", "spend", note, "warn", 5, label_role="warn"))
    if not_connected and not resuming:
        rows.append(CardRow("not-connected", "", CARD_NOT_CONNECTED, "warn", 1, col=2))
    if health is not None:
        problem, checked, pin, contract_source, catalog_version = tuple(health)[:5]
        if problem is not None:
            rows.append(
                CardRow(
                    "health", "health",
                    f"gateway unreachable — launches will fail: {problem}",
                    "error", 1,
                )
            )
        elif checked:
            note = " (operator override)" if contract_source == "override" else ""
            rows.append(
                CardRow(
                    "health", "health",
                    f"gateway ok · Claude {pin}{note} · catalog {catalog_version}", "dim", 1,
                )
            )
    if quota_row is not None:
        text, role = quota_row
        rows.append(CardRow("quota", "quota", text, role, 1))
    if claude_row is not None:
        # The pinned Claude Code, its evidence class here, the owned copy's
        # state and the user's own claude when it differs.
        rows.append(CardRow("claude", "claude", claude_row, "dim", 6))
    if update_hint is not None:
        installed, note = update_hint
        old_release = " days old" in note
        rows.append(
            CardRow(
                "update", "update",
                f"claude-multi {installed} · {note} · press U to update",
                "warn" if old_release else "dim", 1 if old_release else 6,
                label_role="warn" if old_release else "dim",
            )
        )
    if lineup is not None and project is not None and project[0]:
        count, hits = project
        rows.append(
            CardRow("project", "project", project_text(count, hits), "error" if hits else "normal", 7)
        )
    if lineup is not None:
        for finding in lineup.warnings:
            rows.append(CardRow("warning", "", f"! {finding.message}", "warn", 4, col=2))
        for finding in lineup.notices:
            if finding.code != "outsider":
                rows.append(CardRow("notice", "", f"! {finding.message}", "warn", 4, col=2))
    if radar_hits > 0:
        rows.append(
            CardRow(
                "radar", "",
                f"! {radar_hits} model line(s) near an upstream retirement date — "
                "M (models) or H (doctor)",
                "warn", 4, col=2,
            )
        )
    if lineup is not None:
        for finding in lineup.outsiders:
            rows.append(CardRow("outsider", "", finding.message, "normal", 8, col=4))
    if seed_updates:
        names = ", ".join(name for name, _old, _new in seed_updates)
        rows.append(
            CardRow(
                "seeds", "", CARD_SEED_UPDATES.format(n=len(seed_updates), names=names), "warn", 10, col=2,
            )
        )
    resume = action != "fresh"
    if resume and lineup is not None:
        for index, line in enumerate(diff):
            rows.append(CardRow("diff", "diff" if index == 0 else "", line, "normal", 1))
    if lineup is not None:
        # Readiness warnings ("! <slot>: <reason> — <remedy>") and the
        # seed-fallback warning are warn rows on the fresh and resume card;
        # other notes (resume only in practice) stay dim.
        drawn = {finding.message for finding in lineup.notices}
        for line in notices:
            if line in drawn:
                continue
            if line.startswith("! "):
                if not_connected and not resume:
                    continue  # one notice says it: nothing is connected yet
                rows.append(CardRow("readiness", "", line, "warn", 4, col=2))
            elif resume:
                rows.append(CardRow("notes", "note", line, "dim", 2))
    caveat = "thinking continuity is not carried across generations"
    expanded = []
    for row in rows:
        if caveat in row.text:
            first = row.text.split(caveat, 1)[0].rstrip(" (;,").replace(" (applies at", "; applies at")
            expanded.append(replace(row, kind="retirement", label="note", text=first, priority=0, col=12))
            expanded.append(CardRow("retirement-caveat", "", caveat, "dim", 0))
        else:
            expanded.append(row)
    rows = expanded
    unloadable: tuple[str, ...] = ()
    if load_error is not None:
        line_no = getattr(load_error, "line", None)
        shown = load_path if load_path is not None else str(getattr(load_error, "path", ""))
        template = CARD_UNLOADABLE if line_no else CARD_UNLOADABLE_NO_LINE
        unloadable = (template.format(name=getattr(load_error, "name", name), path=shown, line=line_no,
                                      detail=getattr(load_error, "detail", str(load_error))),)
    blocking = unloadable + tuple(errors) + tuple(secret_problems) + tuple(collisions)
    ready = lineup is not None and not blocking
    remedy = (SECRET_REMEDY_RESUME.format(way=resume_way) if resume else SECRET_REMEDY) if secret_problems else None
    if unloadable:
        remedy = CARD_UNLOADABLE_FIX
    update = update_hint is not None
    if resume:
        keys: list[tuple[str, str]] = [("Enter", "resume")]
        if update:
            keys.append(("U", "update"))
        keys += list(CARD_KEYS_RESUME)
    else:
        keys = list(CARD_KEYS_NOT_CONNECTED if not_connected else CARD_KEYS_FRESH)
        if update:
            keys.insert(1, ("U", "update"))
    return CardModel(
        rows=tuple(rows),
        ready=ready,
        errors=blocking,
        remedy=remedy,
        keys=tuple(keys),
        resume=resume,
        update=update,
    )


def card_row_text(row: CardRow, width: int, widths: Sequence[int] | None) -> str:
    """One card row as text from column 0 (the curses card draws the same text)."""

    if row.kind == "retirement" and len(row.text) > width - row.col - 1:
        return _labelled("note", "retirement change at this resume — V details")
    if row.kind == "title":
        return f"  {row.label} {row.text}"
    if row.kind == "description":
        return " " * row.col + clip(row.text, max(0, width - row.col - 1))
    if row.kind == "rule":
        return "  " + rule(width)
    if row.kind in ("header", "agent"):
        return "  " + _table_line(row.cells, widths or (), 3)
    if row.kind == "overflow":
        return row.text
    if row.col == 2:
        return "  " + row.text
    if row.col == 4:
        return "    " + row.text
    if row.kind == "lineup":
        # ``{n} agents · durable scope · {native}``: the badge sits mid-line.
        return _labelled(row.label, row.text + row.badge + " · " + row.cells[0])
    if row.badge:
        return _labelled(row.label, f"{row.text}   {row.badge}")
    return _labelled(row.label, row.text)


def card_widths(model: CardModel, width: int) -> list[int]:
    """The agent table's column widths at ``width`` (``tui.Table``'s algorithm, gap 3)."""

    table =[row.cells for row in model.rows if row.kind in ("header", "agent")]
    if not table:
        return []
    columns = table[0]
    minimums = CARD_WINDOW_WIDTHS_MIN if columns == CARD_WINDOW_TABLE_COLUMNS else model.agent_widths_min
    return table_widths(columns, table[1:], width - 2, min_widths=minimums, gap=3)


def card_fit(model: CardModel, *, height: int, bar_rows: int) -> tuple[CardRow, ...]:
    """The §4.1.1 fit algorithm: the rows to draw at rows 1.., in canonical order.

    Available = ``height - bar_rows - 1 (top) - 3`` (blank + Status + one
    error line).  Rows are taken in ascending ``P`` (ties: canonical order)
    while they fit; agent rows beyond the first are one unit each (P 5) and
    the clipped ones collapse into a resume-specific or fresh overflow row.
    """

    available = max(0, height - bar_rows - 1 - 3)
    indexed = list(enumerate(model.rows))
    extra_agents = [i for i, row in indexed if row.kind == "agent" and row.priority != 0]
    chosen: set[int] = set()
    overflow = 0
    used = 0
    agents_done = False
    for index, row in sorted(indexed, key=lambda item: (item[1].priority, item[0])):
        if index in chosen:
            continue
        if row.kind == "agent" and row.priority != 0:
            if agents_done:
                continue
            agents_done = True
            free = available - used
            if free >= len(extra_agents):
                chosen.update(extra_agents)
                used += len(extra_agents)
            elif free >= 1:
                keep = extra_agents[: free - 1]
                chosen.update(keep)
                overflow = len(extra_agents) - len(keep)
                used += free
            continue
        if used + 1 <= available:
            chosen.add(index)
            used += 1
    clipped = [i for i in extra_agents if i not in chosen]
    if clipped and not overflow:
        # The overflow row is never silently lost: it displaces the least
        # important row drawn (highest P, then the latest), never a P 0 row.
        room = used < available
        if not room:
            victims = [i for i in chosen if model.rows[i].priority > 0]
            if victims:
                chosen.discard(max(victims, key=lambda i: (model.rows[i].priority, i)))
                room = True
        if room:
            overflow = len([i for i in extra_agents if i not in chosen])
    out: list[CardRow] = []
    last_agent = max((i for i in chosen if model.rows[i].kind == "agent"), default=None)
    for index, row in indexed:
        if index in chosen:
            out.append(row)
            if overflow and index == last_agent:
                out.append(
                    CardRow("overflow", "", f"  … {overflow} more agents "
                            f"({'resize to show all' if model.resume else 'E shows all'})", "dim", 5, col=0)
                )
    return tuple(out)


# Card notices added after the card is built (``card_with_notices``); each
# has shorter forms the card draws when the full one does not fit, so the
# remedy stays on the row at 80 columns.
CARD_CONFIG_DIR = ("! CLAUDE_CONFIG_DIR is set ({path}), but managed sessions use ~/.claude for settings, "
                   "MCP servers, memory and resume — V details")
CARD_CONFIG_DIR_SHORT = (
    "! CLAUDE_CONFIG_DIR ({path}) is ignored: settings, MCP servers, memory and resume come from ~/.claude "
    "— V details",
    "! CLAUDE_CONFIG_DIR is ignored: settings, MCP servers, memory and resume come from ~/.claude — V details",
    "! CLAUDE_CONFIG_DIR ignored: settings, MCP, memory, resume use ~/.claude — V",
)
CARD_NEW_LINES = "! {n} new model line(s), not admitted: {names} — M → Enter adds an optional badge"
CARD_NEW_LINES_SHORT = ("! {n} new model line(s), not admitted — M → Enter adds an optional badge",)
CARD_WORKTREE = ("implementer agents are unavailable here: they work in a Git worktree and this directory "
                 "is not a Git repository (git init enables them)")
CARD_WORKTREE_SHORT = (
    "implementer agents are unavailable: this directory is not a Git repository (git init enables them)",
    "implementers unavailable: not a Git repository here (git init enables them)",
)
# Rows that make a launchable card Attention rather than Ready.
CARD_ATTENTION_KINDS = frozenset({"not-connected", "readiness", "config-dir"})
# Notice rows: (role, fit priority). Their rows are kept ahead of the
# review row and the agent table, so a short card still shows them.
_CARD_NOTICE_STYLE = {"config-dir": ("warn", 1), "new-lines": ("dim", 2), "worktree": ("dim", 2)}
CARD_STATUS_TEXT = {"ready": "Ready", "attention": "Attention", "blocked": "BLOCKED"}
CARD_STATUS_ROLE = {"ready": "ok", "attention": "warn", "blocked": "error"}


def card_with_notices(model: CardModel, notices: Sequence[tuple[str, ...]]) -> CardModel:
    """``model`` with notice rows ``(kind, text, *shorter forms)`` placed
    ahead of its resume diff and readiness rows: an ignored
    ``CLAUDE_CONFIG_DIR`` warns (and makes the card Attention); new lines
    and unavailable implementers are dim facts. Each keeps its row at any
    card height the agent table can shrink for."""

    if not notices:
        return model
    rows = list(model.rows)
    at = next((index for index, row in enumerate(rows) if row.kind in ("diff", "readiness", "notes")), len(rows))
    added = [CardRow(kind, "", text, *_CARD_NOTICE_STYLE.get(kind, ("warn", 4)), col=2, short_forms=tuple(shorter))
             for kind, text, *shorter in notices]
    return replace(model, rows=tuple(rows[:at] + added + rows[at:]))


def card_status(model: CardModel) -> str:
    """The card's health in doctor's words: ``blocked`` when the launch is
    refused, ``attention`` when it can launch but something needs a look
    (nothing connected, a slot not ready, an unreachable gateway, a quota
    warning, an ignored CLAUDE_CONFIG_DIR), else ``ready``. Launch eligibility stays ``model.ready``."""

    if not model.ready:
        return "blocked"
    for row in model.rows:
        if row.kind in CARD_ATTENTION_KINDS:
            return "attention"
        if row.kind in ("health", "quota") and row.role in ("warn", "error"):
            return "attention"
    return "ready"


def card_detail_lines(model: CardModel) -> tuple[str, ...]:
    """Every card row as full text for the details view (labels kept, no
    clipping; the agent table as one line per agent)."""

    lines: list[str] = []
    header: tuple[str, ...] = ()
    for row in model.rows:
        if row.kind == "rule":
            continue
        if row.kind == "title":
            lines.append(f"{row.label} {row.text}")
        elif row.kind == "header":
            header = row.cells
        elif row.kind == "agent":
            named = [f"{name} {cell}" if name and name not in ("agent", "model") else cell
                     for name, cell in zip(header or CARD_TABLE_COLUMNS, row.cells) if cell]
            lines.append("  " + " · ".join(named))
        elif row.kind == "lineup":
            lines.append(f"{row.label}: {row.text}{row.badge} · {row.cells[0] if row.cells else ''}")
        elif row.label:
            lines.append(f"{row.label}: {row.text}" + (f"   {row.badge}" if row.badge else ""))
        else:
            lines.append(row.text)
    return tuple(lines)


def card_text(
    model: CardModel,
    width: int,
    *,
    line_mode: bool = False,
    height: int | None = None,
    bar_rows: int = 0,
) -> str:
    """The card as text lines (``\\n``-terminated).

    ``line_mode=True`` prints rows 1–16 with no fit: the
    health, update, diff and note rows, Status, the errors and the keybar
    are omitted, and so are the ``lineup.notices`` part of row 14 and the
    outsider rows (the launch's own ``plan.notices`` loop prints those once).
    Otherwise every row is printed (or, with ``height``, the
    :func:`card_fit` selection), then a blank, the Status line, the error
    lines and the remedy hint.  Text from config is returned raw; callers
    sanitise it (``tui.visible_text``).
    """

    widths = card_widths(model, width)
    lines: list[str] = []
    if line_mode:
        skip = {"health", "update", "notice", "outsider", "diff", "notes", "readiness", "not-connected"}
        for row in model.rows:
            if row.kind not in skip:
                lines.append(card_row_text(row, width, widths))
        return "".join(line.rstrip() + "\n" for line in lines)
    rows = model.rows if height is None else card_fit(model, height=height, bar_rows=bar_rows)
    for row in rows:
        lines.append(card_row_text(row, width, widths))
    lines.append("")
    lines.append("  Status  " + CARD_STATUS_TEXT[card_status(model)])
    for error in model.errors:
        lines.append("    " + error)
    if model.remedy:
        lines.append("    " + model.remedy)
    return "".join(line.rstrip() + "\n" for line in lines)


# ================================================================ profile editor (§4.2)


@dataclass(frozen=True)
class EditorRow:
    """One editor row: ``key`` is the focus target (``general``, ``lead``, an
    agent id, ``native``, ``review``, ``checks``) or ``""`` for chrome."""

    kind: str  # "header" | "rule" | "general" | "lead" | "blank" | "agents-header" | "agent" | "native" | "review" | "checks" | "message"
    key: str
    text: str
    role: str = "normal"
    selectable: bool = False
    suffix: str = ""
    suffix_role: str = "dim"


def _slot_view(
    slot: str, binding: Any, *, cat: Any, bindings: Mapping[str, Mapping]
) -> tuple[str, str, str]:
    """``(binding text, provider cell, role)`` of one document slot without a lineup."""

    if not isinstance(binding, Mapping):
        return ("— unbound —", "", "dim")
    named = binding.get("use") if isinstance(binding.get("use"), str) else None
    target = bindings.get(named) if named else binding
    if not isinstance(target, Mapping):
        return (f"{named} → (missing named binding)", "", "error")
    model, effort = target.get("model"), target.get("effort")
    try:
        key = cat.resolve_key(str(model)).key
    except catalog.CatalogError:
        return (f"{model} · {effort} (unknown model)", "", "error")
    if key is None:
        return (f"{model} was removed — unbound", "", "warn")
    entry = cat.lines[key]
    text = f"{short_label(entry.get('display', key))} · {effort}"
    if named:
        text = f"{named} → {text}"
    return (text, provider_cell(str(entry.get("provider", "")), catalog.line_family(entry, cat.providers),
                                cat.providers), "normal")


def _checks_text(evaluation: Any, width: int) -> tuple[str, str]:
    if evaluation.errors:
        count = len(evaluation.errors)
        return (f"✗ {count} error(s): {evaluation.errors[0]}", "error")
    warnings = list(evaluation.lineup.warnings) if evaluation.lineup is not None else []
    text = "✓ valid"
    room = max(0, width - 12)
    for index, finding in enumerate(warnings):
        piece = f"   ! {finding.compact}"
        rest = len(warnings) - index
        tail = f" (+{rest} more — ? lists all)"
        if len(text) + len(piece) > room or (
            index < len(warnings) - 1 and len(text) + len(piece) + len(tail) > room
        ):
            text += f" (+{rest} more — ? lists all)"
            break
        text += piece
    return (text, "warn" if warnings else "ok")


def editor_rows(state: Any, *, width: int = 80, message: str = "", focus: str | None = None) -> tuple[EditorRow, ...]:
    """The profile editor's rows (§4.2) from a ``ProfileEditorState``-shaped ``state``.

    ``state`` carries ``document``, ``original``, ``evaluation``
    (``profile.Evaluation``), ``cat`` (``LineupCatalog``), ``bindings``,
    ``effective``, ``is_seed`` and ``seed_update`` (``(installed, shipped)``
    or None).  Bindings come from ``evaluation.lineup`` when it evaluates,
    else from the document through ``cat.resolve_key``.  At width ≥ 80 the
    agent rows carry the provider column (``openrouter·google`` for an
    aggregator line); at 60–79 it is dropped and the binding narrows with ``…``.
    With ``focus`` on the lead or an agent row and no ``message``, the
    message row describes that slot's role and grade (``role_description``).
    """

    document = state.document
    evaluation = state.evaluation
    lineup = evaluation.lineup
    cat = state.cat
    bindings = state.bindings
    name = str(document.get("name", ""))
    rows: list[EditorRow] = []
    header = f"edit profile — {name}" + ("  (seed)" if state.is_seed else "")
    dirty = document != state.original
    rows.append(EditorRow("header", "", header, "accent", suffix="unsaved changes ●" if dirty else "", suffix_role="warn"))
    rows.append(EditorRow("rule", "", rule(width), "dim"))
    general = f'name {name} · description "{document.get("description", "")}"'
    overrides = document.get("settings_overrides") or {}
    percent = overrides.get(settings.COMPACTION_PERCENT_KEY)
    if percent is not None and state.effective is not None and percent != state.effective.compaction_percent:
        general += f" · ◆ compaction {percent} %"
    rows.append(EditorRow("general", "general", _labelled("General", general)[2:], selectable=True))
    lead_doc = document.get("lead")
    if lineup is not None:
        lb = lineup.lead.binding
        lead_text = f"{short_label(lb.display)} · {lb.effort}"
        if lb.named:
            lead_text += f"  (named {lb.named})"
        lead_role = "normal"
    else:
        lead_text, _family, lead_role = _slot_view(catalog.LEAD_ROLE, lead_doc, cat=cat, bindings=bindings)
        if isinstance(lead_doc, Mapping) and isinstance(lead_doc.get("use"), str):
            lead_text = lead_text.split(" → ", 1)[-1] + f"  (named {lead_doc['use']})"
    rows.append(EditorRow("lead", "lead", _labelled("Lead", lead_text)[2:], lead_role, True, suffix="[Enter]"))
    rows.append(EditorRow("blank", "", ""))
    wide = width >= 80
    agents_header = f"{'Agents':<24} {'binding':<32}" + (" provider" if wide else "")
    rows.append(EditorRow("agents-header", "", agents_header.rstrip(), "dim"))
    doc_agents = document.get("agents") if isinstance(document.get("agents"), Mapping) else {}
    notices = {f.slot: f for f in lineup.notices} if lineup is not None else {}
    binding_width = 32 if wide else max(8, width - 2 - 23 - 2)
    for rid in catalog.AGENT_ROLE_IDS:
        label = profile.label(rid)
        suffix = ""
        role = "normal"
        family = ""
        if lineup is not None and rid in lineup.agents:
            ab = lineup.agents[rid].binding
            text = f"{short_label(ab.display)} · {ab.effort}"
            if ab.named:
                text = f"{ab.named} → {text}"
                suffix = "(named)"
            family = provider_cell(ab.provider, ab.family, cat.providers)
            finding = notices.get(rid)
            if finding is not None and ab.requested != ab.key:
                suffix = (suffix + "  " if suffix else "") + f"(was {ab.requested})"
        elif rid in doc_agents:
            text, family, role = _slot_view(rid, doc_agents[rid], cat=cat, bindings=bindings)
            if role == "warn":
                family = ""
            if isinstance(doc_agents[rid], Mapping) and doc_agents[rid].get("use"):
                suffix = "(named)"
        else:
            text, role = "— unbound —", "dim"
        line = f"{label:<22} {clip(text, binding_width):<{binding_width}}"
        if wide and family:
            line += f" {family}"
        rows.append(EditorRow("agent", rid, line.rstrip(), role, True, suffix=suffix))
    rows.append(EditorRow("blank", "", ""))
    native = document.get("native_agents") or {}
    native_text = (
        f"{_explore_text(native.get('explore', 'native'))} · Plan {native.get('plan', 'native')} · "
        f"general-purpose {native.get('general_purpose', 'on')} · "
        f"workflows {document.get('workflows', 'native')}"
    )
    rows.append(EditorRow("native", "native", _labelled("Native", native_text)[2:], selectable=True))
    if lineup is not None:
        review = (
            f"routing table ▸ ({lineup.routing.authors} authors, "
            f"{len(lineup.routing.same_family_authors)} same-family)"
        )
    else:
        review = "routing table ▸ (fix the errors first)"
    rows.append(EditorRow("review", "review", _labelled("Review", review)[2:], selectable=True))
    checks, checks_role = _checks_text(evaluation, width)
    rows.append(
        EditorRow("checks", "checks", _labelled("Checks", checks)[2:], checks_role, bool(evaluation.errors))
    )
    slot = catalog.LEAD_ROLE if focus == "lead" else focus
    described = role_description(cat, slot) if not message and slot in (catalog.LEAD_ROLE, *catalog.AGENT_ROLE_IDS) else ""
    if described:
        rows.append(EditorRow("message", "", described, "dim"))
        return tuple(rows)
    if not message and state.seed_update is not None:
        message = EDITOR_SEED_NAG
    rows.append(EditorRow("message", "", message, "warn" if message else "normal"))
    return tuple(rows)


# ================================================================ binding picker (§4.3)


@dataclass(frozen=True)
class PickerItem:
    kind: str  # "line" | "named" | "zero" | "off"
    key: str  # line key, binding name or ""
    text: str
    selectable: bool
    role: str = "normal"
    efforts: tuple[str, ...] = ()  # ← → cycles these (empty = inert)
    initial_effort: str = ""
    note: str = ""
    details: tuple[str, ...] = ()
    effort_reasons: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PickerModel:
    title: str
    items: tuple[PickerItem, ...]
    selected: int  # first selectable item, or the current binding's line
    subtitle: str = ""  # the slot's role and grade description (``role_description``)


def role_description(lcat: Any, slot: str) -> str:
    """``<role> (<grade>): <description>`` of a lead or agent slot from the
    catalog roles, or "" for a slot that is not a role (a named binding,
    the workflow default)."""

    roles = getattr(lcat, "roles", None)
    role = roles.get(slot) if isinstance(roles, Mapping) else None
    if not isinstance(role, Mapping) or not isinstance(role.get("description"), str):
        return ""
    grade = role.get("grade")
    return f"{profile.label(slot)}" + (f" ({grade})" if isinstance(grade, str) else "") + f": {role['description']}"


_PICKER_SLOT_LABELS = {"binding": "named binding", "workflow": "workflow default binding"}


def _picker_admits(row: LineRow, slot: str) -> bool:
    if slot == "binding":
        return row.lead_capable or row.agents_capable
    if slot == "workflow":
        return row.agents_capable
    return row.admits(slot)


def _operator_origin(lcat: Any, key: str) -> bool:
    origin = getattr(lcat, "origin", None)
    return callable(origin) and origin(key) in profile.OPERATOR_ORIGINS


def _operator_agent_verdict(lcat: Any, key: str, slot: str, effort: str) -> Any:
    """The current agent gate verdict of an operator line in ``slot``."""

    entry = lcat.lines.get(key)
    if not isinstance(entry, Mapping):
        return None
    gate = getattr(lcat, "agent_gate", None)
    return profile.agent_eligibility(
        entry, key=key, slot=None if slot == "workflow" else slot, effort=effort,
        facts=gate.facts.get(key) if gate is not None else None, agent_efforts=lcat.agent_efforts,
        use=profile.WORKFLOW_USE if slot == "workflow" else profile.AGENT_USE,
        family=lcat.line_family(key, entry), known_families=lcat.known_families,
    )


def picker_rows(
    rows: Sequence[LineRow],
    *,
    slot: str,
    bindings: Mapping[str, Mapping],
    lcat: Any,
    eff: settings.Effective,
    current: Mapping[str, Any] | None,
) -> PickerModel:
    """The binding picker: route availability, recommendations and named rows.

    ``slot`` is ``cm-lead``, an agent id, ``"binding"`` (the named-binding
    editor) or ``"workflow"`` (Settings).  ``current`` is the slot's
    binding as written (``{model, effort}`` or ``{use}``) or None.
    """

    current_model: str | None = None
    current_effort: str | None = None
    if isinstance(current, Mapping):
        target = bindings.get(current["use"]) if isinstance(current.get("use"), str) else current
        if isinstance(target, Mapping):
            current_model, current_effort = target.get("model"), target.get("effort")
    current_key = None
    removed = ""
    if isinstance(current_model, str):
        try:
            current_key = lcat.resolve_key(current_model).key
        except catalog.CatalogError:
            current_key = None
        if current_key is None:
            removed = f"   (current: {current_model} was removed)"
    label = _PICKER_SLOT_LABELS.get(slot, profile.label(slot))
    title = f"{label} — choose model{removed}"
    items: list[PickerItem] = []
    if slot == "workflow":
        items.append(PickerItem("off", "", "off", True))
    eligible = list(rows)  # Recommendations and optional attestations never hide a line.
    by_provider: dict[str, list[LineRow]] = {}
    displays: dict[str, str] = {}
    for row in eligible:
        by_provider.setdefault(row.provider, []).append(row)
        displays[row.provider] = str(row.provider_display)
    lead_like = slot in (catalog.LEAD_ROLE, "binding")
    selected = None
    for provider_id in sorted(by_provider, key=lambda pid: (displays[pid].lower(), pid)):
        for index, row in enumerate(by_provider[provider_id]):
            entry = lcat.lines[row.key]
            shown = list(profile.available_efforts(
                entry, lcat.lead_efforts if lead_like else lcat.agent_efforts,
                lead=lead_like, workflow=slot == "workflow"))
            prov = provider_id if index == 0 else ""
            tag = "◇ " if _operator_origin(lcat, row.key) else ""
            text = f"{prov:<11} {tag + row.short:<16} {row.family:<9} {row.class_label:<5} efforts {' '.join(shown)}"
            note = row.unavailable_reason or eff.unavailable_lines.get(row.key, "")
            if not row.provider_enabled:
                note = "provider off — G → Space enables it"
            if not note and slot == catalog.LEAD_ROLE and not row.lead_usable:
                note = "missing lead/context fields — edit the model definition"
            if not note and not shown:
                note = "no representable effort/selector — edit the model definition"
            enabled = not note
            warnings = []
            if row.status == "new" and not row.admitted:
                warnings.append("not admitted (optional badge — M → Enter)")
            if not _picker_admits(row, slot):
                warnings.append("binding overrides capability/role recommendations")
            if not row.family_recognized:
                warnings.append("family independence unknown")
            undeclared = [e for e in shown if e != profile.ULTRACODE and e not in row.efforts]
            if undeclared:
                warnings.append("effort unverified for this line: " + ", ".join(undeclared))
            effort_reasons = {}
            if not lead_like and _operator_origin(lcat, row.key):
                for effort in shown:
                    verdict = _operator_agent_verdict(lcat, row.key, slot, effort)
                    if verdict is not None:
                        warnings.extend(f.message for f in verdict.warnings)
                        if not verdict.eligible:
                            effort_reasons[effort] = "; ".join(verdict.reasons) + " — " + verdict.remedy
                if enabled and shown and len(effort_reasons) == len(shown):
                    enabled = False
                    note = next(iter(effort_reasons.values()))
            if slot == catalog.LEAD_ROLE and profile.ULTRACODE in shown:
                initial = profile.ULTRACODE
            elif current_key == row.key and current_effort in shown:
                initial = str(current_effort)
            else:
                initial = row.default_effort if row.default_effort in shown else next(iter(shown), "")
            if enabled and initial in effort_reasons:
                initial = next(e for e in shown if e not in effort_reasons)
            if 0 < row.provider_tokens < 200000:
                warnings.append("provider bound below the 200K client class; no per-agent window")
            if enabled and warnings:
                note = "Attention: " + warnings[0] + " — V details"
            cycle = tuple(shown)
            if slot == "workflow" and row.mode == "client":
                cycle = ()
            items.append(
                PickerItem(
                    "line", row.key, text + (f"  {note}" if note else ""), enabled,
                    "normal" if enabled else "dim", cycle, initial, note,
                    details=(f"provider: {row.provider_display} ({row.provider})",
                             f"family: {row.family}" + (" (operator-declared)" if tag else ""),
                             f"declared client class: {row.class_label}; provider bound: {row.provider_tokens}",
                             "admission: " + ("admitted (optional badge)" if row.admitted else "not admitted"),
                             "qualification: " + row.qualification,
                             *dict.fromkeys(warnings)),
                    effort_reasons=effort_reasons,
                )
            )
            if (
                selected is None
                and enabled
                and row.key == current_key
                and not (isinstance(current, Mapping) and "use" in current)
            ):
                selected = len(items) - 1
    zero = zero_model_providers(lcat)
    if zero:
        items.append(PickerItem("zero", "", f"{' · '.join(zero)}     configured · no models", False, "dim"))
    if slot not in ("binding", "workflow"):
        by_key = {row.key: row for row in eligible}
        line_items = {item.key: item for item in items if item.kind == "line"}
        for name in sorted(bindings):
            spec = bindings[name]
            try:
                key = lcat.resolve_key(str(spec.get("model"))).key
            except catalog.CatalogError:
                key = None
            row = by_key.get(key)
            item = line_items.get(key)
            effort = str(spec.get("effort"))
            text = f"named       {name} → {row.short if row else spec.get('model')} · {effort}"
            allowed = item is not None and item.selectable and effort in item.efforts and effort not in item.effort_reasons
            note = (item.effort_reasons.get(effort) or item.note) if item else "unknown model — edit the named binding"
            if item and item.selectable and effort not in item.efforts:
                note = "unsupported effort/selector — edit the named binding"
            items.append(PickerItem("named", name, text + (f"  {note}" if note else ""), allowed,
                                    "normal" if allowed else "dim",
                                    initial_effort=effort, note=note,
                                    details=item.details if item else (note,)))
            if (
                selected is None
                and isinstance(current, Mapping)
                and current.get("use") == name
            ):
                selected = len(items) - 1
    if selected is None:
        selected = next((i for i, item in enumerate(items) if item.selectable), 0)
    return PickerModel(title=title, items=tuple(items), selected=selected, subtitle=role_description(lcat, slot))


# ================================================================ routing preview (§4.4)


def routing_rows(lineup: profile.ResolvedLineup) -> tuple[tuple[str, str, str], ...]:
    """``(author, normal change, high-stakes change)`` per ``Routing.rows`` (AUTHOR_ORDER).

    A cell with a reviewer is ``{label} {short display} {✓|≈}`` plus ``°``
    when the preferred reviewer shares the author's family; a cell with a
    reason is ``— ({reason})``.  A direct lineup is one row: ``no agents:
    nothing to route``.
    """

    if lineup.is_direct:
        return (("no agents: nothing to route", "", ""),)

    def cell(value: profile.RouteCell) -> str:
        if value.reviewer is None:
            return f"— ({value.reason})" if value.reason else "—"
        agent = lineup.agents.get(value.reviewer)
        display = short_label(agent.binding.display) if agent is not None else "?"
        mark = "? independence unknown" if value.independence_unknown else "≈" if value.same_family else "✓"
        return f"{profile.label(value.reviewer)} {display} {mark}" + ("°" if value.only_other_family else "")

    out = []
    for row in lineup.routing.rows:
        if row.author == catalog.LEAD_ROLE:
            author = f"lead ({short_label(lineup.lead.binding.display)})"
        else:
            author = profile.label(row.author)
        out.append((author, cell(row.normal), cell(row.high)))
    return tuple(out)


# ================================================================ propagation (§4.6)


def propagation_rows(
    followers: Sequence[Any],
    states: Mapping[str, str],
    titles: Mapping[str, str],
    *,
    width: int = 80,
) -> tuple[str, ...]:
    """One row per follower (§4.6; sc[8]).

    ``states[managed_id]`` is ``_liveness``'s ``live``/``unknown``/``ended``
    (a missing state reads ``unknown``); ``titles[managed_id]`` the record
    title or ``—``.  A pending change wins over the generation-0 row, which
    wins over the state row.  Below 80 columns the title column is dropped;
    no row is wider than ``width - 3``.
    """

    out: list[str] = []
    for follower in followers:
        mid = follower.managed_id
        state = states.get(mid, "unknown")
        word = _STATE_WORDS.get(state, state)
        if follower.has_pending:
            mark, action = f"↻ {word}", "has a pending relaunch change; not applied"
        elif follower.generation == 0:
            mark, action = f"○ {word}", "no lineup-enabled scope yet; applies at next resume"
        elif state in ("live", "unknown"):
            mark, action = _STATE_MARKS[state], "apply now (then /reload-plugins there)"
        else:
            mark, action = _STATE_MARKS.get(state, f"○ {word}"), "applies at next resume"
        cwd = PurePath(follower.cwd).name or follower.cwd
        title = titles.get(mid) or "—"
        shown = "—" if title == "—" else f'"{title}"'
        if width >= 80:
            text = f"  {mark:<12}{cwd:<14} {shown}   {action}"
        else:
            text = f"  {mark:<12}{cwd:<14} {action}"
        out.append(clip(text, max(0, width - 3)))
    return tuple(out)


# ================================================================ sessions (§4.7)


@dataclass(frozen=True)
class SessionRow:
    managed_id: str
    cells: tuple[str, ...]
    role: str
    needs: bool
    lead_error: bool
    version: int


@dataclass(frozen=True)
class SessionsModel:
    title: str
    columns: tuple[str, ...]
    rows: tuple[SessionRow, ...]
    min_widths: tuple[int, ...]
    banner: str | None  # below the last row; the last selectable item
    needs_count: int
    native: str | None


def _lead_cell(key: str, lcat: Any) -> tuple[str, bool]:
    needs = sessions.lead_key_needs_choice(key, lcat)
    if needs:
        return (f"{key} !", True)
    try:
        resolved = lcat.resolve_key(key).key
    except catalog.CatalogError:
        return (f"{key} !", True)
    return (short_label(lcat.lines[resolved].get("display", resolved)), False)


def session_rows(
    summaries: Sequence[sessions.RecordSummary],
    *,
    lcat: Any,
    states: Mapping[str, str],
    now: datetime,
    width: int,
    cwd: str | None = None,
    forks: frozenset[str] = frozenset(),
    native_count: int = 0,
) -> SessionsModel:
    """The sessions table (§4.7): columns by width tier, marks, the banner.

    ``states[managed_id]`` is ``_liveness``'s value; ``cwd`` the filter
    directory (None = all directories); ``forks`` the managed ids with a
    pending fork; ``native_count`` the unmanaged native sessions.
    """

    if width >= 96:
        tier, name_w, compact = "all", 16, False
    elif width >= 80:
        tier, name_w, compact = "all", 12, True
    elif width >= 60:
        tier, name_w, compact = "mid", 12, True
    else:
        tier, name_w, compact = "narrow", 12, True
    columns_all = ("state", "profile", "lead", "agents", "follow", "last seen", "title")
    keep = {
        "all": columns_all,
        "mid": ("state", "profile", "lead", "last seen", "title"),
        "narrow": ("state", "profile", "title"),
    }[tier]
    rows: list[SessionRow] = []
    needs_total = 0
    for summary in summaries:
        lead, needs = _lead_cell(summary.lead_key, lcat)
        needs_total += needs
        state = states.get(summary.managed_id, "unknown")
        v4 = summary.version == sessions.RECORD_VERSION
        agents = str(summary.agent_count) if summary.agent_count else "—"
        if not v4 or summary.follow is None or summary.profile_label == "(ad-hoc)":
            follow = "—"
        else:
            follow = "follow" if summary.follow else "pinned"
        if summary.title:
            title = summary.title
        elif needs:
            title = "! needs a choice"
        else:
            title = "—"
        if summary.managed_id in forks:
            title = "⚠ " + title
        if summary.pending:
            title = "↻ " + title
        full = {
            "state": _STATE_MARKS.get(state, "◐ unknown"),
            "profile": clip(summary.profile_label, name_w),
            "lead": clip(lead, name_w),
            "agents": agents,
            "follow": follow,
            "last seen": ages(summary.last_seen_at, now=now, compact=compact),
            "title": title,
        }
        rows.append(
            SessionRow(
                managed_id=summary.managed_id,
                cells=tuple(full[c] for c in keep),
                role="error" if needs else "normal",
                needs=needs,
                lead_error=needs,
                version=summary.version,
            )
        )
    total = len(summaries)
    where = "all directories" if cwd is None else f"{PurePath(cwd).name or cwd} (cwd)"
    title = f"sessions — {where} · {total} total"
    banner = (
        f"! {needs_total} session(s) need a profile choice (lead model removed)"
        if needs_total
        else None
    )
    native = f"native (not managed): {native_count} — L links one" if native_count else None
    mins = {"state": 9, "profile": 8, "lead": 8, "agents": 6, "follow": 6, "last seen": 9, "title": 5}
    return SessionsModel(
        title=title,
        columns=keep,
        rows=tuple(rows),
        min_widths=tuple(mins[c] for c in keep),
        banner=banner,
        needs_count=needs_total,
        native=native,
    )


def session_actions(
    summary: sessions.RecordSummary,
    *,
    drift: Sequence[str] = (),
    pending_reasons: Sequence[str] = (),
    lead_target: str | None = None,
    needs_notice: str | None = None,
    needs: bool = False,
    repair: bool = False,
) -> str:
    """The actions row of the selected record (§4.7): facts plus the first applicable note.

    ``repair`` (an identity repair is needed; the resume gate offers
    ``Repair & resume``) outranks the §4.7 notes: it blocks the resume.
    """

    v4 = summary.version == sessions.RECORD_VERSION
    if v4:
        head = (
            f"lineup gen {summary.lineup_generation} · {_plural(summary.agent_count, 'agent')} · "
            f"{'follow' if summary.follow else 'pinned'}"
        )
    else:
        head = f"legacy record · {_plural(summary.agent_count, 'agent')}"
    if repair:
        note = "identity repair needed — Enter offers Repair & resume"
    elif v4 and drift:
        note = f"settings changed since launch: {drift[0]} — applies at the next resume"
    elif v4 and summary.pending:
        note = f"pending relaunch: {'; '.join(pending_reasons) or 'recorded'}"
    elif v4 and summary.lead_target and lead_target:
        note = f"requested lead: {lead_target} — /model in the session, or the next resume"
    elif needs:
        note = (
            f"needs a lead choice: {needs_notice}"
            if v4 and needs_notice
            else f"needs a lead choice: {summary.lead_key} has no live line"
        )
    elif not v4:
        note = "legacy record — the next resume migrates it"
    else:
        return head
    return f"{head} · {note}"


SESSION_USAGE_UNAVAILABLE = "usage (last 24 h): unavailable — the gateway log could not be read (not zero)"


def session_details(
    summary: sessions.RecordSummary,
    record: Mapping[str, Any],
    *,
    state: str,
    lead: str,
    agents: Sequence[str] = (),
    drift: Sequence[str] = (),
    pending_target: str | None = None,
    explain: Sequence[str] | None = None,
    usage: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Sessions V: identity, directory, runtime id, the applied lineup, the
    pending change and why it waits, follow, drift, the routing explanation
    and the observed usage of the last 24 hours. ``explain``/``usage`` None
    are unavailable (said so, never zero). Metadata only."""

    v4 = summary.version == sessions.RECORD_VERSION
    mid = summary.managed_id
    lines = [f"session {mid}" + (f' · "{summary.title}"' if summary.title else ""),
             f"state: {_STATE_MARKS.get(state, '◐ unknown')}",
             f"directory: {summary.cwd}",
             f"runtime id: {summary.runtime_id} (what Claude Code resumes)"]
    aliases = record.get("runtime_aliases") or ()
    if aliases:
        lines.append(f"earlier runtime ids: {len(aliases)}")
    if not v4:
        lines.append("legacy record: the next resume (Enter) migrates it; its lineup is shown after that")
    else:
        follow = ("follows profile " + summary.profile_label) if summary.follow else "pinned (follow off)"
        lines.append(f"profile: {summary.profile_label} · {follow}")
        lines.append(f"lineup: gen {summary.lineup_generation} · lead {lead}")
        lines += [f"  {line}" for line in agents] or ["  no agents"]
        pending = record.get("pending")
        if pending:
            why = "; ".join(pending.get("reasons") or ()) or "a change that needs a relaunch"
            lines.append(f"pending: {pending_target or 'a recorded change'} — waits because {why}")
            lines.append(f"  it applies at the next resume (Enter, or claude-multi -r {mid[:8]}); "
                         "T → keep current lineup discards it")
        else:
            lines.append("pending: none")
        lines.append("settings drift: " + ("; ".join(drift) + " — applies at the next resume" if drift else "none"))
    lines.append("")
    if explain is None:
        lines.append("routing: unavailable for this record")
    else:
        lines += list(explain)
    lines.append("")
    lines += list(usage) if usage is not None else [SESSION_USAGE_UNAVAILABLE]
    return tuple(lines)


# ================================================================ lineup dialog (§4.7.1)


@dataclass(frozen=True)
class DialogModel:
    title: str
    lines: tuple[tuple[str, str, str], ...]  # (label, text, role)
    can_apply: bool
    focus: str


def lineup_dialog_model(
    *,
    title: str | None,
    managed_id: str,
    state: str,
    profile_label: str,
    generation: int,
    target: str,
    target_profile: str | None = None,
    focus: str = "to",
    diff: Sequence[str] = (),
    mode: str | None = None,
    reasons: Sequence[str] = (),
    lead_switch: str | None = None,
    errors: Sequence[str] = (),
    preview: Sequence[str] | None = None,
    target_provider: str | None = None,
) -> DialogModel:
    """The lineup dialog.

    ``target`` is ``"profile"``, ``"per-agent"``, ``"direct"``, ``"keep"``
    (keep the current lineup: pin semantics, a pending change is
    discarded) or ``"fallback"`` (``target_provider``); ``diff`` the
    ``lineup.render_diff`` lines; ``mode`` ``"LIVE"``/``"RELAUNCH"`` (None
    with errors); ``reasons`` the RELAUNCH reasons; ``lead_switch`` the LIVE
    lead-switch display. The keep and fallback targets show ``preview``,
    the read-only ``lineup.preview`` text, instead of a diff.
    """

    word = _STATE_WORDS.get(state, state)
    head = f'"{title}"' if title else managed_id[:8]
    lines: list[tuple[str, str, str]] = []
    lines.append(("from", f"{profile_label} (gen {generation})", "normal"))
    if target == "profile":
        to = f"[{target_profile or '—'} ▾]"
    elif target == "per-agent":
        to = "per-agent edit ▸"
    elif target == "keep":
        to = "keep current lineup (follow off)"
    elif target == "fallback":
        to = f"fallback → [{target_provider or '—'} ▾]"
    else:
        to = "direct (no agents)"
    lines.append(("to", to, "normal"))
    if preview is not None:
        for index, line in enumerate(preview):
            lines.append(("plan" if index == 0 else "", line.strip(), "normal"))
        for error in errors:
            lines.append(("✗", error, "error"))
        return DialogModel(title=f"change lineup — {head} ({word})", lines=tuple(lines),
                           can_apply=not errors and bool(preview), focus=focus)
    if diff:
        for index, line in enumerate(diff):
            lines.append(("diff" if index == 0 else "", line.strip(), "normal"))
    else:
        lines.append(("diff", "(no change)", "dim"))
    if errors:
        for error in errors:
            lines.append(("✗", error, "error"))
    elif mode == "LIVE" and state == "ended":
        # The session is not running: written to its scope now, used at its next resume.
        lines.append(("mode", "applies at the next resume — the session is not running", "ok"))
        lines.append(("next", "its scope is updated now; the session uses it when it resumes", "normal"))
    elif mode == "LIVE":
        lines.append(("mode", "LIVE — running agents keep their model; new spawns use the new lineup", "ok"))
        if state in ("live", "unknown"):
            lines.append(("next", "run /reload-plugins in that session", "normal"))
        else:
            lines.append(("next", "the session uses it when it resumes", "normal"))
        if lead_switch:
            lines.append(
                ("lead", f"switch with /model → {lead_switch} (press s: this session only)", "normal")
            )
    elif mode == "RELAUNCH":
        lines.append(("mode", "RELAUNCH — exit the session; resume applies it", "warn"))
        for reason in reasons:
            lines.append(("why", reason, "normal"))
        lines.append(("next", "recorded as a pending change; it applies at the session's next resume", "normal"))
    return DialogModel(
        title=f"change lineup — {head} ({word})",
        lines=tuple(lines),
        can_apply=not errors and mode is not None,
        focus=focus,
    )


# ================================================================ models (§4.8)


@dataclass(frozen=True)
class ModelsModel:
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]  # main table: active/admitted catalog lines, then custom
    keys: tuple[str, ...]  # line key per main row
    new_heading: str
    new_rows: tuple[tuple[str, ...], ...]
    new_keys: tuple[str, ...]
    retired: str
    details: Mapping[str, tuple[str, ...]]  # key -> detail line (+ radar line)
    admitted: frozenset[str]
    # Operator lines: Enter routes to the guarded admission flow,
    # never a Settings-only admission.
    guarded: frozenset[str] = frozenset()


MODELS_COLUMNS = ("line", "generation", "provider", "class", "efforts", "used by")


def models_model(
    rows: Sequence[LineRow],
    *,
    used: Mapping[str, int],
    retired: Mapping[str, str | None],
    continuity_count: int,
    catalog_version: int,
    radar: Mapping[str, str],
    width: int = 80,
) -> ModelsModel:
    """The models screen (§4.8).

    ``retired`` maps each retired key to its resolved successor (None for
    a null chain); ``radar`` maps a line key to its doctor radar line.
    """

    def efforts(row: LineRow) -> str:
        text = efforts_text(row.efforts)
        return text if row.agents_capable else f"{text} (lead recommended)"

    def used_text(row: LineRow) -> str:
        count = used.get(row.key, 0)
        return _plural(count, "profile") if count else "—"

    main: list[tuple[str, ...]] = []
    keys: list[str] = []
    for row in rows:
        if row.source == "catalog" and (row.status != "new" or row.admitted):
            main.append((row.key, row.display, row.provider, row.class_label, efforts(row), used_text(row)))
            keys.append(row.key)
    for row in rows:
        if row.source == "custom":
            main.append((row.key, row.display, "custom", row.class_label, efforts(row), used_text(row)))
            keys.append(row.key)
    new_rows: list[tuple[str, ...]] = []
    new_keys: list[str] = []
    for row in rows:
        if row.source == "catalog" and row.status == "new" and not row.admitted:
            new_rows.append((row.key, row.display, row.provider, row.class_label, efforts(row),
                             "New · unavailable" if row.unavailable_reason else "New · not admitted"))
            new_keys.append(row.key)
    heading = "new (not admitted — Enter adds an optional badge):" + ("" if new_rows else " —")
    groups: dict[str | None, list[str]] = {}
    for key, successor in retired.items():
        groups.setdefault(successor, []).append(key)
    pieces = [
        f"{' '.join(sorted(keys_))} → {successor if successor is not None else '(none)'}"
        for successor, keys_ in sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or ""))
    ]
    if continuity_count:
        pieces.append(f"{continuity_count} continuity aliases retained")
    retired_text = "retired  " + (" · ".join(pieces) if pieces else "—")
    retired_text = _wrap(retired_text, max(20, width - 3), indent=" " * 9)
    details: dict[str, tuple[str, ...]] = {}
    for row in rows:
        detail = (
            f"{row.display} · {row.provider_display} · {row.mode}-effort · default {row.default_effort}"
        )
        if row.status == "new" and row.admitted:
            detail += " · admitted (New, stored in Settings)"
        if row.origin in profile.OPERATOR_ORIGINS:
            # The screen's own key does it (the bar names Enter's action here);
            # the command line spelling stays in V and on the command line.
            detail += " · operator line"
        availability = row.unavailable_reason or ("available" if row.offered else "unavailable — check Providers (G)")
        badge = "admitted" if row.admitted else "not admitted"
        family = "recognized" if row.family_recognized else "independence unknown"
        lines = (detail, f"use: {availability} · admission: {badge} (optional)",
                 f"qualification: {row.qualification} · family: {row.family} ({family})")
        lines += ((radar[row.key],) if row.key in radar else ())
        details[row.key] = lines
    return ModelsModel(
        title=f"models — catalog {catalog_version}",
        columns=MODELS_COLUMNS,
        rows=tuple(main),
        keys=tuple(keys),
        new_heading=heading,
        new_rows=tuple(new_rows),
        new_keys=tuple(new_keys),
        retired=retired_text,
        details=details,
        admitted=frozenset(row.key for row in rows if row.status == "new" and row.admitted),
        guarded=frozenset(row.key for row in rows if row.origin in profile.OPERATOR_ORIGINS),
    )


def operator_admission_command(key: str, admitted: bool) -> str:
    """The guarded CLI verb for an operator line (never Settings-only)."""

    return f"claude-multi models {'revoke' if admitted else 'admit'} {key}"


def operator_admission_refusal(key: str, admitted: bool) -> str:
    """Models Enter on an operator line: the guarded flow runs in a terminal."""

    return (f"{key} is an operator line: admission is guarded — run "
            f"`{operator_admission_command(key, admitted)}` in a terminal outside Claude Code sessions")


LINE_ORIGINS = {"catalog": "shipped with claude-multi", "legacy-custom": "custom registry",
                "operator": "a model you added", "operator-migrated": "a model you added (migrated)"}


def line_selectors(entry: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(effort, selector)`` pairs a line offers: one selector for every
    effort of a client-effort line, one per effort otherwise. The names a
    session's ``/model`` and the agent files use."""

    efforts = entry.get("efforts")
    if isinstance(efforts, Mapping):
        return tuple((str(level), str(spec.get("selector", ""))) for level, spec in efforts.items()
                     if isinstance(spec, Mapping))
    selector = str(entry.get("selector", ""))
    return tuple((str(level), selector) for level in (efforts or ()))


def line_inspection(
    entry: Mapping[str, Any], row: LineRow, *, used: int = 0, radar: str | None = None, key_route: bool = False,
) -> tuple[str, ...]:
    """The full details of one model line: origin, provider, wire model,
    selectors per effort, context bounds and their evidence, admission and
    qualification. Plain facts from the line; nothing is observed.
    ``key_route``: the line's provider uses its API key now, so a line
    reviewed for that route shows the route's own evidence (documented
    bounds, the conservative validated floor), never the account route's."""

    route = catalog.key_route(entry) if key_route else None
    context = entry.get("context") if isinstance(entry.get("context"), Mapping) else {}
    lines = [
        f"{row.display} ({row.key})",
        f"origin: {LINE_ORIGINS.get(row.origin, row.origin)}",
        f"provider: {row.provider_display} ({row.provider}) · family {row.family}",
        f"wire model: {entry.get('wire_model', '—')}",
        f"efforts: {efforts_text(row.efforts)} · default {row.default_effort} · "
        + ("effort travels in the agent file (one selector)" if row.mode == "client"
           else "one selector per effort"),
    ]
    lines += [f"  {level}: {selector}" for level, selector in line_selectors(entry)]
    roles = "all agent roles" if row.roles == "all" else ", ".join(row.roles) or "none"
    lines.append(f"recommendations — leads: {'yes' if row.lead_capable else 'no'} · agents: "
                 + (roles if row.agents_capable else "not recommended"))
    bounds = [f"client class {row.class_label}"]
    if route is not None:
        evidence = {"provider_tokens": route["input_tokens"], "validated_tokens": catalog.key_route_floor(route),
                    "declared_tokens": route["context_tokens"], "output_tokens": route.get("output_tokens")}
    else:
        evidence = dict(context)
    for name, label in (("provider_tokens", "provider bound"), ("validated_tokens", "validated"),
                        ("declared_tokens", "declared"), ("output_tokens", "output")):
        value = evidence.get(name)
        if isinstance(value, int) and value > 0:
            bounds.append(f"{label} {profile.format_tokens(value)}")
    lines.append("context: " + " · ".join(bounds))
    if route is not None:
        lines.append(f"context evidence: {row.provider_display} API key in use — from the vendor's documentation "
                     f"({route['source_ref']}, checked {route['checked']}), not measured on this route; the "
                     "validated figure is the conservative floor, and the account route's measurement does "
                     "not apply")
        lines.append(f"efforts on the API key: {', '.join(catalog.key_route_levels(entry)) or 'none'}")
    elif context.get("qualification"):
        lines.append(f"context evidence: {context['qualification']}")
    availability = row.unavailable_reason or ("available" if row.offered else "unavailable — check Providers (G)")
    lines.append(f"use: {availability}")
    state = "admitted (stored in Settings)" if row.admitted else "not admitted"
    lines.append(f"admission: {state} — optional badge; revoke keeps availability and evidence")
    lines.append(f"qualification: {row.qualification}")
    if row.origin in profile.OPERATOR_ORIGINS:
        lines.append(f"optional diagnostics: Q (claude-multi models qualify {row.key} --agents); consent before requests")
    lines.append("family independence: " + ("recognized family; depends on the review pair" if row.family_recognized
                                             else "unknown — this label does not establish independence"))
    lines.append(f"used by: {_plural(used, 'profile') if used else 'no profile'}")
    if radar:
        lines.append(radar)
    return tuple(lines)


def _wrap(text: str, width: int, *, indent: str) -> str:
    """Greedy word wrap on `` · `` boundaries with a continuation indent."""

    if len(text) <= width:
        return text
    parts = text.split(" · ")
    lines: list[str] = []
    current = parts[0]
    for part in parts[1:]:
        if len(current) + 3 + len(part) <= width:
            current += " · " + part
        else:
            lines.append(current + " ·")
            current = indent + part
    lines.append(current)
    return "\n".join(lines)


# ================================================================ direct (§4.9)


@dataclass(frozen=True)
class DirectRow:
    key: str
    short: str
    provider: str
    class_label: str
    lead_class: str | None
    mode: str
    efforts: tuple[str, ...]
    default_effort: str
    source: str
    mark: str | None
    provider_width: int = 11
    unavailable_reason: str = ""
    attention: str = ""

    def text(self, effort: str | None = None) -> str:
        """``{provider} {short:<22} {class:<5}{  effort [e]}{  ({mark})}``; the
        provider column is as wide as the longest provider id listed."""

        if self.source == "custom":  # a legacy custom line
            return (f"{'custom':<{self.provider_width}} {self.short:<22} {self.class_label}   (custom)"
                    + (f"  ({self.mark})" if self.mark else ""))
        text = f"{self.provider:<{self.provider_width}} {self.short:<22} {self.class_label:<5}"
        if self.mode == "gateway":
            text += f"  effort [{effort or self.default_effort}]"
        if self.mark:
            text += f"  ({self.mark})"
        return text.rstrip()


def direct_rows(rows: Sequence[LineRow], *, marks: Mapping[str, str | None]) -> tuple[DirectRow, ...]:
    """Every valid declaration; unavailable routes stay visible with remedies."""

    first_rows = sorted((r for r in rows if r.source == "catalog"),
                        key=lambda r: (r.provider_display.lower(), r.display.lower(), r.key))
    custom_source = [r for r in rows if r.source == "custom"]
    width = max([len(r.provider) for r in first_rows] + [len("custom")] * bool(custom_source) + [1])

    def one(row: LineRow) -> DirectRow:
        unavailable = row.unavailable_reason
        if not row.provider_enabled:
            unavailable = "provider off — G → Space enables it"
        elif not row.lead_usable:
            unavailable = "missing lead/context fields — edit the model definition"
        attention = []
        if row.status == "new" and not row.admitted:
            attention.append("not admitted (optional badge)")
        if not row.lead_capable:
            attention.append("lead binding overrides capability recommendation")
        if row.origin != "catalog":
            attention.append("qualification: " + row.qualification)
        if not row.family_recognized:
            attention.append("family independence unknown")
        return DirectRow(
            key=row.key,
            short=row.short,
            provider=row.provider,
            class_label=row.class_label,
            lead_class=row.lead_class,
            mode=row.mode,
            efforts=row.efforts,
            default_effort=row.default_effort,
            source=row.source,
            mark="unavailable" if unavailable else marks.get(row.key),
            provider_width=width,
            unavailable_reason=unavailable,
            attention="; ".join(attention),
        )

    return tuple(one(r) for r in first_rows) + tuple(one(r) for r in custom_source)


def direct_detail(row: DirectRow, rows: Sequence[DirectRow], *, reason: str | None = None) -> str:
    """The detail line: the mark reason, or ``lead class {class} · {n} models in that class``."""

    if row.unavailable_reason:
        return row.unavailable_reason
    count = sum(1 for other in rows if other.lead_class == row.lead_class)
    detail = reason or f"lead class {row.lead_class} · {_plural(count, 'model')} in that class"
    return detail + (f" · Attention: {row.attention}" if row.attention else "")


# ================================================================ providers (§4.10)


@dataclass(frozen=True)
class ProviderRow:
    id: str
    cells: tuple[str, ...]  # provider, enabled, credential, quota, models, served
    span: str | None  # models_note across ``models``/``served`` for zero-model rows
    enabled: bool
    kind: str
    custom: bool
    details: tuple[str, ...]


PROVIDERS_COLUMNS = ("provider", "on", "credential", "quota", "models", "served", "src", "route")
# A provider with no model line yet: drawn across the served column.
PROVIDERS_NO_MODELS = "no models yet — A adds models"
# The credential vocabulary: an API key is set, missing or invalid; an
# account is signed in (×n), not signed in, or its sign-in is failing.
CREDENTIAL_TEXT = {
    "key-set": "key set", "key-missing": "key missing", "key-invalid": "key invalid",
    "signed-in": "signed in", "signed-in-n": "signed in ×{n}", "not-signed-in": "not signed in",
    "failing": "sign-in failing", "keyless": "keyless",
    "api-key-set": "API key set", "api-key-missing": "API key missing",
}
ROUTE_TEXT = {"approved": "approved", "unapproved": "not approved", "changed": "changed"}
PROVIDER_KEY_SET = "API key {name} is set — K replaces it, X removes it"


def _key_guidance(fact: Mapping[str, Any]) -> str:
    """The row's guidance, saying a key that is set is set (the fact's
    ``set … with K`` is for a key still to be entered)."""

    guidance = str(fact.get("guidance") or "")
    credential = str(fact.get("credential", ""))
    if guidance.endswith(" with K") and credential.endswith(" present"):
        return PROVIDER_KEY_SET.format(name=credential.removesuffix(" present"))
    return guidance


SIGNIN_REMEDY = "sign-in failing — L signs in again (personal use)"
SIGNIN_GUIDANCE = "sign in: L (personal use; it runs in this terminal)"
# An account provider already signed in: L adds an account or signs out;
# a quota or management state is never a reason to sign in again.
SIGNED_IN_GUIDANCE = "signed in — L signs in another account or signs out (personal use)"


def _account_guidance(fact: Mapping[str, Any], journal: Any) -> str:
    """The sign-in line of an account provider: how to sign in while none
    is, what L does once one is."""

    cell = _credential_cell(fact, journal)
    signed = cell not in (CREDENTIAL_TEXT["not-signed-in"], CREDENTIAL_TEXT["failing"]) and cell.startswith("signed in")
    return SIGNED_IN_GUIDANCE if signed else SIGNIN_GUIDANCE


def api_key_transport(fact: Mapping[str, Any]) -> bool:
    """An account provider whose Anthropic API key transport is selected."""

    return str(fact.get("transport_label", "")).startswith("transport: api-key")


def _credential_cell(fact: Mapping[str, Any], journal: Any) -> str:
    kind = fact.get("kind")
    credential = str(fact.get("credential", ""))
    if kind == "oauth-pool":
        pool = fact.get("pool")
        dead = getattr(journal, "dead_pools", None) or {}
        if pool is not None and pool in dead:
            return CREDENTIAL_TEXT["failing"]
        count = fact.get("records")
        if count is None:
            try:
                count = int(credential.split(" ", 1)[0])
            except ValueError:
                return credential
        if not count:
            return CREDENTIAL_TEXT["not-signed-in"]
        return CREDENTIAL_TEXT["signed-in"] if int(count) == 1 else CREDENTIAL_TEXT["signed-in-n"].format(n=count)
    if kind == "direct-openai" and fact.get("auth", "none") == "none":
        return CREDENTIAL_TEXT["keyless"]  # a keyed compat route shows its key state
    prefix = "api-" if api_key_transport(fact) else ""
    if credential.endswith(" present"):
        return CREDENTIAL_TEXT[prefix + "key-set"]
    if credential.endswith(" missing"):
        return CREDENTIAL_TEXT[prefix + "key-missing"]
    if credential.endswith(" invalid"):
        return CREDENTIAL_TEXT["key-invalid"]
    return credential


def provider_rows(facts: Any, *, eff: settings.Effective, journal: Any,
                  pool: quota.PoolStatus | None = None, now: datetime | None = None,
                  tz=None, login_commands: Mapping[str, str] | None = None,
                  restart_hint: str = "restart the gateway", detail_width: int = 77
                  ) -> tuple[ProviderRow, ...]:
    """The providers table from ``_provider_facts`` rows.

    Each fact carries ``id``, ``kind``, ``custom``, ``credential``,
    ``served_note``, ``models_note``, ``guidance`` and ``models``
    (the line count) and ``pool`` (oauth pools); optional ``selectors`` (the
    provider's served aliases) attribute journal quota counts.  ``journal``
    has ``dead_pools`` and ``quota`` (``{(model, code): count}``) or is None.
    """

    now = now or datetime.now(timezone.utc)
    items = getattr(facts, "facts", facts)
    out: list[ProviderRow] = []
    journal_quota = getattr(journal, "quota", None) or {}
    for fact in items:
        pid = str(fact["id"])
        enabled = bool(fact["enabled"]) if "enabled" in fact else settings.provider_enabled(eff, pid)
        served = str(fact.get("served_note", "")).removesuffix(" served")
        models = fact.get("models")
        span = PROVIDERS_NO_MODELS if fact.get("models_note") else None
        credentials = tuple(c for c in pool.credentials if c.provider == fact.get("pool")) if (
            pool is not None and pool.state == "ok" and fact.get("kind") == "oauth-pool"
        ) else ()
        finding = quota.worst(credentials, now) if now is not None else None
        credential = _credential_cell(fact, journal)
        if finding is not None and finding.level in {"login", "unusable"}:
            credential = CREDENTIAL_TEXT["failing"]
        quota_cell = ""
        if fact.get("kind") == "oauth-pool":
            quota_cell = quota.provider_cell(credentials, now) if pool and pool.state == "ok" else "—"
        route = str(fact.get("route", "n/a"))
        cells = (
            pid,
            "on" if enabled else "off",
            credential,
            quota_cell,
            "" if models is None else str(models),
            served if span is None else "",
            {"catalog": "cat", "operator": "op", "custom": "legacy", "operator-migrated": "op"}.get(fact.get("source"), "cat"),
            ROUTE_TEXT.get(route, route),
        )
        details: list[str] = []
        if fact.get("kind") == "oauth-pool":
            details.append(_account_guidance(fact, journal))
        elif fact.get("guidance"):
            details.append(_key_guidance(fact))
        selectors = set(fact.get("selectors") or ())
        for (model, code), count in sorted(journal_quota.items()):
            if model in selectors:
                details.append(
                    f"{model}: {count}× {code} ({_QUOTA_WORDS.get(code, code)}) "
                    f"{_QUOTA_WINDOWS.get(code, '')}".rstrip()
                    + (" (partial journal read)" if code != "429" and getattr(journal, "partial", None) else "")
                )
        if not enabled:
            details.append(
                "off in Settings: new sessions do not fence its models; running sessions keep their routes"
            )
        if fact.get("custom"):
            details.append("custom provider — edit or remove it with claude-multi custom …")
        if fact.get("kind") == "oauth-pool" and pool is not None and pool.state not in {"seam", "down"}:
            details = _provider_quota_details(
                fact, pool, credentials, finding, now, tz, enabled, journal,
                journal_quota, selectors, login_commands or {}, restart_hint, detail_width,
            )
        note = str(fact.get("models_note") or "")
        if note and note != "configured · no models":
            details.append(note)  # retained aliases of a provider with no model line
        if fact.get("transport_label"):
            details.insert(0, str(fact["transport_label"]))
        out.append(
            ProviderRow(
                id=pid,
                cells=cells,
                span=span,
                enabled=enabled,
                kind=str(fact.get("kind", "")),
                custom=bool(fact.get("custom")),
                details=tuple(details),
            )
        )
    return tuple(out)


def _provider_quota_details(fact, pool, credentials, finding, now, tz, enabled, journal,
                            journal_quota, selectors, login_commands, restart_hint, width):
    """Two visible lines, not a list silently cut off by the screen.

    Line 1: refresh/sign-in remedy > management Attention remedy > quota fallback
    > Settings-off guidance > ordinary guidance. Info-level quota-off states
    stay on line 2, after journal counts. Otherwise line 2: Settings-off,
    journal status counts, worst observation age, +N-more route, reset detail.
    Healthy management never disproves the journal's refresh failure.
    """
    login = login_commands.get(str(fact.get("pool")))
    dead = fact.get("pool") in (getattr(journal, "dead_pools", None) or {})
    auth_bad = finding is not None and finding.level in {"login", "unusable"}
    if dead or auth_bad:
        first = SIGNIN_REMEDY
    elif pool.state != "ok" and pool.state not in quota.INFO_STATES:
        # The read-only CLI has the full category-specific repair. Keep its
        # route first so a long state explanation cannot hide the remedy.
        first = "claude-multi quota — " + (quota.state_text(pool, restart_hint=restart_hint) or "")
    elif finding and finding.level in {"high", "exhausted", "payment"}:
        first = quota.guidance(str(fact["id"]), "providers")
    elif not enabled:
        first = "off in Settings: new sessions exclude it; running sessions keep their routes"
    else:
        first = _account_guidance(fact, journal)
    notes = []
    if not enabled and not first.startswith("off in Settings"):
        notes.append("off in Settings")
    counts: dict[str, int] = {}
    for (model, code), count in journal_quota.items():
        if model in selectors:
            counts[code] = counts.get(code, 0) + count
    if counts:
        notes.append("journal " + ", ".join(f"{n}× {code}" for code, n in sorted(counts.items())))
    if pool.state == "ok" and now is not None:
        observations = quota.provider_details(credentials, now, tz, login_command=login)
        if finding:
            c = finding.credential
            notes.append(f"{c.handle} · {quota.observation_age(c, now, compact=True)}")
            notes.extend(observations[1:])
            notes.append(observations[0].split(" · ", 2)[-1])
        else:
            notes.extend(observations)
    elif pool.state == "unavailable":
        notes.append("quota unavailable in this build")
    elif pool.state in quota.INFO_STATES:
        notes.append("quota off — " + ("no management key" if pool.state == "no-key" else "disabled by operator"))
    elif pool.state != "ok":
        notes.append(f"quota {pool.state} — claude-multi quota")
    return [clip(first, width), clip(" · ".join(notes), width)]


# ================================================================ settings (§4.11)


@dataclass(frozen=True)
class SettingsRow:
    section: str
    key: str  # settings key, or "effective"/"ceiling"/"reserve"/"providers"/"admitted"/"token"
    label: str
    value: str
    note: str
    when: str  # "next" | "live" | ""
    edit: str  # "int" | "toggle" | "choice" | "picker" | "providers" | "models" | "token" | ""
    detail: str
    editable: bool

    def text(self, width: int) -> str:
        """Label col 2 (28), value col 30 (10), note col 41 (clipped), ``when`` ending at ``width - 3``."""

        if self.key == "effective":
            return clip("  " + self.label, max(0, width - 3))
        head = f"  {self.label:<28}{self.value:<10} "
        tail = self.when
        room = max(0, width - 3 - len(head) - (len(tail) + 1 if tail else 0))
        note = clip(self.note, room)
        line = head + note
        if tail:
            line = line.ljust(max(len(line) + 1, width - 3 - len(tail))) + tail
        return line


SETTINGS_SECTIONS = ("Context & compaction", "Agents", "Claude Code", "Providers & models", "Gateway")
# The context window ceiling row (``choices.json``, not ``settings.json``).
SETTINGS_CEILING_KEY = "ceiling"
SETTINGS_CEILING_LABEL = "context window ceiling"
SETTINGS_CEILING_RANGE = (
    f"{profile.format_tokens(settings.WINDOW_CEILING_MIN)}–{profile.format_tokens(settings.WINDOW_CEILING_MAX)}"
)
SETTINGS_CEILING_DETAIL = (
    "The effective window is the smaller of this ceiling and the lead set's smallest provider bound; "
    "agent lines below it keep the 200K class."
)
SETTINGS_CEILING_UNREADABLE = "choices.json cannot be read, so launches refuse until it is fixed: {problem}"
SETTINGS_DEFAULT_KEY = "default_profile"
SETTINGS_DEFAULT_LABEL = "default profile"
SETTINGS_DEFAULT_AUTOMATIC = "automatic"
SETTINGS_DEFAULT_DETAIL = ("Used in a directory with no recent profile, when it is connected. automatic: the first "
                           "connected shipped profile.")
# The rows whose Enter opens another screen (never reset).
SETTINGS_NAVIGATION_EDITS = frozenset({"providers", "models"})


def _ceiling_row(section: str, ceiling: tuple[int, bool, str | None] | None) -> SettingsRow:
    """The context window ceiling: its value and source (set or default)."""

    value, is_set, problem = ceiling if ceiling is not None else (settings.WINDOW_CEILING_DEFAULT, False, None)
    source = "set" if is_set and problem is None else "default"
    detail = SETTINGS_CEILING_UNREADABLE.format(problem=problem) if problem else SETTINGS_CEILING_DETAIL
    return SettingsRow(
        section, SETTINGS_CEILING_KEY, SETTINGS_CEILING_LABEL, profile.format_tokens(value),
        f"{source} · range {SETTINGS_CEILING_RANGE}", "next", "ceiling", detail, problem is None,
    )


def settings_rows(
    doc: Mapping[str, Any] | None,
    eff: settings.Effective,
    *,
    overrides: Mapping[str, int],
    context: tuple[str, str, Any] | None,
    token_state: tuple[float | None, bool],
    lcat: Any = None,
    feedback_drafts: tuple[str, str | None] = (settings.FEEDBACK_DRAFTS_DEFAULT, None),
    default_profile: tuple[str | None] | None = None,
    window_ceiling: tuple[int, bool, str | None] | None = None,
) -> tuple[SettingsRow, ...]:
    """The Settings rows: the edited fields, summaries, token.

    ``feedback_drafts`` is ``(value, problem)`` of the feedback-drafts host
    preference (``preferences.json``, not ``settings.json``); a problem makes
    the row read-only with the default shown.

    ``doc`` None means ``settings.json`` is invalid: every row shows its
    default and is read-only.  ``overrides`` maps a profile name to its
    ``compaction_percent`` override.  ``context`` is ``(profile label,
    short lead display, LeadContext or its document)`` or None.
    ``token_state`` is ``(api-key mtime or None, rotation in progress)``.
    ``default_profile`` is ``(chosen default or None)`` from ``choices.json``;
    when given, the default-profile row follows the feedback drafts row (it
    is a choice of its own, so an invalid ``settings.json`` never locks it).
    ``window_ceiling`` is ``(value, set, problem)`` of the context window
    ceiling (``choices.json``; None: the default): an editable row of its
    own, read-only with the default shown when the file cannot be read.
    """

    readonly = doc is None
    if readonly:
        eff = settings.Effective(
            providers_enabled=dict(eff.providers_enabled), admitted_lines=frozenset(), unknown=(),
            unavailable_lines=eff.unavailable_lines
        )
    rows: list[SettingsRow] = []
    sec = SETTINGS_SECTIONS
    rows.append(_ceiling_row(sec[0], window_ceiling))
    percent = eff.compaction_percent
    diamond = any(value != percent for value in overrides.values())
    detail = (
        "Compaction trigger percent (process env and the reactive trigger); a profile may "
        "override it (◆). Applies at the next launch or resume."
    )
    if overrides:
        detail += " ◆ overridden by: " + ", ".join(
            f"{name} ({value} %)" for name, value in sorted(overrides.items())
        )
    rows.append(
        SettingsRow(
            sec[0], settings.COMPACTION_PERCENT_KEY, "auto-compact at",
            f"{percent} %" + (" ◆" if diamond else ""),
            f"default {settings.COMPACTION_PERCENT_DEFAULT} %  range "
            f"{settings.COMPACTION_PERCENT_MIN}–{settings.COMPACTION_PERCENT_MAX}",
            "next", "int", detail, not readonly,
        )
    )
    rows.append(
        SettingsRow(
            sec[0], "reserve", "output reserve", profile.format_tokens(OUTPUT_RESERVE),
            "pinned (client policy)", "", "", "Client policy constant; shown, not editable.", False,
        )
    )
    if context is None:
        effective = "→ effective: open from the card to see a profile's numbers"
    else:
        name, lead, ctx = context
        values = ctx.as_document() if hasattr(ctx, "as_document") else ctx
        effective = (
            f"→ effective for {name} ({lead}): window {profile.format_tokens(values['window'])} · "
            f"compacts at ~{profile.format_tokens(values['trigger'])}"
        )
    rows.append(SettingsRow(sec[0], "effective", effective, "", "", "", "", "", False))
    default_on = "on" if settings.EXPLORE_INHERIT_CAP_DISABLED_DEFAULT else "off"
    rows.append(
        SettingsRow(
            sec[1], settings.EXPLORE_INHERIT_CAP_DISABLED_KEY, "Explore follows lead model",
            "on" if eff.explore_inherit_cap_disabled else "off", f"default {default_on}", "next",
            "toggle",
            "on: native Explore uses the lead's model (CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP=1). "
            "off: the client caps Explore to Opus under a Fable lead only; GPT leads inherit either way.",
            not readonly,
        )
    )
    binding = eff.workflow_default_binding
    if binding is None:
        value = "off"
    else:
        model = binding["model"]
        shown = model
        if lcat is not None:
            try:
                key = lcat.resolve_key(model).key
            except catalog.CatalogError:
                key = None
            if key is not None:
                shown = short_label(lcat.lines[key].get("display", key))
        value = f"{shown} · {binding['effort']}"
    rows.append(
        SettingsRow(
            sec[1], settings.WORKFLOW_DEFAULT_BINDING_KEY, "workflow default binding", value,
            "default off", "next", "picker",
            "Model for workflow agents started without a cm-* agentType; an agents-capable "
            "offered model (it joins every new session's fence).",
            not readonly,
        )
    )
    rows.append(
        SettingsRow(
            sec[1], settings.REVIEW_ROUND_CAP_KEY, "review rounds (max)", str(eff.review_round_cap),
            f"default {settings.REVIEW_ROUND_CAP_DEFAULT}  range "
            f"{settings.REVIEW_ROUND_CAP_MIN}–{settings.REVIEW_ROUND_CAP_MAX}",
            "next", "int",
            "Review rounds per change set before the lead decides and reports open "
            "disagreement (stated in lineup.md).",
            not readonly,
        )
    )
    drafts, drafts_problem = feedback_drafts
    rows.append(
        SettingsRow(
            sec[2], settings.FEEDBACK_DRAFTS_KEY, "feedback drafts", drafts,
            f"default {settings.FEEDBACK_DRAFTS_DEFAULT}", "next", "choice",
            ("preferences.json is unreadable, so the default applies: " + drafts_problem)
            if drafts_problem else
            ("off: managed sessions never offer Claude Code's SendFeedback tool; notify: the "
             "client's own default. Plain claude is never changed (preferences.json)."),
            drafts_problem is None,
        )
    )
    if default_profile is not None:
        chosen = default_profile[0]
        rows.append(
            SettingsRow(
                sec[2], SETTINGS_DEFAULT_KEY, SETTINGS_DEFAULT_LABEL, chosen or SETTINGS_DEFAULT_AUTOMATIC,
                "", "next", "default-profile", SETTINGS_DEFAULT_DETAIL, True,
            )
        )
    enabled = sum(1 for value in eff.providers_enabled.values() if value)
    rows.append(
        SettingsRow(
            sec[3], "providers", "providers enabled", f"{enabled} of {len(eff.providers_enabled)}",
            "G opens providers", "next", "providers",
            "Switching a provider off narrows new sessions' fences only; credentialed providers "
            "stay rendered, so running sessions keep their routes.",
            True,
        )
    )
    rows.append(
        SettingsRow(
            sec[3], "admitted", "Optional admission badges", str(len(eff.admitted_lines)),
            "M opens models", "now", "models",
            "Admission is an optional badge, not availability. M → Enter adds or removes it; evidence is unchanged.", True,
        )
    )
    mtime, rotating = token_state
    if rotating:
        note = "rotation in progress"
    elif mtime is None:
        note = "last rotated —"
    else:
        note = "last rotated " + datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d")
    rows.append(
        SettingsRow(
            sec[4], "token", "token", "rotate…", note, "live", "token",
            "Hitless dual-key rotation (claude-multi doctor --rotate-token); running sessions "
            "switch within one helper TTL.",
            True,
        )
    )
    return tuple(rows)


# The kept environment row (``choices.json`` ``session_env_keep``).
SETTINGS_ENV_KEEP_KEY = "session_env_keep"
SETTINGS_ENV_KEEP_LABEL = "kept environment"
SETTINGS_ENV_KEEP_DETAIL = (
    "API-key variables (names only) a managed session keeps; every other *_API_KEY is removed, so tools "
    "the session starts (MCP servers too) see only these. Provider keys are never kept. Applies at the "
    "next launch or resume."
)


def settings_env_keep_row(names: Sequence[str], *, refused: Sequence[tuple[str, str]] = (),
                          problem: str | None = None) -> SettingsRow:
    """The Settings row of ``session_env_keep``: the names (never a value;
    the caller passes them as ``secret_store.shown_env_name`` shows them),
    each name a launch removes anyway with its reason, or the document's
    problem (then read-only)."""

    if problem is not None:
        return SettingsRow(SETTINGS_SECTIONS[2], SETTINGS_ENV_KEEP_KEY, SETTINGS_ENV_KEEP_LABEL, "none",
                           "choices.json is unreadable", "next", "env-keep",
                           "choices.json is unreadable, so nothing is kept: " + problem, False)
    value = _plural(len(names), "name") if names else "none"
    note = ", ".join(names) if names else "default none"
    detail = SETTINGS_ENV_KEEP_DETAIL
    if refused:
        detail += " Removed anyway: " + "; ".join(f"{name} ({reason})" for name, reason in refused) + "."
    return SettingsRow(SETTINGS_SECTIONS[2], SETTINGS_ENV_KEEP_KEY, SETTINGS_ENV_KEEP_LABEL, value, note,
                       "next", "env-keep", detail, True)


# Draft form descriptions: pure data; no reads or authority.
PROVIDER_KIND_CHOICES = (
    ("anthropic-compatible", "anthropic-compatible — recommended (Messages)"),
    ("openai-compatible", "openai-compatible — with an API key"),
    ("openai-compatible-lan", "openai-compatible-lan — keyless LAN"),
)
PROVIDER_KIND_CLOSED_LABEL = "(openai-compatible with an API key: not available in this release)"


def provider_kind_choices(*, keyed_audited: bool = True) -> tuple[tuple[str, str], ...]:
    """The kinds the manual form offers: without the keyed audit the keyed
    OpenAI-compatible kind is dropped and the closed note leads (it never
    selects a kind: choosing it keeps the recommended one)."""

    if keyed_audited:
        return PROVIDER_KIND_CHOICES
    return tuple(choice for choice in PROVIDER_KIND_CHOICES if choice[0] != "openai-compatible")


def provider_form_fields(*, kind="anthropic-compatible", keyed_audited: bool = True):
    choices = provider_kind_choices(keyed_audited=keyed_audited)
    label = "Generation protocol (Left/Right chooses)"
    if not keyed_audited:
        label += " " + PROVIDER_KIND_CLOSED_LABEL
    return (
        ("id", "Provider id", "", ()),
        ("kind", label, kind, choices),
        ("url", "Base URL (vendor-documented endpoint)", "", ()),
        ("auth", "Authentication", "none" if kind == "openai-compatible-lan" else "bearer",
         endpoint_auth_choices(kind)),
        ("secret", "Logical secret name (NAME, not a key value)", "", ()),
        ("family", "Model family label (unknown if unsure)", "unknown", ()),
        ("contracts", "Payload contracts (comma-separated, optional)", "", ()),
        ("listing", "Listing URL (optional; no protocol autodetection)", "", ()),
        ("listing_auth", "Listing authentication", "provider", (("provider", "provider credential (same origin)"), ("none", "none (public listing)"))),
        ("shape", "Listing shape (not the generation protocol)", "openai", (("openai", "OpenAI listing"), ("anthropic", "Anthropic listing"))),
    )


ENDPOINT_AUTH_CHOICES = (("header", "x-api-key header"), ("bearer", "Authorization: Bearer"))


def endpoint_auth_choices(kind: str):
    if kind == "openai-compatible-lan":
        return (("none", "none (LAN)"),)
    return tuple(choice for choice in ENDPOINT_AUTH_CHOICES
                 if kind != "openai-compatible" or choice[0] == "bearer")


def endpoint_form_fields(kind: str):
    """The form of your own Anthropic- or OpenAI-compatible endpoint."""

    return (
        ("id", "Name for this provider (a–z, 0–9, -)", "", ()),
        ("url", "Base URL from your vendor's documentation (https://…)", "", ()),
        ("auth", "How the key is sent", "header" if kind == "anthropic-compatible" else "bearer",
         endpoint_auth_choices(kind)),
        ("family", "Who makes the models (a family like deepseek; unknown if unsure)", "unknown", ()),
        ("listing", "Model list URL (optional)", "", ()),
    )


def provider_edit_form_fields(provider: Mapping[str, Any]):
    """The edit form of a provider you added, prefilled from its
    declaration (the id, the kind and the key name stay)."""

    auth = provider.get("auth") if isinstance(provider.get("auth"), Mapping) else {}
    listing = provider.get("listing") if isinstance(provider.get("listing"), Mapping) else {}
    fields = [
        ("display", "Display name", str(provider.get("display", "")), ()),
        ("url", "Base URL from your vendor's documentation", str(provider.get("base_url", "")), ()),
    ]
    if auth.get("kind") in ("header", "bearer"):
        fields.append(("auth", "How the key is sent", str(auth["kind"]),
                       endpoint_auth_choices(provider.get("kind"))))
    fields.append(("family", "Who makes the models (a family like deepseek; unknown if unsure)",
                   str(provider.get("independence_family", "unknown")), ()))
    if provider.get("kind") != "openai-compatible-lan":
        fields.append(("listing", "Model list URL (blank: none)", str(listing.get("url", "")), ()))
    return tuple(fields)


def lan_form_fields():
    """The form of a server on your network (a preset; no key)."""

    return (
        ("id", "Name for this server", "lan", ()),
        ("url", "Server address (http://host:port/v1)", "", ()),
    )


PRESET_ADDRESS_LABEL = "Address (https://…; Enter keeps the preset's)"


def preset_form_fields(name: str, base_url: str = ""):
    """The form of a reviewed preset with an API key: its name (the
    preset's own by default) and its address (the preset's base URL by
    default; another address its vendor documents, such as a workspace
    endpoint, is checked like ``providers add --preset --base-url``);
    everything else comes from the preset."""

    return (
        ("id", "Name for this provider (a–z, 0–9, -)", name, ()),
        ("url", PRESET_ADDRESS_LABEL, base_url, ()),
    )


def model_form_fields(line, *, key="", family="unknown"):
    efforts = line.get("efforts", ["high"])
    effort_text = ",".join(f"{level}={contract}" for level, contract in efforts.items()) if isinstance(efforts, Mapping) else ",".join(efforts)
    roles = line.get("roles", [])
    return (
        ("wire", "Wire model id", line.get("wire_model", ""), ()),
        ("key", "Local key (custom- prefix)", key, ()),
        ("context", "Declared context tokens (not a measured floor)", str(line.get("context", {}).get("declared_tokens", "")), ()),
        ("source", "Context source", line.get("context", {}).get("source", "docs"),
         tuple((x, x) for x in ("docs", "listing", "registry", "operator"))),
        ("ref", "Context source reference (URL, date)", line.get("context", {}).get("source_ref", ""), ()),
        ("family", "Model family label (unknown if unsure)", line.get("family", family), ()),
        ("efforts", "Efforts: level or level=contract, comma-separated", effort_text, ()),
        ("default", "Default effort", line.get("default_effort", "high"), ()),
        ("output", "Max output tokens (optional; edits are operator-stated)", str(line.get("output", {}).get("declared_tokens", "")), ()),
        ("capabilities", "Recommended uses (bindings may override)", "agents" if "agents" in line.get("capabilities", []) else "lead",
         (("lead", "lead recommended"), ("agents", "lead + agents recommended"))),
        ("roles", "Recommended agent roles: all or comma-separated cm-* ids", roles if isinstance(roles, str) else ",".join(roles), ()),
    )


# The qualify form has no declaration preview: the consent plan is next.
QUALIFY_FORM_FOOTER = "Nothing is sent before the consent plan; qualification records evidence only."


def qualification_form_fields():
    return (
        ("checks", "Checks", "agents", (("agents", "agents: smoke + efforts + tools + stream"),
                                          ("smoke", "smoke"), ("tools", "tools"), ("context", "context"),
                                          ("efforts", "efforts"), ("stream", "stream"))),
        ("variant", "Tool contract (no weaker retry)", "forced", (("forced", "forced named tool"), ("auto", "strict automatic tool round trip"))),
        ("context", "Context test tokens (only for context check)", "", ()),
    )
