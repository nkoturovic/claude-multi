"""One durable inhibition of gateway changes per state root.

A transaction owner — ``claude-multi gateway service install|uninstall``, the
installer, the updater, a machine move — records an inhibition before it
changes the gateway or its installation and removes it when it is done.
While one is recorded, every other path that would start, stop, restart,
hand over, install, update or reconfigure the gateway refuses with the same
*inhibited* outcome before it does anything: the token helper's automatic
start, the gateway start of a launch or resume, ``claude-multi gateway
start|restart|stop``, ``gateway service install|uninstall``, the setup
step, a published configuration (provider and model writes, the token
rotation, the alias prune), the ``claude-multi-proxy`` commands that render,
run or sign in, the activation boundary, and the installer and updater
(which call :func:`guard`). Reading stays available: health, status, doctor,
and the token for a gateway already proven ours and ready. Launchers also
leave the shared hook and token shims alone while an inhibition they do not
own is recorded.

The record is ``<state root>/gateway/inhibition.json``: a closed JSON
document (version 1), written atomically with mode 0600 while
``gateway-start.lock`` is held — the same lock every start, stop and service
hand-off takes before it looks at the backend, so a begin and a start never
interleave::

    {"version": 1,
     "owner": "installer",                  # who holds it
     "purpose": "update 1.0.0 to 1.0.1",    # one line, shown to the user
     "phase": "replace",                    # the owner's current step
     "created": "2026-10-02T12:00:00+00:00",
     "updated": "2026-10-02T12:00:05+00:00",  # begin, and every advance
     "expiry": {"policy": "owner-process", "pid": 4242, "seconds": 600},
     "remedy": "sh install.sh --recover",   # how its owner finishes or releases it
     "token": "<32 hex>"}                   # the owner's continuation, never shown

``expiry`` says when the record counts as stale (its owner was interrupted):
``owner-process`` once ``pid`` no longer runs or ``seconds`` passed since the
last update, ``deadline`` once ``seconds`` passed. A stale record still
refuses every change — it is never ignored, and doctor reports it with its
remedy — until its owner finishes or releases it.

An earlier service hand-off record (``service-handoff.json`` beside it:
operation, unit, PID, start time and token) inhibits exactly like a record of
the owner ``service`` (stale ten minutes after its start, or once its process
is gone); an unreadable one inhibits like an unreadable record, and so does
the presence of both files. Every writer of the record first migrates a
readable earlier record into this format (same token, under the start lock),
so the service verbs finish it like any interrupted hand-off.

Writers that do not dispatch — configuration publication, the endpoint's
first port, the shared hook and token shims, the ``claude-multi-proxy``
commands — check the inhibition inside :func:`fenced`, which holds the
root's writer fence (``gateway-inhibition.lock`` in the state root) shared
from the check through the write; :func:`begin` takes it exclusively while
it records.
So a writer that passed its check finishes before a begin returns, and every
writer that starts after it sees the record. The fence is held only around
a check and its write, never across a start, a stop or a wait for the start
lock: a launcher holding the start lock never waits for its own child. A
change of a home's gateway configuration fences every root that may own that
home's gateway (:func:`home_roots`: the writer's own and the managed root
``continuity.json`` records). That authority is read again once the fences
are held, and again under the api-key lock right before each write
(:func:`revalidate`; a root adoption moves it under the same lock): a root
adopted meanwhile that this writer did not fence refuses the write. A
``continuity.json`` that exists but cannot be read or validated leaves the
managed root unknown, so every such change refuses
(:class:`AuthorityUnreadable`) until it is repaired or removed.

Only the owner continues past its record, through the random token
:func:`begin` returned (a PID alone may be reused): in process by passing
it, and for the owner's own commands through
``CLAUDE_MULTI_INHIBITION_TOKEN`` (honoured by the explicit gateway verbs,
the ``claude-multi-proxy`` commands and the shim refresh; never by the
token helper's ``gateway ensure``, so a session started from the owner's
shell never acts for it). Recovery is explicit and idempotent:
:func:`recover` hands an interrupted owner its record (token and phase) back
so it can finish or :func:`end` it; nothing to recover is not an error.

The same operations as a command for shell owners (exit 0 ok, 1 refused or
inhibited, 2 usage; the token travels in the environment, never on a
command line it would be visible on)::

    python3 -m claude_multi.gateway_inhibition [--state-root DIR] status
    python3 -m claude_multi.gateway_inhibition begin --owner NAME --purpose TEXT --phase NAME
        --remedy COMMAND [--pid PID] [--stale-after SECONDS]   # prints the token
    python3 -m claude_multi.gateway_inhibition advance --phase NAME   # token from the environment
    python3 -m claude_multi.gateway_inhibition end                    # token from the environment
    python3 -m claude_multi.gateway_inhibition recover --owner NAME   # prints the token, if any
    python3 -m claude_multi.gateway_inhibition check                  # 0: this caller may change it
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import datetime
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from claude_multi import errors, paths, service, state, strict_json, termtext
from claude_multi.platform import posix_process

RECORD = "inhibition.json"
VERSION = 1
TOKEN_ENV = "CLAUDE_MULTI_INHIBITION_TOKEN"
# The writers' fence (a FileLock target: ``<state root>/gateway-inhibition.lock``;
# at the root, so a writer never creates the gateway directory for it).
FENCE = "gateway-inhibition"
# The owners this release knows; any name of the grammar is accepted, so an
# owner a later release adds is still reported by name.
OWNERS = ("service", "installer", "updater", "cutover")
# The service hand-off (``claude-multi gateway service install|uninstall``).
SERVICE_OWNER = "service"
SERVICE_REMEDY = "claude-multi gateway service install (or: claude-multi gateway service uninstall)"
# An earlier hand-off record (see the module docstring).
LEGACY_RECORD = "service-handoff.json"
LEGACY_STALE_AFTER = 600
_LEGACY_KEYS = frozenset({"version", "operation", "unit", "pid", "started_at", "token"})
OWNER_PROCESS, DEADLINE = "owner-process", "deadline"
POLICIES = (OWNER_PROCESS, DEADLINE)
# A process owner that stops making progress for this long is reported stale.
DEFAULT_STALE_AFTER = 600
MAX_STALE_AFTER = 7 * 24 * 3600
LOCK_WAIT = 10.0
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2

_KEYS = frozenset({"version", "owner", "purpose", "phase", "created", "updated", "expiry", "remedy", "token"})
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PHASE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_PURPOSE_MAX, _REMEDY_MAX = 200, 300


class InhibitionError(errors.ClaudeMultiError, RuntimeError):
    """An inhibition operation was refused (another owner, a wrong token, bad input)."""


class Inhibited(errors.ClaudeMultiError, RuntimeError):
    """Raised by :func:`guard`: a recorded inhibition refuses this change."""

    def __init__(self, record: "Inhibition", what: str, *, now: datetime.datetime | None = None) -> None:
        message, remedy = refusal(record, what, now=now)
        super().__init__(message, remedy=remedy)
        self.record = record


class AuthorityUnreadable(InhibitionError):
    """``continuity.json`` exists but cannot be read or validated: the root
    that owns the home's gateway is unknown, so no change of that home's
    configuration or credentials is made."""


class AuthorityChanged(InhibitionError):
    """The managed root moved to a root this writer did not fence."""


AUTHORITY_CHANGED = "the managed state root changed while this command waited; run it again"
# The roots whose writer fence this flow holds (:func:`fenced`), for :func:`revalidate`.
_FENCED: contextvars.ContextVar[tuple[Path, ...]] = contextvars.ContextVar("claude_multi_fenced_roots", default=())


def record_path(state_root: Path | str) -> Path:
    return service.gateway_workdir(Path(state_root)) / RECORD


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class Expiry:
    """When a record counts as stale (see the module docstring)."""

    policy: str
    seconds: int
    pid: int | None = None

    def document(self) -> dict[str, Any]:
        result: dict[str, Any] = {"policy": self.policy, "seconds": self.seconds}
        if self.policy == OWNER_PROCESS:
            result["pid"] = self.pid
        return result


def owner_process(pid: int | None = None, seconds: int = DEFAULT_STALE_AFTER) -> Expiry:
    """Stale once ``pid`` (default: this process) is gone or ``seconds`` passed without progress."""

    return Expiry(OWNER_PROCESS, seconds, os.getpid() if pid is None else pid)


def deadline(seconds: int) -> Expiry:
    """Stale once ``seconds`` passed since the owner's last step (a shell owner with no process to watch)."""

    return Expiry(DEADLINE, seconds)


