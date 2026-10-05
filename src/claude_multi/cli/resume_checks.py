"""Resume checks."""

from __future__ import annotations

from claude_multi import paths

from typing import Any
from pathlib import Path
import claude_multi.cli.text as cli_text
from dataclasses import dataclass
import claude_multi.cli.session_facts as session_facts
from claude_multi import sessions
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


# -- resume gate -------------------------------------------------
#
# One pure evaluator consumed by every resume surface: the sessions picker,
# the quick-confirm card, line mode, and Runtime.perform (mandatory
# backstop). The gate is metadata-only, takes no locks, and never mutates —
# stop/relink stay explicit operator-confirmed actions in the UI adapters.


@dataclass(frozen=True)
class ResumeGate:
    """Action-needed state for a resume target, or kind "ok"."""

    kind: str  # "ok" | "repair-needed" | "daemon-owned" | "transcript-elsewhere" | "transcript-missing"
    title: str
    lines: tuple[str, ...]
    actions: tuple[tuple[str, str], ...]  # (value, label); Cancel/Esc always exists
    # The decoded directory a "Relink to …" action points the record at.
    relink: str | None = None


def _resume_transcript_status(
    runtime: runtime_mod.Runtime, record: dict[str, Any]
) -> tuple[str, str, bool]:
    """Metadata-only: is the runtime transcript where the record points?

    Returns (status, detail, decoded): ("present", path, True) |
    ("elsewhere", project-path-or-slugs, decoded?) | ("missing", path, True).
    Never opens a transcript; filename checks only.
    """

    runtime_id = sessions.runtime_session_id(record)
    home = Path(runtime.environ.get("HOME") or Path.home())
    expected = (
        paths.native_projects(home, runtime.environ)
        / session_facts._native_project_slug(record["cwd"])
        / f"{runtime_id}.jsonl"
    )
    if expected.is_file():
        return ("present", str(expected), True)
    found = session_facts._slugs_for_session(runtime, runtime_id)
    decoded: set[str] = set()
    for slug in found:
        # Best-effort decode back to real project paths. Slugs are NOT
        # injective ("a-b/c" and "a/b-c" collide), so only a single
        # surviving candidate counts as exact — anything else falls back
        # to slug-level guidance when decoding is ambiguous.
        for candidate in session_facts._decode_project_slug_candidates(slug):
            decoded.add(str(candidate))
    if len(decoded) == 1:
        return ("elsewhere", decoded.pop(), True)
    if found:
        return ("elsewhere", ", ".join(sorted(found)), False)
    if runtime.environ.get("CLAUDE_CONFIG_DIR"):
        default = _default_root_transcript(runtime, record)
        if default is not None:
            return ("default-root", str(default), True)
        return ("unknown", str(expected), False)
    return ("missing", str(expected), True)


def _default_root_transcript(runtime: runtime_mod.Runtime, record: dict[str, Any]) -> Path | None:
    """The transcript under ``~/.claude/projects``, the root a managed launch
    writes to (it unsets ``CLAUDE_CONFIG_DIR``), when this shell exports
    ``CLAUDE_CONFIG_DIR`` and the lookup therefore looked elsewhere; None
    when it is not there. A file-existence check only, never a read."""

    home = Path(runtime.environ.get("HOME") or Path.home())
    path = (
        paths.native_projects(home, {})
        / session_facts._native_project_slug(record["cwd"])
        / f"{sessions.runtime_session_id(record)}.jsonl"
    )
    return path if path.is_file() else None


def config_dir_retry(stable_id: str) -> str:
    """The resume command with ``CLAUDE_CONFIG_DIR`` unset for that one
    invocation (the shell keeps its value)."""

    return f"env -u CLAUDE_CONFIG_DIR claude-multi -r {shlex.quote(stable_id)}"


