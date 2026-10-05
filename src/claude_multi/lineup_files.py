"""Names and pure helpers shared by the scope compiler, the hooks and ``/cm``.

``hooks.py`` must stay free of the compiler import chain (``scope`` imports
``compiler``, which imports ``catalog``), so the file names, the
``lineup.gen`` grammar, the model normalisation and the ``/cm`` verb table
every side needs live here, in a stdlib-only leaf module. ``scope.py``
re-exports the scope-content names; ``hooks.py`` imports this module
directly.

Nothing here touches the filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Scope-relative files of a lineup-enabled (v2) scope, besides
# ``settings.json`` and the agent files. All four are plan content
# (compiled, hashed, drift-checked).
LINEUP_MD = "lineup.md"
LINEUP_GEN = "lineup.gen"
LEAD_SET_JSON = "lead-set.json"
SKILL_RELPATH = ".claude/skills/cm/SKILL.md"
LINEUP_FILES = (LINEUP_MD, LINEUP_GEN, LEAD_SET_JSON, SKILL_RELPATH)

# lineup.md is injected into the lead's context by the notice (a 50K client
# limit); the compile refuses anything larger.
LINEUP_MD_MAX_BYTES = 16384

# ``lineup.gen``: "<generation> <sha256(lineup.md)[:12]>" plus one newline.
GEN_LINE = re.compile(r"^[1-9][0-9]{0,8} [0-9a-f]{12}$")
GENERATION_MAX = 999_999_999

# The lineup log lives OUTSIDE the scope (a hard link inside it would not
# survive the scope swap): ``<state>/lineup-log/<managed_id>.log``,
# owner-only 0600, bounded with one rotation to ``.1``. It is observability,
# never plan content, never drift, never a golden. Its effects (append with
# rotation, removal, orphan listing) are ``lineup_log.py``.
LINEUP_LOG_DIR = "lineup-log"
LINEUP_LOG_SUFFIX = ".log"
LINEUP_LOG_ROTATED_SUFFIX = ".log.1"
LINEUP_LOG_MAX_BYTES = 1024 * 1024

_ONE_M = "[1m]"


def normalize_model(value: str) -> str:
    """Lower-case with one trailing ``[1m]`` removed.

    The client lower-cases and strips ``[1m]`` itself (the recorded client
    evidence); the ``premodel`` hook matches ``to_model`` against lead-set
    selectors through this function only (exact match, never a prefix or a
    wire id).
    """

    lowered = value.lower()
    return lowered[: -len(_ONE_M)] if lowered.endswith(_ONE_M) else lowered


def lineup_log_path(state_root: Path | str, managed_id: str) -> Path:
    """``<state>/lineup-log/<managed_id>.log`` (the caller validates the id)."""

    return Path(state_root) / LINEUP_LOG_DIR / f"{managed_id}{LINEUP_LOG_SUFFIX}"


def lineup_log_rotated_path(state_root: Path | str, managed_id: str) -> Path:
    """The single rotation target ``<state>/lineup-log/<managed_id>.log.1``."""

    return Path(state_root) / LINEUP_LOG_DIR / f"{managed_id}{LINEUP_LOG_ROTATED_SUFFIX}"


# ------------------------------------------------------------ the /cm verbs


@dataclass(frozen=True)
class CmVerb:
    """One ``/cm`` (``claude-multi lineup``) request verb: the single
    definition the request parser, the in-session help line and the
    generated skill grammar read. ``args`` is the argument grammar after the
    verb in the help spelling (``<name>``, ``[optional]``); ``writes`` says
    whether the verb may change the session's lineup or record."""

    name: str
    args: str
    writes: bool
    summary: str

    def spelled(self, *, placeholders: str = "angle") -> str:
        """``verb args``; ``placeholders="upper"`` writes ``<agent>`` as ``AGENT``."""

        args = self.args
        if placeholders == "upper":
            args = re.sub(r"<([a-z-]+)>", lambda match: match[1].upper(), args)
        return f"{self.name} {args}".rstrip()


CM_VERBS: tuple[CmVerb, ...] = (
    CmVerb("show", "", False, "this session's lineup (also the empty request)"),
    CmVerb("profiles", "", False, "the profiles this session can switch to"),
    CmVerb("profile", "<name>", True, "follow another profile"),
    CmVerb("set", "<agent>=<model>[:<effort>]", True, "bind one agent"),
    CmVerb("unset", "<agent>", True, "unbind one agent"),
    CmVerb("direct", "[<model>[:<effort>]]", True, "a lead with no agents"),
    CmVerb("pin", "", True, "keep the current lineup and stop following the profile (drops a pending change)"),
    CmVerb("follow", "", True, "follow the session's profile again"),
    CmVerb("fallback", "<provider> [--preview]", True, "move the roles bound to a provider to a fallback lineup"),
    CmVerb("review", "[high-stakes] [<range>]", False, "ask for a review from another model family"),
    CmVerb("quota", "", False, "the quota of this session's accounts"),
)
CM_VERB_NAMES = tuple(verb.name for verb in CM_VERBS)
CM_READ_VERBS = frozenset(verb.name for verb in CM_VERBS if not verb.writes)
# The launcher-wide presentation switches. ``claude-multi lineup`` prints
# plain text, so it takes them before or after its name as no-ops
# (``claude-multi --no-color lineup show`` and ``claude-multi lineup
# --no-color show`` both show).
PRESENTATION_FLAGS = ("--line", "--no-color")


def cm_help_line() -> str:
    """The one-line ``/cm`` grammar the session shows (``/cm show`` help)."""

    return " · ".join(f"/cm {verb.spelled()}" for verb in CM_VERBS)


def cm_skill_description() -> str:
    """The skill frontmatter's verb list (the empty request is ``/cm``)."""

    return ", ".join("/cm" if verb.name == "show" else f"/cm {verb.spelled(placeholders='upper')}"
                     for verb in CM_VERBS)


def cm_argument_hint() -> str:
    """The skill's argument hint: every verb with arguments, upper-case placeholders."""

    return " · ".join(verb.spelled(placeholders="upper") for verb in CM_VERBS if verb.name != "show")


__all__ = [
    "CM_READ_VERBS",
    "CM_VERBS",
    "CM_VERB_NAMES",
    "CmVerb",
    "GENERATION_MAX",
    "GEN_LINE",
    "LEAD_SET_JSON",
    "LINEUP_FILES",
    "LINEUP_GEN",
    "LINEUP_LOG_DIR",
    "LINEUP_LOG_MAX_BYTES",
    "LINEUP_LOG_ROTATED_SUFFIX",
    "LINEUP_LOG_SUFFIX",
    "LINEUP_MD",
    "LINEUP_MD_MAX_BYTES",
    "SKILL_RELPATH",
    "lineup_log_path",
    "cm_argument_hint",
    "cm_help_line",
    "cm_skill_description",
    "lineup_log_rotated_path",
    "normalize_model",
]


HOOK_PROTOCOL = 3