@dataclass(frozen=True)
class Inhibition:
    """One recorded inhibition; ``problem`` set means the record could not be read."""

    owner: str | None = None
    purpose: str | None = None
    phase: str | None = None
    created: datetime.datetime | None = None
    updated: datetime.datetime | None = None
    expiry: Expiry | None = None
    remedy: str | None = None
    token: str | None = None
    problem: str | None = None

    @property
    def readable(self) -> bool:
        return self.problem is None

    def stale(self, *, now: datetime.datetime | None = None,
              exists: Callable[[int], bool | None] = posix_process.exists) -> bool:
        """Its owner was interrupted (an unreadable record is never "stale": it is unknown).

        A process record that names the asking process is stale too: its
        owner continues through its token and never asks, so the record was
        left behind (an owner that kept it on purpose for a later finish, or
        an earlier process whose PID this one reuses).
        """

        if not self.readable or self.expiry is None or self.updated is None:
            return False
        current = now or _now()
        if (current - self.updated).total_seconds() > self.expiry.seconds:
            return True
        if self.expiry.policy == OWNER_PROCESS and self.expiry.pid is not None:
            return self.expiry.pid == os.getpid() or exists(self.expiry.pid) is False
        return False

    def describe(self) -> str:
        """``<owner> (<purpose>; phase <phase>, since <created>)``, sanitized."""

        if not self.readable:
            return termtext.visible_text(f"an unreadable record ({self.problem})")
        since = self.created.isoformat(timespec="seconds") if self.created else "unknown"
        return termtext.visible_text(f"{self.owner} ({self.purpose}; phase {self.phase}, since {since})")


