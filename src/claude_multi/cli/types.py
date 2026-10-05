"""The launch types and the typed command errors the entry point maps to exit codes."""

from __future__ import annotations

from claude_multi import errors as cli_errors

from typing import Any
import claude_multi.cli.text as cli_text
from claude_multi import compiler
from dataclasses import dataclass
from claude_multi import errors
from claude_multi import profile as profile_mod
from claude_multi import sessions


class UsageError(cli_errors.CLIError):
    """An invalid command line argparse cannot express (an option that does
    not belong here, a value this context cannot use): exit 2. Every other
    refusal of an ordinary command is exit 1."""

    exit_status = 2


class LaunchPlanError(cli_errors.CLIError):
    """The lineup cannot launch (evaluation errors or missing credentials)."""

    def __init__(self, problems: Any):
        self.problems = tuple(str(problem) for problem in problems)
        super().__init__(
            "profile is blocked:\n" + "\n".join(f"- {problem}" for problem in self.problems)
        )


class NeedsChoiceError(cli_errors.CLIError):
    """The session's lead has no live line; a person must choose."""

    def __init__(self, record: dict[str, Any], key: str, notice: str | None):
        self.record = record
        self.key = key
        self.notice = notice or f"lead {key} has no live line"
        mid = sessions.managed_id(record)
        super().__init__(
            cli_text.NEEDS_CHOICE_REFUSAL.format(
                mid=mid,
                key=key,
                notice=self.notice,
                rid=sessions.runtime_session_id(record),
            )
        )


@dataclass(frozen=True)
class LaunchTarget:
    """What a launch applies."""

    kind: str  # "profile" | "profile-file" | "ad-hoc" | "record" | "relaunch"
    document: dict[str, Any] | None  # profile v2 doc; None for "record" (built from the record)
    profile: str | None  # record `profile` after commit
    follow: bool  # record `follow` after commit
    source: str  # "Profile balanced" | "Direct sol" | "Session <id8> lineup" | "Relaunch <id8>" …


@dataclass(frozen=True)
class PreparedLaunch:
    result: compiler.CompileResult
    record: dict[str, Any]  # the v4 record to commit
    lineup: profile_mod.ResolvedLineup
    target: LaunchTarget
    diff: tuple[str, ...] = ()  # one-time diff lines (§6.3)
    notices: tuple[str, ...] = ()  # migration, retired N1/N2, settings drift, follow reset
    model_relaunch: bool = False
    expected_launch_epoch: int | None = None
    expected_mutation_token: str | None = None
    expected_applied_hash: str | None = None
    expected_lineup_generation: int | None = None
    # The on-disk lineup.gen prepare planned against (0 when absent);
    # None for a fresh launch.
    expected_disk_generation: int | None = None
    secret_problems: tuple[str, ...] = ()  # blocking for non-interactive launches
    # The workflow default's label and window under the session policy
    # (None: off), and the window ceiling the plan was compiled with
    # (``choices.WindowCeiling``); display only.
    workflow_window: tuple[str, profile_mod.RoleWindow] | None = None
    window_ceiling: Any = None
