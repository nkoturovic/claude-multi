"""Shared types of the setup layer: plans, confirmations, results and undo."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from claude_multi import errors, strict_json

READ_ONLY_SETUP = "read-only: this claude-multi cannot change your setup"
CONFIRMATION_MISMATCH = "confirmation does not match the plan"
STALE = "{what} changed while you were deciding — nothing written; try again"
NOTHING_CHANGED = "Nothing changed."

CONSENTS = (None, "confirm", "approve-route", "network", "destructive", "typed")
RELOAD_OUTCOMES = ("reloaded", "restart_required", "token_mismatch", "down", "not-needed")


@dataclass(frozen=True)
class Fix:
    """One fix in both forms: the full-screen key path and the command."""

    tui: str | None
    cli: str | None
    argv: tuple[str, ...] = ()


class SetupError(errors.ClaudeMultiError, RuntimeError):
    """A setup operation did not happen; ``remedy`` is the command form of
    the fix and ``fix`` the structured one."""

    fix: Fix | None = None
    line: str | None = None

    def __init__(self, message: str, *, remedy: str | None = None, fix: Fix | None = None,
                 line: str | None = None):
        super().__init__(message, remedy=remedy if remedy is not None else (fix.cli if fix else None))
        if fix is not None:
            self.fix = fix
        if line is not None:
            self.line = line

    @property
    def line_text(self) -> str:
        """The command-line form of the message (it names commands, not keys)."""

        return self.line if self.line is not None else str(self)


class Refused(SetupError):
    """A rule refuses the operation; nothing was written."""


class Stale(SetupError):
    """The state moved after the plan was made; nothing was written."""

    def __init__(self, what: str, *, fix: Fix | None = None):
        self.what = what
        super().__init__(STALE.format(what=what), fix=fix)


class Declined(SetupError):
    """The person said no; nothing was written."""

    def __init__(self, message: str = NOTHING_CHANGED):
        super().__init__(message)


def digest_of(*facts: Any) -> str:
    """``sha256:<hex>`` over the canonical form of every fact a plan relied on."""

    return "sha256:" + strict_json.sha256_hex(strict_json.canonical_bytes(list(facts)))


@dataclass(frozen=True)
class Plan:
    """What an apply will do, shown before it happens (names only).

    ``consent`` is None or one of ``confirm``, ``approve-route``,
    ``network``, ``destructive``, ``typed``; ``guarded`` applies need the
    human guard (a real terminal outside Claude Code sessions); ``digest``
    covers the operation, its subject and every state fact relied on;
    ``writes`` names what the apply may write.
    """

    op: str
    subject: str
    lines: tuple[str, ...]
    consent: str | None
    guarded: bool
    digest: str
    writes: tuple[str, ...]


@dataclass(frozen=True)
class Confirmation:
    """A person's agreement to one plan, made by the surface that showed it."""

    digest: str
    consent: str | None
    at: str

    @classmethod
    def given(cls, plan: Plan) -> "Confirmation":
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cls(plan.digest, plan.consent, now)


class Secret:
    """A secret value that never shows itself: its text is only revealed to
    the store write."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = str(value)

    def reveal(self) -> str:
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret({len(self._value)} chars)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__

    def __reduce__(self) -> Any:
        raise TypeError("a Secret is never pickled")


@dataclass(frozen=True)
class Applied:
    """The result of an apply: what happened to the gateway and the lines to show."""

    op: str
    subject: str
    reload: str
    lines: tuple[str, ...]


class UndoStack:
    """Exact undo steps, run in reverse order; every step is attempted and the
    first failure is raised after all of them ran."""

    def __init__(self) -> None:
        self._steps: list[Callable[[], None]] = []

    def push(self, undo: Callable[[], None]) -> None:
        self._steps.append(undo)

    def __len__(self) -> int:
        return len(self._steps)

    def run(self) -> None:
        first: BaseException | None = None
        while self._steps:
            step = self._steps.pop()
            try:
                step()
            except BaseException as exc:  # every undo is attempted
                if first is None:
                    first = exc
        if first is not None:
            raise first


def check_apply(runtime: Any, plan: Plan, confirmation: Confirmation, *, verb: str) -> None:
    """The apply contract: the confirmation is for this plan, a guarded plan
    passes the human guard, and the runtime may change state."""

    if confirmation.digest != plan.digest or confirmation.consent != plan.consent:
        raise Refused(CONFIRMATION_MISMATCH)
    if plan.guarded:
        from claude_multi.cli import consent  # the one human guard

        consent.require_human(verb, runtime.gateway_environ())
    if not getattr(runtime, "allow_state_writes", False):
        raise Refused(READ_ONLY_SETUP)


def lines_of(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(line for line in items if line)