def _evaluate_resume_gate(
    runtime: runtime_mod.Runtime,
    record: dict[str, Any],
    *,
    live_prefixes: frozenset[str] | None = None,
) -> ResumeGate:
    """Pure resume-gate evaluation; re-scans liveness unless injected."""

    stable_id = sessions.managed_id(record)
    identity_state = record.get("identity_state", sessions.IDENTITY_UNVERIFIED)
    if identity_state == sessions.IDENTITY_REPAIR_NEEDED and (
        "observed_cwd" in record or "observed_model" not in record
    ):
        return ResumeGate(
            kind="repair-needed",
            title="Session needs identity repair",
            lines=(sessions.relink_message(record),),
            actions=(("repair-resume", "Repair & resume"),),
        )
    # A v4 lead with no live line needs a choice before anything
    # else (pure: catalog + record); the card wires the "Choose & resume" modal.
    if record.get("version") == sessions.RECORD_VERSION:
        lcat = runtime.lineup_catalog()
        if sessions.lead_needs_choice(record, lcat):
            return ResumeGate(
                kind="needs-choice",
                title="Session needs a lead choice",
                lines=(
                    cli_text.NEEDS_CHOICE_REFUSAL.format(
                        mid=stable_id,
                        key=record["applied"]["lead"]["key"],
                        notice=sessions.lead_choice_notice(record, lcat),
                        rid=sessions.runtime_session_id(record),
                    ),
                ),
                actions=(("choose-resume", "Choose & resume"),),
            )
    # Transcript blockers outrank liveness: a missing transcript makes the
    # daemon question moot, and a force decision must never bypass them.
    status, detail, decoded = _resume_transcript_status(runtime, record)
    if status == "elsewhere":
        runtime_id = sessions.runtime_session_id(record)
        home = Path(runtime.environ.get("HOME") or Path.home())
        expected = (
            paths.native_projects(home, runtime.environ)
            / session_facts._native_project_slug(record["cwd"])
            / f"{runtime_id}.jsonl"
        )
        if decoded:
            remedy = (
                f"if the session was intentionally re-homed there, repair "
                f"the record with `claude-multi sessions relink-runtime "
                f"{stable_id} {runtime_id} --cwd {shlex.quote(detail)}`; "
                "otherwise move the transcript to the expected location "
                f"{expected} and resume from the recorded dir"
            )
        else:
            remedy = (
                "inspect those directories and relink with "
                "`claude-multi sessions relink-runtime "
                f"{stable_id} {runtime_id} --cwd <that project directory>` "
                "only if the session was intentionally re-homed; the "
                f"expected transcript location is {expected}"
            )
        text = (
            f"the transcript for runtime {runtime_id} was not found under "
            f"the recorded project dir ({record['cwd']}), but a file with "
            f"the same name exists under: {detail}. {remedy}."
        )
        # One decoded directory is offered as it is; an ambiguous or
        # undecodable one gets the directory chooser (validated, then the
        # gate again). Moving the transcript itself stays manual.
        return ResumeGate(
            kind="transcript-elsewhere",
            title="Transcript found in a different project",
            lines=(text,),
            actions=(("relink", cli_text.RELINK_TO.format(path=detail)),) if decoded
            else (("relink-choose", cli_text.RELINK_CHOOSE),),
            relink=detail if decoded else None,
        )
    if status == "default-root":
        # The lookup honoured this shell's CLAUDE_CONFIG_DIR, but managed
        # launches unset it, so their transcripts live under ~/.claude. The
        # root this resume would read is still not the one the lookup used:
        # refuse, and name the retry that looks where the session was written.
        configured = runtime.environ.get("CLAUDE_CONFIG_DIR", "")
        return ResumeGate(
            kind="transcript-unknown", title="Transcript is under ~/.claude, not CLAUDE_CONFIG_DIR",
            lines=(cli_text.CONFIG_DIR_RESUME_MISMATCH.format(
                configured=paths.display(configured, runtime.environ) if Path(configured).is_absolute() else configured,
                found=paths.display(detail, runtime.environ),
                retry=config_dir_retry(stable_id)),),
            actions=(),
        )
    if status == "unknown":
        return ResumeGate(
            kind="transcript-unknown", title="Historical native root unknown",
            lines=(f"No metadata at {detail}. The historical CLAUDE_CONFIG_DIR was not "
                   "recorded; select the original native root before resuming. "
                   "No transcript was read; do not infer deletion or forget the record.",),
            actions=(),
        )
    if status == "missing":
        text = (
            f"no transcript file exists for runtime "
            f"{sessions.runtime_session_id(record)} anywhere under "
            "~/.claude/projects (expected at "
            f"{detail}). Resume cannot work — Claude resumes from that "
            "file, and claude-multi never deletes transcripts. Restore it "
            "from a backup if one exists; otherwise forget the record with "
            f"`claude-multi sessions forget {stable_id}`."
        )
        return ResumeGate(
            kind="transcript-missing",
            title="Transcript not found",
            lines=(text,),
            actions=(),
        )
    if status == "present" and not Path(record["cwd"]).is_dir():
        # The classic rename: the transcript sits
        # exactly where the record points, but the recorded project
        # directory itself is gone — resume would fail entering the CWD at
        # launch. Name the two real exits up front: rename back, or move
        # the transcript into the new project dir and relink the record.
        runtime_id = sessions.runtime_session_id(record)
        text = (
            f"the recorded project dir ({record['cwd']}) is gone, though the "
            "transcript is still filed under it. If the directory was "
            "renamed or moved, either rename it back (resume then just "
            "works), or repair the record to the new location — `claude-multi "
            f"sessions relink-runtime {stable_id} {runtime_id} --cwd "
            "'<new project directory>'` — and resume again: the follow-up "
            "check then names the exact spot to move the transcript to."
        )
        return ResumeGate(
            kind="cwd-missing",
            title="Recorded project directory is gone",
            lines=(text,),
            actions=(("relink-choose", cli_text.RELINK_CHOOSE),),
        )
    prefixes = (
        live_prefixes if live_prefixes is not None else session_facts._live_background_prefixes()
    )
    # The resume conflict is only the RESUME TARGET being live (native
    # resume of the same UUID forks/refuses). A live historical alias is
    # an older branch — its hooks are stale-epoch-rejected, and resuming
    # the current runtime does not conflict with it (review: alias-aware
    # liveness must not gate). The ● row marker intentionally stays
    # lineage-broad for display.
    runtime_id = sessions.runtime_session_id(record)
    if any(runtime_id.startswith(prefix) for prefix in prefixes):
        text = (
            f"session {stable_id} is live in the background (●). Resuming a "
            "background-owned session natively either fails or forks it — "
            "the fork path is what caused the original incident. The "
            "supported route is to stop it first "
            f"(`claude-multi sessions stop {stable_id}`), then resume. The "
            "marker is a best-effort heuristic — if you are sure it is "
            "stale, Resume anyway."
        )
        return ResumeGate(
            kind="daemon-owned",
            title="Session is live in the background",
            lines=(text,),
            actions=(
                ("stop-resume", "Stop & resume"),
                ("force", "Resume anyway"),
            ),
        )
    return ResumeGate(kind="ok", title="", lines=(), actions=())


def _resume_gate_refusal(gate: ResumeGate) -> str:
    """Text-mode backstop text for a non-ok gate (perform enforcement)."""

    return gate.title + " — " + " ".join(gate.lines)