def _same_token(recorded: str | None, offered: str | None) -> bool:
    """Constant-time token comparison (any offered text, never an exception)."""

    if recorded is None or offered is None:
        return False
    return secrets.compare_digest(recorded.encode("utf-8", "replace"), offered.encode("utf-8", "replace"))


def _check_text(value: Any, what: str, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        raise InhibitionError(f"the inhibition {what} must be one printable line of at most {limit} characters")
    return value


def _check_name(value: Any, what: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InhibitionError(f"the inhibition {what} {value!r} is not a plain lower-case name")
    return value


def _check_expiry(expiry: Expiry) -> Expiry:
    if expiry.policy not in POLICIES:
        raise InhibitionError(f"unknown expiry policy {expiry.policy!r}")
    if type(expiry.seconds) is not int or not 0 < expiry.seconds <= MAX_STALE_AFTER:
        raise InhibitionError(f"the stale bound must be 1..{MAX_STALE_AFTER} seconds")
    if expiry.policy == OWNER_PROCESS and (type(expiry.pid) is not int or expiry.pid <= 0):
        raise InhibitionError("an owner-process inhibition needs the owner's PID")
    if expiry.policy == DEADLINE and expiry.pid is not None:
        raise InhibitionError("a deadline inhibition names no PID")
    return expiry


def _time(value: Any) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive time")
    return parsed


def _parse(document: Any) -> Inhibition:
    if not isinstance(document, dict) or set(document) != _KEYS or document["version"] != VERSION \
            or isinstance(document["version"], bool):
        raise ValueError("not a version 1 inhibition record")
    expiry_doc = document["expiry"]
    if not isinstance(expiry_doc, dict):
        raise ValueError("expiry is not an object")
    policy = expiry_doc.get("policy")
    expected = {"policy", "seconds", "pid"} if policy == OWNER_PROCESS else {"policy", "seconds"}
    if set(expiry_doc) != expected:
        raise ValueError("expiry has unexpected keys")
    if not isinstance(document["token"], str) or not _TOKEN.fullmatch(document["token"]):
        raise ValueError("invalid token")
    try:
        expiry = _check_expiry(Expiry(policy, expiry_doc.get("seconds"), expiry_doc.get("pid")))
        owner = _check_name(document["owner"], "owner", _NAME)
        phase = _check_name(document["phase"], "phase", _PHASE)
        purpose = _check_text(document["purpose"], "purpose", _PURPOSE_MAX)
        remedy = _check_text(document["remedy"], "remedy", _REMEDY_MAX)
    except InhibitionError as exc:
        raise ValueError(str(exc)) from exc
    return Inhibition(owner, purpose, phase, _time(document["created"]), _time(document["updated"]),
                      expiry, remedy, document["token"])


def _document(record: Inhibition) -> dict[str, Any]:
    assert record.expiry is not None and record.created is not None and record.updated is not None
    return {"version": VERSION, "owner": record.owner, "purpose": record.purpose, "phase": record.phase,
            "created": record.created.isoformat(), "updated": record.updated.isoformat(),
            "expiry": record.expiry.document(), "remedy": record.remedy, "token": record.token}


def legacy_path(state_root: Path | str) -> Path:
    return service.gateway_workdir(Path(state_root)) / LEGACY_RECORD


def _load(target: Path, parse: Callable[[Any], Inhibition], label: str) -> Inhibition | None:
    if not os.path.lexists(target):
        return None
    try:
        return parse(strict_json.loads(state.read_private(target)))
    except state.StateError as exc:
        if not os.path.lexists(target):
            return None
        return Inhibition(problem=f"{label}{exc.strerror or exc}")
    except (OSError, ValueError, TypeError, KeyError, RecursionError, errors.ClaudeMultiError) as exc:
        return Inhibition(problem=f"{label}{type(exc).__name__}: {exc}"[:200])


def _parse_legacy(document: Any) -> Inhibition:
    """An earlier hand-off record as the ``service`` owner's inhibition."""

    if not isinstance(document, dict) or set(document) != _LEGACY_KEYS or document["version"] != 1 \
            or isinstance(document["version"], bool) or document["operation"] not in ("install", "uninstall") \
            or type(document["pid"]) is not int or document["pid"] <= 0 \
            or not isinstance(document["token"], str) or not _TOKEN.fullmatch(document["token"]):
        raise ValueError("not a version 1 hand-off record")
    started = _time(document["started_at"])
    try:
        purpose = _check_text(f"gateway service {document['operation']} ({document['unit']})", "purpose",
                              _PURPOSE_MAX)
    except InhibitionError as exc:
        raise ValueError(str(exc)) from exc
    return Inhibition(SERVICE_OWNER, purpose, document["operation"], started, started,
                      Expiry(OWNER_PROCESS, LEGACY_STALE_AFTER, document["pid"]), SERVICE_REMEDY, document["token"])


def read(state_root: Path | str) -> Inhibition | None:
    """The recorded inhibition; None when there is none. Never raises: a
    record that cannot be read or validated comes back with ``problem`` set
    (it inhibits like any other: unknown is never "free"). An earlier
    hand-off record counts as a record; both files at once are unknown."""

    current = _load(record_path(state_root), _parse, "")
    legacy = _load(legacy_path(state_root), _parse_legacy, f"{LEGACY_RECORD}: ")
    if legacy is None:
        return current
    if current is None:
        return legacy
    return Inhibition(problem=f"both {RECORD} and the earlier {LEGACY_RECORD} are recorded")


def _migrate_legacy(state_root: Path | str) -> None:
    """Under the start lock: a readable earlier hand-off record (and no
    record of this format) becomes one, with the same token, phase and
    times; then the earlier file is removed. Anything else stays as it is
    (and keeps refusing through :func:`read`)."""

    if read(state_root) is None or os.path.lexists(record_path(state_root)):
        return
    legacy = _load(legacy_path(state_root), _parse_legacy, "")
    if legacy is None or not legacy.readable:
        return
    state.atomic_write(record_path(state_root), strict_json.canonical_bytes(_document(legacy)))
    state.remove_private(legacy_path(state_root))


def blocking(state_root: Path | str, *, token: str | None = None) -> Inhibition | None:
    """The inhibition that refuses a change by the holder of ``token`` (None:
    nothing is recorded, or ``token`` is the record's own)."""

    record = read(state_root)
    if record is None:
        return None
    if record.readable and _same_token(record.token, token):
        return None
    return record


def refusal(record: Inhibition, what: str, *, now: datetime.datetime | None = None) -> tuple[str, str]:
    """``(message, remedy)``: the one inhibited text every refusing path shows."""

    if not record.readable:
        return (f"the gateway is inhibited by {record.describe()}; {what}",
                "if no installer, updater, machine move or gateway service hand-off is running, remove "
                f"{RECORD} (and an earlier {LEGACY_RECORD}) from the gateway directory of the state root "
                "(claude-multi doctor names it)")
    state_text = " — its owner did not finish (stale)" if record.stale(now=now) else ""
    return (f"the gateway is inhibited by {record.describe()}{state_text}; {what}",
            f"wait until it has finished; if it was interrupted, its owner finishes or releases it: "
            f"{termtext.visible_text(str(record.remedy))}")


def guard(state_root: Path | str, *, token: str | None = None, what: str = "nothing was changed",
          now: datetime.datetime | None = None) -> None:
    """Raise :class:`Inhibited` unless the holder of ``token`` may change the
    gateway now (the installer's and the updater's check before they mutate)."""

    record = blocking(state_root, token=token)
    if record is not None:
        raise Inhibited(record, what, now=now)


class _StartLock:
    """``gateway-start.lock`` for the record's writers (bounded wait)."""

    def __init__(self, state_root: Path | str, *, held: bool, wait: float,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.workdir = service.ensure_gateway_workdir(Path(state_root))
        self.held = held
        self.wait = wait
        self.clock = clock
        self.sleep = sleep
        self.lock: state.FileLock | None = None

    def __enter__(self) -> "_StartLock":
        if self.held:
            return self
        lock = state.FileLock(self.workdir / service.START_LOCK.removesuffix(".lock"))
        deadline_at = self.clock() + self.wait
        while not lock.acquire(blocking=False):
            if self.clock() >= deadline_at:
                raise InhibitionError("a start or stop of the gateway holds its start lock; nothing was recorded",
                                      remedy="retry in a moment")
            self.sleep(0.05)
        self.lock = lock
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.lock is not None:
            self.lock.release()


def _locked(state_root: Path | str, held: bool, wait: float | None = None) -> _StartLock:
    return _StartLock(state_root, held=held, wait=LOCK_WAIT if wait is None else wait,
                      clock=time.monotonic, sleep=time.sleep)


def _fence_lock(state_root: Path | str, *, shared: bool, create: bool) -> state.FileLock | None:
    """The root's writer fence; None for a root that does not exist when
    ``create`` is False (no record can be there)."""

    root = Path(state_root)
    if create:
        state.ensure_private_dir(root)
    elif not root.is_dir():
        return None
    return state.FileLock(root / FENCE, shared=shared)


def _take(lock: state.FileLock, wait: float) -> bool:
    deadline_at = time.monotonic() + wait
    while not lock.acquire(blocking=False):
        if time.monotonic() >= deadline_at:
            return False
        time.sleep(0.02)
    return True


@contextlib.contextmanager
def _recording(state_root: Path | str, wait: float) -> Iterator[None]:
    """:func:`begin`'s exclusive hold of the writer fence: every writer
    inside :func:`fenced` finishes first, and none starts until the record
    is written (bounded wait)."""

    lock = _fence_lock(state_root, shared=False, create=True)
    assert lock is not None
    if not _take(lock, wait):
        raise InhibitionError("a change of the gateway's configuration or shims is in progress; nothing was recorded",
                              remedy="retry in a moment")
    try:
        yield
    finally:
        lock.release()


def managed_root(home: Path | str, *, what: str = "nothing was changed") -> Path | None:
    """The managed state root ``home``'s ``continuity.json`` records (None:
    no file, or no root recorded yet). Raises :class:`AuthorityUnreadable`
    when the file exists but cannot be read or validated: unknown is never
    "none" for a change of that home's configuration or credentials."""

    from claude_multi import continuity

    try:
        persisted = continuity.read(Path(home))
    except (continuity.ContinuityError, OSError, ValueError, errors.ClaudeMultiError) as exc:
        where = paths.display(continuity.path(Path(home)), {"HOME": str(home)})
        raise AuthorityUnreadable(
            f"gateway continuity set unreadable ({termtext.visible_text(str(exc))}): the state root that "
            f"manages this home's gateway is unknown; {what}",
            remedy=f"claude-multi doctor shows it; repair or remove {where}, then run it again") from None
    managed = persisted.get("state_root") if isinstance(persisted, dict) else None
    return Path(managed) if isinstance(managed, str) and os.path.isabs(managed) else None


def home_roots(home: Path | str, state_root: Path | str, *, what: str = "nothing was changed") -> tuple[Path, ...]:
    """The roots whose inhibition fences a change of ``home``'s gateway
    configuration (config, keys, endpoint): ``state_root`` and, when it
    names another one, the managed root ``continuity.json`` records (that
    root's gateway serves the same configuration). An absent authority adds
    nothing; an unreadable one raises :class:`AuthorityUnreadable`."""

    roots = [Path(state_root)]
    managed = managed_root(home, what=what)
    if managed is not None and managed != roots[0]:
        roots.append(managed)
    return tuple(roots)


def revalidate(home: Path | str, *, what: str = "nothing was changed") -> None:
    """At a write of ``home``'s configuration or credentials, under the
    api-key lock (the lock a root adoption moves the authority under): the
    managed root ``continuity.json`` records now must be one whose fence this
    flow holds (:func:`fenced`). Raises :class:`AuthorityChanged` when an
    adoption moved it meanwhile (a begin of the new root does not wait for
    this writer), :class:`AuthorityUnreadable` when it cannot be read. A
    flow that holds no fence (the unit's directives, a library caller) is
    not checked here."""

    held = _FENCED.get()
    if not held:
        return
    managed = managed_root(home, what=what)
    if managed is not None and managed not in held:
        raise AuthorityChanged(f"{AUTHORITY_CHANGED} ({what})",
                               remedy="run the command again: it then waits for the state root that manages "
                                      "this home now")


@contextlib.contextmanager
def fenced(state_roots: Iterable[Path | str], *, token: str | None = None, what: str = "nothing was changed",
           wait: float | None = None, home: Path | str | None = None) -> Iterator[None]:
    """Hold each root's writer fence (shared) with its inhibition checked
    under it, for a non-dispatching check-through-write (see the module
    docstring). The first root is the writer's own (its fence is created
    when missing); another that does not exist is only checked. With
    ``home`` (a change of that home's configuration, the roots from
    :func:`home_roots`), the managed root is read again once every fence is
    held, as :func:`revalidate` does.

    Raises :class:`Inhibited` for an inhibition the holder of ``token`` does
    not own, :class:`InhibitionError` when the fence cannot be taken (a
    begin is recording it, or the state root directory is unusable),
    :class:`AuthorityChanged` or :class:`AuthorityUnreadable` from the
    authority's second reading.
    """

    roots = tuple(dict.fromkeys(Path(item) for item in state_roots))
    held: list[state.FileLock] = []
    marker = _FENCED.set(_FENCED.get() + roots)
    try:
        for index, root in enumerate(roots):
            try:
                lock = _fence_lock(root, shared=True, create=index == 0)
                if lock is not None:
                    if not _take(lock, LOCK_WAIT if wait is None else wait):
                        raise InhibitionError(f"a transaction owner is recording the gateway's inhibition; {what}",
                                              remedy="retry in a moment")
                    held.append(lock)
            except state.StateError as exc:  # an unsafe state root directory: its own one-line reason
                raise InhibitionError(str(exc), remedy=exc.remedy) from exc
            except OSError as exc:
                raise InhibitionError(f"the gateway's inhibition cannot be checked ({exc.strerror or exc}); {what}",
                                      remedy="check: claude-multi doctor") from exc
            guard(root, token=token, what=what)
        if home is not None:
            revalidate(home, what=what)
        yield
    finally:
        _FENCED.reset(marker)
        for lock in reversed(held):
            lock.release()


def begin(state_root: Path | str, *, owner: str, purpose: str, phase: str, remedy: str,
          expiry: Expiry | None = None, now: datetime.datetime | None = None, held_lock: bool = False,
          take_over_stale: bool = False, lock_wait: float | None = None) -> str:
    """Record an inhibition for ``owner``; returns its token.

    Refused (:class:`InhibitionError`) while another inhibition is recorded,
    except that ``take_over_stale`` lets an owner replace its own kind's
    stale record (finishing an interrupted run of itself). ``held_lock``: the
    caller already holds ``gateway-start.lock``. The writer fence is taken
    exclusively while the record is written (both waits bounded by
    ``lock_wait``).
    """

    _check_name(owner, "owner", _NAME)
    _check_name(phase, "phase", _PHASE)
    _check_text(purpose, "purpose", _PURPOSE_MAX)
    _check_text(remedy, "remedy", _REMEDY_MAX)
    chosen = _check_expiry(expiry or owner_process())
    with _locked(state_root, held_lock, lock_wait), _recording(state_root, LOCK_WAIT if lock_wait is None
                                                               else lock_wait):
        _migrate_legacy(state_root)
        current = read(state_root)
        moment = now or _now()
        if current is not None and not (take_over_stale and current.readable and current.owner == owner
                                        and current.stale(now=moment)):
            message, hint = refusal(current, "nothing was recorded", now=moment)
            raise InhibitionError(message, remedy=hint)
        token = secrets.token_hex(16)
        record = Inhibition(owner, purpose, phase, moment, moment, chosen, remedy, token)
        state.atomic_write(record_path(state_root), strict_json.canonical_bytes(_document(record)))
        return token


def _owned(state_root: Path | str, token: str) -> Inhibition:
    current = read(state_root)
    if current is None:
        raise InhibitionError("no inhibition is recorded")
    if not current.readable or not _same_token(current.token, token):
        raise InhibitionError(f"the recorded inhibition belongs to another owner: {current.describe()}")
    return current


def advance(state_root: Path | str, token: str, phase: str, *, now: datetime.datetime | None = None,
            held_lock: bool = False) -> Inhibition:
    """Record the owner's next phase (and its progress, for the stale bound)."""

    _check_name(phase, "phase", _PHASE)
    with _locked(state_root, held_lock):
        _migrate_legacy(state_root)
        current = _owned(state_root, token)
        updated = Inhibition(current.owner, current.purpose, phase, current.created, now or _now(),
                             current.expiry, current.remedy, current.token)
        state.atomic_write(record_path(state_root), strict_json.canonical_bytes(_document(updated)))
        return updated


def end(state_root: Path | str, token: str, *, held_lock: bool = False) -> bool:
    """Remove the owner's record; False when none is recorded (ending twice is fine).
    Another owner's record is never removed."""

    with _locked(state_root, held_lock):
        _migrate_legacy(state_root)
        if read(state_root) is None:
            return False
        _owned(state_root, token)
        state.remove_private(record_path(state_root))
        return True


def recover(state_root: Path | str, owner: str, *, now: datetime.datetime | None = None,
            exists: Callable[[int], bool | None] = posix_process.exists) -> Inhibition | None:
    """Hand ``owner`` its interrupted record back (token and phase), so it can
    finish, :func:`advance` or :func:`end` it. None when nothing is recorded.
    Refused for another owner's record, an unreadable one, or one whose owner
    still runs."""

    _check_name(owner, "owner", _NAME)
    with _locked(state_root, False):
        _migrate_legacy(state_root)
        current = read(state_root)
        if current is None:
            return None
        if not current.readable or current.owner != owner:
            raise InhibitionError(f"the recorded inhibition is not {owner}'s: {current.describe()}")
        if not current.stale(now=now, exists=exists):
            raise InhibitionError(f"the inhibition by {current.describe()} is still in progress",
                                  remedy="wait until it has finished")
        return current


# ------------------------------------------------------------ the command

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m claude_multi.gateway_inhibition",
                                     description="Record, inspect and release the gateway's inhibition.")
    parser.add_argument("--state-root", help="the state root (default: $XDG_STATE_HOME/claude-multi)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show the recorded inhibition (exit 1 when one is recorded)")
    start = commands.add_parser("begin", help="record an inhibition and print its token")
    start.add_argument("--owner", required=True)
    start.add_argument("--purpose", required=True)
    start.add_argument("--phase", required=True)
    start.add_argument("--remedy", required=True, help="the command that finishes or releases it")
    start.add_argument("--pid", type=int, help="the owner process (stale once it is gone)")
    start.add_argument("--stale-after", type=int, help="seconds without progress before it counts as stale")
    step = commands.add_parser("advance", help=f"record the next phase (token from {TOKEN_ENV})")
    step.add_argument("--phase", required=True)
    commands.add_parser("end", help=f"remove the record (token from {TOKEN_ENV})")
    back = commands.add_parser("recover", help="print the token of this owner's interrupted inhibition")
    back.add_argument("--owner", required=True)
    commands.add_parser("check", help=f"exit 0 when the holder of {TOKEN_ENV} may change the gateway")
    return parser


def _status_lines(record: Inhibition | None, root: Path, environ: Mapping[str, str]) -> list[str]:
    if record is None:
        return [f"gateway inhibition: none ({paths.display(record_path(root), environ)})"]
    message, remedy = refusal(record, "gateway changes are refused")
    return [message, f"  fix: {remedy}"]


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         stdout: Any = None, stderr: Any = None) -> int:
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    if args.state_root is not None and not os.path.isabs(args.state_root):
        err.write("gateway inhibition: --state-root must be absolute\n")
        return EXIT_USAGE
    root = Path(args.state_root) if args.state_root else paths.state_root(dict(env))
    token = env.get(TOKEN_ENV) or None
    try:
        if args.command == "status":
            record = read(root)
            for line in _status_lines(record, root, env):
                out.write(line + "\n")
            return EXIT_OK if record is None else EXIT_REFUSED
        if args.command == "check":
            guard(root, token=token, what="this change is refused")
            return EXIT_OK
        if args.command == "begin":
            if args.pid is not None:
                expiry = owner_process(args.pid, args.stale_after or DEFAULT_STALE_AFTER)
            elif args.stale_after is not None:
                expiry = deadline(args.stale_after)
            else:
                raise InhibitionError("give the owner's --pid, or --stale-after SECONDS for an owner "
                                      "with no process to watch")
            out.write(begin(root, owner=args.owner, purpose=args.purpose, phase=args.phase,
                            remedy=args.remedy, expiry=expiry) + "\n")
            return EXIT_OK
        if args.command == "recover":
            record = recover(root, args.owner)
            if record is not None:
                out.write(f"{record.token}\n")
                err.write(f"gateway inhibition: recovered {record.describe()}\n")
            return EXIT_OK
        if token is None:
            raise InhibitionError(f"{args.command} needs the owner's token in {TOKEN_ENV}")
        if args.command == "advance":
            advance(root, token, args.phase)
            return EXIT_OK
        if args.command == "end":
            if not end(root, token):
                err.write("gateway inhibition: none recorded; nothing to end\n")
            return EXIT_OK
    except (InhibitionError, Inhibited, state.StateError) as exc:
        err.write(f"gateway inhibition: {termtext.visible_message(exc)}\n")
        remedy = getattr(exc, "remedy", None)
        if remedy:
            err.write(f"  fix: {termtext.visible_text(remedy)}\n")
        return EXIT_REFUSED
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
