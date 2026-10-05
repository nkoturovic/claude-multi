"""Account sign-in and sign-out: the typed personal-use acknowledgement, the
sign-in that runs the gateway program in this terminal, the account names
and the sign-out that moves the sign-in records into a kept backup.

The acknowledgement records which text was shown (its id and SHA-256), when
and the word typed; a changed text needs a fresh acknowledgement. The
gateway's own login commands refuse without a current one, so every sign-in
path shows it. Records are judged by their file names, sizes and times only:
their contents (account tokens) are never opened.

Every pool-specific fact (its provider, display, sign-in flow, host, login
command, offering policies, acknowledgement, record prefix) is the pool's
data in :mod:`claude_multi.account_pools`; nothing here names a pool.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_multi import account_pools, choices, errors, paths
from claude_multi.setup import model, texts

# Provider -> pool, pool -> provider and pool -> the gateway program's login
# command, from the pools' data.
ACCOUNT_POOLS = {pool.provider: name for name, pool in account_pools.pools().items()}
POOL_PROVIDERS = {pool: provider for provider, pool in ACCOUNT_POOLS.items()}
LOGIN_COMMANDS = {name: pool.sign_in.command for name, pool in account_pools.pools().items()}
METHODS = account_pools.METHODS
PORT_IN_USE_EXIT = 13  # the gateway program's exit status when its sign-in port is taken
SIGNIN_POLICIES = account_pools.SIGNIN_POLICIES
BACKUP_INFIX = ".signed-out."
VERIFY_SECONDS = 5.0
# Pool -> (text id, text) of its personal-use acknowledgement.
_ACK = {name: (pool.acknowledgement.id, pool.acknowledgement.text) for name, pool in account_pools.pools().items()}


class SignInError(model.SetupError):
    """The sign-in could not run (the gateway program is missing, say)."""


class SignOutIncomplete(model.SetupError):
    """A sign-out stopped part-way and could not put every record back; the
    message names where the records are kept."""


# ------------------------------------------------------------------ build policy


def signin_policy() -> str:
    """This build's account sign-in policy, from the packaged release
    identity (``version.json`` ``signin_policy``; absent: ``public``).

    ``public`` offers every account sign-in behind its typed
    acknowledgement; ``public-strict`` offers only the pools whose data
    lists it (the Claude account sign-in is not one of them). Every surface
    (commands, the full-screen picker, the gateway program's login
    commands) reads this one value."""

    from claude_multi import resources_root

    try:
        document = json.loads((resources_root() / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "public"
    value = document.get("signin_policy", "public") if isinstance(document, dict) else "public"
    return value if value in SIGNIN_POLICIES else "public-strict"


def pool_offered(pool: str) -> bool:
    """Whether this build offers the account sign-in of ``pool`` (its data
    lists this build's sign-in policy)."""

    entry = account_pools.pool(pool)
    return entry is not None and entry.offered(signin_policy())


def methods(pool: str) -> tuple[str, ...]:
    """The ways the sign-in of ``pool`` can open (its flow's methods)."""

    entry = account_pools.pool(pool)
    if entry is None:
        raise model.Refused(f"unknown account pool {pool!r}")
    return entry.sign_in.methods


def client_account(pool: str) -> bool:
    """Whether the managed client signs in with ``pool``'s kind of account
    itself (a sign-in here is then separate from that login); False for an
    unknown pool."""

    entry = account_pools.pool(pool)
    return entry is not None and entry.client_account


# ------------------------------------------------------------------ acknowledgements


@dataclass(frozen=True)
class Ack:
    pool: str
    text_id: str
    text_sha256: str
    acknowledged_at: str


def ack_text(pool: str) -> tuple[str, str]:
    """``(text_id, text)`` of the pool's acknowledgement."""

    if pool not in _ACK:
        raise model.Refused(f"unknown account pool {pool!r}")
    return _ACK[pool]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_ack(environ: Mapping[str, str], pool: str) -> Ack | None:
    """The stored acknowledgement when it is for today's text (else None;
    an unreadable choices file is no acknowledgement)."""

    text_id, text = ack_text(pool)
    try:
        record = choices.read(environ).get("acknowledgements").get(pool)
    except choices.ChoicesError:
        return None
    if not isinstance(record, Mapping):
        return None
    if record.get("text_id") != text_id or record.get("text_sha256") != text_sha256(text):
        return None
    return Ack(pool, text_id, record["text_sha256"], record["acknowledged_at"])


def record_ack(runtime: Any, pool: str, typed: str, *, now: datetime.datetime | None = None) -> Ack:
    """Store the acknowledgement after the person typed the word."""

    if (typed or "").strip().lower() != texts.ACK_WORD:
        raise model.Refused(texts.ACK_WRONG)
    if not pool_offered(pool):
        raise model.Refused(texts.SIGNIN_STRICT)
    if not getattr(runtime, "allow_state_writes", False):
        raise model.Refused(model.READ_ONLY_SETUP)
    text_id, text = ack_text(pool)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {"text_id": text_id, "text_sha256": text_sha256(text), "acknowledged_at": stamp,
              "typed": texts.ACK_WORD}
    choices.set_acknowledgement(runtime.environ, pool, record)
    return Ack(pool, text_id, record["text_sha256"], stamp)


def login_refusal(command: str, environ: Mapping[str, str]) -> str | None:
    """Why the gateway program's own login command must not run (None: it may)."""

    pool = next((pool for pool, name in LOGIN_COMMANDS.items() if name == command), None)
    if pool is None:
        return None
    if not pool_offered(pool):
        return texts.LOGIN_STRICT.format(command=command)
    if current_ack(environ, pool) is None:
        return texts.LOGIN_REFUSED.format(command=command, provider=POOL_PROVIDERS[pool])
    return None


# ------------------------------------------------------------------ accounts


def auth_dir(runtime: Any) -> Path:
    gateway = runtime.catalog.docs["gateway"]["gateway"]
    return runtime.home / gateway["auth_dir"]


def pool_of(provider_id: str) -> str:
    pool = ACCOUNT_POOLS.get(provider_id)
    if pool is None:
        raise model.Refused(texts.SIGNIN_NOT_ACCOUNT.format(id=provider_id))
    return pool


def _record_files(directory: Path, pool: str) -> dict[str, tuple[int, int]]:
    """The pool's record files (regular files whose name is its record, by
    the pools' collision-free classification): name -> (size, mtime_ns).
    Names only."""

    found: dict[str, tuple[int, int]] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    table = account_pools.load()
    for name in names:
        if table.classify(name) != pool:
            continue
        try:
            info = os.lstat(directory / name)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found[name] = (info.st_size, info.st_mtime_ns)
    return found


def account_name(pool: str, file_name: str) -> str:
    entry = account_pools.pool(pool)
    if entry is None:
        raise model.Refused(f"unknown account pool {pool!r}")
    return entry.account(file_name)


def accounts(runtime: Any, provider_id: str) -> tuple[str, ...]:
    """The signed-in account names of a provider's pool (file names only)."""

    pool = pool_of(provider_id)
    return tuple(account_name(pool, name) for name in sorted(_record_files(auth_dir(runtime), pool)))


# ------------------------------------------------------------------ sign-in


def method_default(pool: str, environ: Mapping[str, str], *, wsl: bool = False,
                   which: Callable[[str], str | None] = shutil.which, platform: str | None = None) -> str:
    """How the sign-in opens: a device-flow pool always uses its device
    flow; a browser-flow pool prints an address over SSH, without a display,
    or on WSL without an opener, and opens the browser otherwise."""

    import sys

    entry = account_pools.pool(pool)
    if entry is not None and entry.sign_in.flow == "device":
        return "device"
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "address"
    if wsl:
        return "browser" if opener_argv(environ, which=which) else "address"
    system = sys.platform if platform is None else platform
    if system.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return "address"
    return "browser"


def opener_argv(environ: Mapping[str, str], *, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """The Windows browser opener under WSL: ``wslview``, else ``explorer.exe``
    (the address is appended as one argument; never a shell)."""

    for name in ("wslview", "explorer.exe"):
        found = which(name)
        if found:
            return [found]
    return None


@dataclass(frozen=True)
class SignInPlan(model.Plan):
    provider_id: str
    pool: str
    method: str
    host: str
    invocation: Any  # proxy.LoginInvocation
    records_before: frozenset[tuple[str, int, int]]
    opener: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignInOutcome:
    status: str  # signed-in | unchanged | cancelled | port-busy | failed
    added: tuple[str, ...]
    accounts: tuple[str, ...]
    exit_code: int | None
    detail: str = ""


def _is_wsl(runtime: Any) -> bool:
    from claude_multi.platform import mounts

    try:
        return mounts.wsl(runtime.environ, proc_root=getattr(runtime, "proc_root", Path("/proc"))) is not None
    except OSError:
        return False


def plan_sign_in(runtime: Any, provider_id: str, *, method: str | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> SignInPlan:
    """The sign-in to run: the gateway program's login command for the
    provider's pool, its method, and the records as they are now."""

    from claude_multi import proxy

    pool = pool_of(provider_id)
    if not pool_offered(pool):
        raise model.Refused(texts.SIGNIN_STRICT)
    wsl = _is_wsl(runtime)
    chosen = method or method_default(pool, runtime.environ, wsl=wsl, which=which)
    if chosen not in methods(pool):
        raise model.Refused(f"{texts.ACCOUNT_KINDS[pool]} sign-in has no {chosen!r} method")
    opener: tuple[str, ...] = ()
    if chosen == "browser" and wsl:
        opener = tuple(opener_argv(runtime.environ, which=which) or ())
    environ = runtime.gateway_environ()
    try:
        invocation = proxy.login_invocation(
            LOGIN_COMMANDS[pool], environ=environ, state_root=runtime.session_store.root,
            no_browser=chosen == "address" or bool(opener))
    except errors.ClaudeMultiError as exc:
        raise SignInError(str(exc), remedy=exc.remedy or "claude-multi setup --step gateway") from exc
    before = _record_files(auth_dir(runtime), pool)
    records = frozenset((name, size, mtime) for name, (size, mtime) in before.items())
    host = texts.SIGNIN_HOSTS[pool]
    lines = (f"{texts.ACCOUNT_KINDS[pool]} sign-in at {host} ({chosen})",)
    digest = model.digest_of("sign-in", provider_id, chosen, sorted(records))
    return SignInPlan("sign-in", provider_id, lines, "typed", True, digest, (str(auth_dir(runtime)),),
                      provider_id, pool, chosen, host, invocation, records, opener)


def header(plan: SignInPlan) -> str:
    """What this terminal shows before the sign-in runs: the text of its
    method, naming the pool's account kind and host."""

    template = {"browser": texts.SIGNIN_HEADER_BROWSER, "address": texts.SIGNIN_HEADER_ADDRESS,
                "device": texts.SIGNIN_HEADER_DEVICE}[plan.method]
    return template.format(kind=texts.ACCOUNT_KINDS[plan.pool], host=plan.host)


def _run_child(invocation: Any, opener: tuple[str, ...] = (),
               write: Callable[[str], None] | None = None) -> int:
    """Run the login child with this terminal's stdio. With an ``opener``,
    the child's output is relayed as it comes and the first sign-in address
    it prints is opened once (an argument vector, never a shell)."""

    if not opener:
        completed = subprocess.run(list(invocation.argv), env=dict(invocation.env), cwd=invocation.cwd,
                                   check=False)
        return completed.returncode
    import sys

    process = subprocess.Popen(list(invocation.argv), env=dict(invocation.env), cwd=invocation.cwd,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    out = write or (lambda text: (sys.stdout.write(text), sys.stdout.flush()))
    seen = ""
    opened = False
    descriptor = process.stdout.fileno()
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        out(text)
        if not opened:
            seen = (seen + text)[-8192:]
            address = first_address(seen)
            if address is not None:
                opened = True
                out(texts.SIGNIN_WSL_OPEN + "\n")
                try:
                    subprocess.run([*opener, address], check=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    out(texts.SIGNIN_WSL_OPEN_FAILED + "\n")
    process.stdout.close()
    return process.wait()


def first_address(text: str) -> str | None:
    """The first complete https address in ``text`` (ended by whitespace)."""

    start = text.find("https://")
    if start < 0:
        return None
    rest = text[start:]
    for index, char in enumerate(rest):
        if char.isspace():
            return rest[:index]
    return None


def _interrupt_here(_signum: int, _frame: Any) -> None:
    """The interrupt handler of this process while a sign-in runs: Ctrl-C
    does nothing here. A handled signal returns to its default in the child
    the moment it starts, so the sign-in program still gets the interrupt
    and stops (an ignored one would be inherited and the sign-in would go
    on)."""


def run_sign_in(runtime: Any, plan: SignInPlan, *, ack: Ack | None,
                runner: Callable[..., int] | None = None) -> SignInOutcome:
    """Run the sign-in in this terminal; the record diff decides the outcome
    (the gateway program exits 0 on failure too)."""

    if ack is None or current_ack(runtime.environ, plan.pool) != ack:
        raise model.Refused(texts.LOGIN_REFUSED.format(command=LOGIN_COMMANDS[plan.pool],
                                                       provider=plan.provider_id))
    from claude_multi.cli import consent

    consent.require_human(f"providers sign-in {plan.provider_id}", runtime.gateway_environ())
    if not getattr(runtime, "allow_state_writes", False):
        raise model.Refused(model.READ_ONLY_SETUP)
    directory = auth_dir(runtime)
    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, _interrupt_here)
        installed = True
    except ValueError:  # not the main thread: the child still gets the terminal's interrupt
        installed = False
    try:
        try:
            if runner is not None:
                code = runner(plan.invocation)
            else:
                code = _run_child(plan.invocation, plan.opener)
        except OSError as exc:
            return SignInOutcome("failed", (), accounts(runtime, plan.provider_id), None,
                                 exc.strerror or type(exc).__name__)
    finally:
        if installed:
            # A handler set outside Python reads as None and cannot be put
            # back; Python's own then takes its place.
            signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)
    after = _record_files(directory, plan.pool)
    before = {name: (size, mtime) for name, size, mtime in plan.records_before}
    added = tuple(sorted(name for name, facts in after.items() if before.get(name) != facts))
    names = tuple(account_name(plan.pool, name) for name in sorted(after))
    if code in (130, -signal.SIGINT):
        return SignInOutcome("cancelled", (), names, code)
    if code == PORT_IN_USE_EXIT:
        # The sign-in never started: a record that changed meanwhile is the
        # running gateway refreshing it, not this sign-in.
        return SignInOutcome("port-busy", (), names, code)
    if added:
        return SignInOutcome("signed-in", tuple(account_name(plan.pool, n) for n in added), names, code)
    return SignInOutcome("unchanged", (), names, code)


def outcome_lines(plan: SignInPlan, outcome: SignInOutcome, *, line: bool = False) -> tuple[str, ...]:
    kind = texts.ACCOUNT_KINDS[plan.pool]
    if outcome.status == "signed-in":
        result = [texts.SIGNIN_OK.format(account=", ".join(outcome.added), kind=kind)]
        if len(outcome.accounts) > 1:
            result.append(texts.SIGNIN_MULTI.format(n=len(outcome.accounts), kind=kind))
        return tuple(result)
    if outcome.status == "cancelled":
        return (texts.SIGNIN_CANCELLED,)
    if outcome.status == "port-busy":
        return (texts.SIGNIN_PORT,)
    if outcome.status == "failed":
        return (texts.SIGNIN_FAILED.format(detail=outcome.detail or "the sign-in could not start"),)
    return (texts.SIGNIN_UNCHANGED_LINE.format(id=plan.provider_id) if line else texts.SIGNIN_UNCHANGED,)


# ------------------------------------------------------------------ sign-out


@dataclass(frozen=True)
class SignOutPlan(model.Plan):
    provider_id: str
    pool: str
    accounts: tuple[str, ...]
    files: tuple[str, ...]
    backup_display: str
    affected_profiles: tuple[str, ...]


@dataclass(frozen=True)
class SignOutOutcome:
    status: str  # stopped | still-listed | down
    moved: tuple[str, ...]
    reappeared: tuple[str, ...]
    backup: str
    lines: tuple[str, ...]
    reload: str = "not-needed"
    left: tuple[str, ...] = ()  # records that came back and stay signed in
    kept: tuple[str, ...] = ()  # records that came back, could not go back and stay in the backup

    @property
    def complete(self) -> bool:
        """The records moved, the gateway reloaded and no longer lists the
        pool's models, and no record came back meanwhile."""

        return (self.status == "stopped" and not self.reappeared and not self.left and not self.kept
                and self.reload in ("reloaded", "not-needed"))


def backup_dir(directory: Path, pool: str, *, today: datetime.date | None = None) -> Path:
    """``<auth dir>.signed-out.<pool>.<YYYYMMDD>[.N]``: the first name not taken."""

    day = (today or datetime.datetime.now(datetime.timezone.utc).date()).strftime("%Y%m%d")
    base = directory.parent / f"{directory.name}{BACKUP_INFIX}{pool}.{day}"
    candidate, n = base, 1
    while os.path.lexists(candidate):
        n += 1
        candidate = base.with_name(f"{base.name}.{n}")
    return candidate


def _affected_profiles(runtime: Any, provider_id: str) -> tuple[str, ...]:
    from claude_multi.setup import providers as setup_providers

    return setup_providers.profiles_using(runtime, provider_id)


def plan_sign_out(runtime: Any, provider_id: str) -> SignOutPlan:
    pool = pool_of(provider_id)
    directory = auth_dir(runtime)
    found = _record_files(directory, pool)
    if not found:
        raise model.Refused(texts.NOT_SIGNED_IN)
    names = tuple(sorted(found))
    names_only = tuple(account_name(pool, name) for name in names)
    backup = backup_dir(directory, pool)
    profiles = _affected_profiles(runtime, provider_id)
    shown = paths.display(backup, runtime.environ)
    kind = texts.ACCOUNT_KINDS[pool]
    profiles_line = (texts.PROFILES_LOSE.format(display=kind, names=", ".join(profiles)) if profiles
                     else texts.PROFILES_NONE.format(display=kind))
    lines = tuple(texts.SIGNOUT_BODY.format(accounts=", ".join(names_only), backup=shown,
                                            profiles_line=profiles_line).split("\n"))
    digest = model.digest_of("sign-out", provider_id, sorted((n, s, m) for n, (s, m) in found.items()))
    return SignOutPlan("sign-out", provider_id, lines, "destructive", True, digest, (shown,),
                       provider_id, pool, names_only, names, shown, profiles)


def _fsync_dir(path: Path) -> None:
    from claude_multi.platform import posix_fs

    posix_fs.fsync_directory(path)


def _put_back(source: Path, target: Path) -> bool:
    """Move ``source`` to ``target`` unless something is there now (a record
    a refresh wrote meanwhile keeps its place: both versions are kept).
    Returns whether it moved. Contents are never opened.

    The move is a hard link, which never replaces an existing name, then
    the removal of ``source``. Without hard links (or with ``target``
    present) nothing moves and ``source`` stays where it is: a check
    followed by a rename could replace a record written in between."""

    try:
        os.link(source, target, follow_symlinks=False)
    except OSError:
        return False
    try:
        os.unlink(source)
    except OSError:
        # Both names now point at the same record; the kept one is harmless.
        pass
    return True


def _put_all_back(moves: list[tuple[Path, Path]]) -> list[Path]:
    """Undo ``(from, to)`` moves, newest first, never over a newer file;
    returns the moved-away files that could not go back (they stay where
    they were moved to)."""

    kept: list[Path] = []
    for origin, moved_to in reversed(moves):
        if not _put_back(moved_to, origin):
            kept.append(moved_to)
    for directory in {path.parent for pair in moves for path in pair}:
        try:
            _fsync_dir(directory)
        except OSError:
            pass
    return kept


def _reason(exc: BaseException) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    return str(exc) or type(exc).__name__


def _free_name(directory: Path, name: str) -> Path:
    """``name`` in ``directory``, else ``name.again``, ``name.again.2``, …"""

    candidate = directory / name
    if not os.path.lexists(candidate):
        return candidate
    candidate, n = directory / f"{name}.again", 1
    while os.path.lexists(candidate):
        n += 1
        candidate = directory / f"{name}.again.{n}"
    return candidate


def _reload(runtime: Any) -> tuple[str, tuple[str, ...]]:
    """The shared apply after the records moved: render and verify."""

    from claude_multi.setup import providers as setup_providers

    try:
        applied = setup_providers.rerender(runtime, "providers sign-out")
    except model.SetupError as exc:
        return "refused", (str(exc),)
    return applied.reload, ()


def apply_sign_out(runtime: Any, plan: SignOutPlan, confirmation: model.Confirmation, *,
                   served: Callable[[], frozenset[str] | None] | None = None,
                   clock: Callable[[], float] | None = None,
                   sleep: Callable[[float], None] | None = None,
                   move: Callable[[Path, Path], None] = os.rename,
                   reload: Callable[[Any], tuple[str, tuple[str, ...]]] | None = None) -> SignOutOutcome:
    """Move the pool's records into a new kept backup (renames, nothing is
    deleted), apply and verify the gateway configuration, then watch the
    gateway stop listing the pool's models. Refused while a credential save
    is unconfirmed (the persistence hold)."""

    import time

    from claude_multi.setup import external

    model.check_apply(runtime, plan, confirmation, verb=f"providers sign-out {plan.provider_id}")
    held = external.persistence_held(runtime)
    if held:
        raise model.Refused(texts.SIGNOUT_HOLD.format(reason=held[0]),
                            remedy="claude-multi gateway status")
    directory = auth_dir(runtime)
    found = _record_files(directory, plan.pool)
    if tuple(sorted(found)) != plan.files:
        raise model.Stale("the signed-in accounts")
    backup = backup_dir(directory, plan.pool)
    os.mkdir(backup, 0o700)
    shown = paths.display(backup, runtime.environ)
    moves: list[tuple[Path, Path]] = []
    try:
        for name in plan.files:
            move(directory / name, backup / name)
            moves.append((directory / name, backup / name))
        _fsync_dir(backup)
        _fsync_dir(directory)
    except BaseException as exc:
        # Put every record back, but never over one a refresh wrote
        # meanwhile: then both are kept and the backup stays.
        kept = _put_all_back(moves)
        if kept:
            raise SignOutIncomplete(texts.SIGNOUT_UNDO_INCOMPLETE.format(
                reason=_reason(exc), names=", ".join(sorted(account_name(plan.pool, p.name) for p in kept)),
                backup=shown, id=plan.provider_id,
                auth_dir=paths.display(directory, runtime.environ))) from exc
        try:
            os.rmdir(backup)
        except OSError:
            pass
        raise
    moved = tuple(plan.files)
    reload_status, reload_notes = (reload or _reload)(runtime)
    status = _await_unlisted(runtime, plan.pool, served=served, clock=clock or time.monotonic,
                             sleep=sleep or time.sleep)
    # Records a refresh wrote again meanwhile go into the same backup. A
    # failure (or an interrupt) here puts this step's moves back, so every
    # record that came back stays signed in, and says so.
    reappeared = tuple(sorted(_record_files(directory, plan.pool)))
    again: list[tuple[Path, Path]] = []
    left: tuple[str, ...] = ()
    stuck: tuple[str, ...] = ()
    failure: BaseException | None = None
    try:
        for name in reappeared:
            target = _free_name(backup, name)
            move(directory / name, target)
            again.append((directory / name, target))
        if reappeared:
            _fsync_dir(backup)
            _fsync_dir(directory)
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
        origin = {moved_to: source for source, moved_to in again}
        # A record that cannot go back stays in the backup (named below).
        stuck = tuple(sorted(origin[path].name for path in _put_all_back(again)))
        left = tuple(sorted(set(_record_files(directory, plan.pool)) & set(reappeared)))
    kind = texts.ACCOUNT_KINDS[plan.pool]
    lines = [texts.SIGNED_OUT.format(kind=kind, backup=shown)]
    if status == "still-listed":
        lines.append(texts.SIGNED_OUT_STILL.format(kind=kind))
    elif status == "down":
        lines.append(texts.SIGNED_OUT_DOWN)
    gone = tuple(name for name in reappeared if name not in left)
    if gone and failure is None:
        lines.append(texts.SIGNED_OUT_REAPPEARED.format(names=", ".join(account_name(plan.pool, n) for n in gone)))
    if failure is not None:
        lines.append(texts.SIGNED_OUT_LEFT.format(
            names=", ".join(account_name(plan.pool, n) for n in left) or "none",
            reason=_reason(failure), id=plan.provider_id))
    if stuck:
        lines.append(texts.SIGNED_OUT_KEPT_BACK.format(
            names=", ".join(account_name(plan.pool, n) for n in stuck), backup=shown,
            auth_dir=paths.display(directory, runtime.environ)))
    if reload_status not in ("reloaded", "not-needed", "down"):
        lines.append(texts.SIGNED_OUT_RELOAD.format(detail="; ".join(reload_notes) or reload_status))
    outcome = SignOutOutcome(status, moved, reappeared, shown, tuple(lines), reload_status, left, stuck)
    if not outcome.complete:
        outcome = SignOutOutcome(status, moved, reappeared, shown,
                                 (*lines, texts.SIGNED_OUT_INCOMPLETE), reload_status, left, stuck)
    return outcome


def pool_aliases(runtime: Any, pool: str) -> frozenset[str]:
    """Every selector the pool's provider serves (the catalog lines' and
    passthrough routes')."""

    from claude_multi import operator as operator_mod

    provider_id = POOL_PROVIDERS[pool]
    docs = runtime.catalog.docs
    provider = docs["providers"]["providers"].get(provider_id) or {}
    bases = {route["name"] for route in provider.get("passthrough_routes", [])}
    for entry in docs["models-v2"]["models"].values():
        if entry.get("provider") == provider_id:
            bases.update(operator_mod.line_aliases(entry))
    return frozenset(bases)


def _served_now(runtime: Any) -> frozenset[str] | None:
    try:
        ids, _status = runtime.served_models(runtime.gateway_token())
    except (errors.ClaudeMultiError, OSError, ValueError):
        return None
    return None if ids is None else frozenset(ids)


def _await_unlisted(runtime: Any, pool: str, *, served: Callable[[], frozenset[str] | None] | None,
                    clock: Callable[[], float], sleep: Callable[[float], None]) -> str:
    """Poll the served models (at most :data:`VERIFY_SECONDS`) until no alias
    of the pool is listed: ``stopped``, ``still-listed`` or ``down``."""

    read = served or (lambda: _served_now(runtime))
    aliases = pool_aliases(runtime, pool)
    deadline = clock() + VERIFY_SECONDS
    while True:
        listed = read()
        if listed is None:
            return "down"
        if not (aliases & listed):
            return "stopped"
        if clock() >= deadline:
            return "still-listed"
        sleep(0.25)
