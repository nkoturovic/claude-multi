"""Dependency-free observation results shared by platform facades and backends.

A backend never imports its facade; both sides use these types. Liveness is
explicit: ``running``, ``stopped`` or ``unknown``. A missing capability (no
``/proc``, no service manager, an unreadable table) is ``unknown`` and never
``stopped``; only a stopped process *and* an absent listener prove a stopped
boundary.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

RUNNING, STOPPED, UNKNOWN = "running", "stopped", "unknown"
LIVENESS = (RUNNING, STOPPED, UNKNOWN)


def liveness(alive: object) -> str:
    """``True`` → running, ``False`` → stopped; anything else is unknown."""
    if alive is True:
        return RUNNING
    if alive is False:
        return STOPPED
    return UNKNOWN


@dataclass(frozen=True)
class OwnerVerdict:
    kind: str  # ours | foreign | unknown | none
    detail: str
    uid: int | None = None
    pid: int | None = None


@dataclass(frozen=True)
class ExecReloadResult:
    started_at: datetime.datetime
    failed: bool


@dataclass(frozen=True)
class BackgroundLiveness:
    known: bool
    prefixes: frozenset[str]
    reason: str | None = None


@dataclass(frozen=True)
class ProcessObservation:
    """One process, with the instance identity that makes PID reuse visible."""

    state: str  # running | stopped | unknown
    pid: int | None
    instance: str | None  # "<pid namespace>/<pid>@<started_at>" of the recorded start
    detail: str


@dataclass(frozen=True)
class GatewayObservation:
    """Backend-supplied gateway process and listener facts, without credentials."""

    process: ProcessObservation
    listener: OwnerVerdict

    @property
    def stopped_proof(self) -> bool:
        """Stopped only when the process is absent AND no listener holds the port."""
        return self.process.state == STOPPED and self.listener.kind == "none"


@dataclass(frozen=True)
class ProcessScan:
    """A ``/proc`` argv scan: ``known`` is False when the table was unreadable."""

    known: bool
    ids: frozenset[str]
    reason: str | None = None


# Source records are transient; domain collectors retain only validated metadata.
# The record cap sits above what the byte budget holds for a day of the gateway's
# journal, so bytes (not a record count) bound the 24 h window; a reader keeps the
# newest records when a cap cuts it. The message cap is the credential-save
# collector's, not the source's: other recognizers still see long records.
LOG_MAX_RECORDS = 65536
LOG_MAX_MESSAGE = 8192


@dataclass(frozen=True)
class LogRecord:
    message: str
    timestamp: datetime.datetime | None = None
    gateway_instance: str | None = None
    source_cursor: str | None = None


@dataclass(frozen=True)
class LogWindow:
    records: tuple[LogRecord, ...] = ()
    coverage: str = "unavailable"  # bounded | truncated | incomplete | unavailable
    timed_out: bool = False
