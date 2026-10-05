"""The safety transaction of the installer, the updater and their rollbacks.

``packaging/install.sh`` and ``claude-multi update`` change the same
installation (``<data root>/install``: ``versions/<v>``, ``current``,
``previous``) and the same state root. Before either mutates anything it
runs the checks here, and it runs them again once it holds the install lock
and its gateway inhibition, so nothing it decided on can have changed
meanwhile:

- **Ownership.** The state root's channel marker is read with the runtime's
  own rules (:func:`claude_multi.installs.read_marker`: exact bytes, a
  private regular file of this user, never a link). A malformed marker
  refuses; a valid marker of another channel refuses unless the caller
  explicitly migrates (the installer's ``--migrate-from-nix``), which still
  never accepts a malformed one.
- **One gateway inhibition.** The installer and the updater record their
  own inhibition of gateway changes (:mod:`claude_multi.gateway_inhibition`,
  owner ``installer`` or ``updater``, stale once the owner process is gone)
  before the first change, advance its phase at every step and end it on
  success or when nothing was changed; meanwhile every start, stop, service
  hand-off and configuration write of another owner refuses with the
  inhibited outcome. Another owner's record refuses them in turn (a service
  hand-off, another installer or updater, a machine move; an earlier
  release's ``service-handoff.json`` reads as one, an unreadable record
  too). An interruption after the installation started changing keeps the
  record; the same owner takes it back with :func:`recover` (explicit, under
  the install lock, idempotent) and finishes the run.
- **State format.** The version that becomes current must read the state on
  disk (forward migration only).
- **The running gateway's installation.** The gateway keeps executing the
  binary it was started from (its start record names that file), so the
  version directory holding it stays installed while that gateway runs,
  whatever ``current`` and ``previous`` say. When that cannot be known (an
  unreadable start record, a held instance lock without one, a process
  that cannot be examined), nothing is removed and nothing is replaced in
  place.
- **The persistence hold.** Neither the installer nor the updater stops the
  gateway; a change of the gateway binary is a restart the user makes later
  with ``claude-multi gateway restart`` (which honours the hold itself). A
  gateway-changing switch is still refused while the hold vetoes a restart,
  so a replacement never waits on a gateway that must not restart.
- **Claude Code copies.** Their cleanup is
  :func:`claude_multi.retention.prune_copies` (each copy's use lock taken
  exclusively without waiting and held through its deletion): a copy a
  session runs is kept and reported in use.

Shell owners run it through a bundle's own interpreter (``python3 -I -B``
with the bundle's site directory first on ``sys.path``), and the inhibition
itself through ``claude_multi.gateway_inhibition``'s command the same way
(the token in ``CLAUDE_MULTI_INHIBITION_TOKEN``)::

    install_txn preflight --state-root S --install-root R [--migrate]
                          [--version V --readable-format N] [--bundle DIR]
                          [--token-env] [--recoverable OWNER]
    install_txn protected --state-root S --install-root R
    install_txn claim --state-root S [--migrate]         (token from the environment)
    install_txn prune-copies --version V                 (Claude Code copies)
    install_txn receipt --install-root R --launcher PATH ... [--path-line FILE LINE]
    install_txn switch --install-root R --current V [--previous V]
    install_txn finish-switch --install-root R            (prints what it put back)

``switch`` and ``finish-switch`` are :func:`claude_multi.self_update.switch`
and :func:`~claude_multi.self_update.finish_switch` (the caller holds the
install lock): ``current`` and ``previous`` change as one recorded step, and
an interrupted switch is put back as it was before it.

Exit 0 ok, 1 refused, 2 usage, 3 unknown (``protected``: keep everything).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from claude_multi import errors, gateway_inhibition, installs, paths, service, state, termtext
from claude_multi.platform import posix_fs

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_UNKNOWN = 0, 1, 2, 3
TOKEN_ENV = gateway_inhibition.TOKEN_ENV
OWNER_INSTALLER = "installer"
OWNER_UPDATER = "updater"
VERSIONS = "versions"


class TransactionError(errors.ClaudeMultiError, RuntimeError):
    """An install, update or rollback must not proceed; nothing was changed."""


# ------------------------------------------------------------ ownership


def check_ownership(state_root: Path | str, environ: Mapping[str, str], *, migrate: bool = False) -> str | None:
    """The recorded owner (None: none yet), validated; refuses another
    channel's state root unless ``migrate``."""

    try:
        owner = installs.read_marker(state_root, environ)
    except installs.ChannelError as exc:
        raise TransactionError(f"{exc}; nothing was changed", remedy=exc.remedy) from exc
    if owner is None or owner == "bundle" or migrate:
        return owner
    shown = paths.display(state_root, environ)
    raise TransactionError(
        f"this account's claude-multi state ({shown}) belongs to the {owner} installation; nothing was changed",
        remedy="to move it to the installed release, run the installer again with --migrate-from-nix, "
               "then remove the other installation")


def claim(state_root: Path | str, environ: Mapping[str, str], *, migrate: bool = False,
          token: str | None = None) -> str | None:
    """Record the bundle channel in the state root (the format and lock of
    :func:`claude_multi.installs.claim_marker`); returns the owner it
    replaced, if any. A foreign owner is replaced only when ``migrate``.
    The check and the write sit inside the root's writer fence with its
    inhibition checked: only the holder of ``token`` (the installer's own
    record) passes a recorded inhibition."""

    root = state.ensure_private_dir(state_root)
    try:
        with gateway_inhibition.fenced([root], token=token), state.FileLock(installs.marker_path(root)):
            owner = check_ownership(root, environ, migrate=migrate)
            if owner == "bundle":
                return None
            installs.write_marker(root, "bundle")
            return owner
    except (gateway_inhibition.Inhibited, gateway_inhibition.InhibitionError) as exc:
        raise _transaction_error(exc) from exc


def check_state(state_root: Path | str, readable: int, version: str, *,
                rollback_from: str | None = None) -> int:
    """The state format on disk; refuses when ``version`` reads only up to
    ``readable`` (forward migration only: a rollback never downgrades state)."""

    from claude_multi import self_update

    try:
        on_disk = self_update.state_version(Path(state_root))
    except self_update.UpdateError as exc:
        raise TransactionError(str(exc), remedy=exc.remedy) from exc
    if on_disk <= readable:
        return on_disk
    if rollback_from is not None:
        raise TransactionError(
            f"cannot roll back to {version}: your state was upgraded to format {on_disk}, which {version} "
            f"cannot read (it reads up to {readable}); nothing was changed",
            remedy=f"stay on {rollback_from}; state is only ever migrated forward")
    raise TransactionError(
        f"claude-multi {version} cannot read your state (format {on_disk}; it reads up to {readable}); "
        "nothing was changed", remedy="install a newer version instead")


# ------------------------------------------------------------ inhibition

# The phases an install, update or rollback records (the inhibition's
# ``phase``). Until ``replace`` nothing the installation runs from has
# changed (only temporary files, removed on failure), so a failure ends the
# record; from ``replace`` on an interruption keeps it, and the owner's
# recovery finishes the run (``sh install.sh --repair`` for the installer,
# ``claude-multi update`` again for the updater).
PHASE_PREPARE = "prepare"
PHASE_DOWNLOAD = "download"
PHASE_PIN = "pin"
PHASE_REPLACE = "replace"
PHASE_SWITCH = "switch"
PHASE_PRUNE = "prune"
PHASE_LAUNCHERS = "launchers"
RESTORABLE_PHASES = frozenset({PHASE_PREPARE, PHASE_DOWNLOAD, PHASE_PIN})
REMEDIES = {
    OWNER_INSTALLER: "sh install.sh --repair",
    OWNER_UPDATER: "claude-multi update (run it again to finish the interrupted run)",
}


def _transaction_error(exc: BaseException) -> TransactionError:
    if isinstance(exc, errors.ClaudeMultiError):
        return TransactionError(termtext.visible_message(exc), remedy=exc.remedy)
    return TransactionError(f"the gateway inhibition cannot be recorded ({exc}); nothing was changed")


def guard(state_root: Path | str, *, token: str | None = None, recoverable: str | None = None,
          now: datetime.datetime | None = None) -> None:
    """Refuse while another owner inhibits gateway changes: any recorded
    inhibition the holder of ``token`` does not own, an earlier release's
    gateway service hand-off record and an unreadable record included.
    ``recoverable``: an interrupted run of that owner passes (the caller
    recovers it under the install lock, :func:`recover`)."""

    record = gateway_inhibition.blocking(state_root, token=token)
    if record is None:
        return
    if recoverable is not None and record.readable and record.owner == recoverable and record.stale(now=now):
        return
    message, remedy = gateway_inhibition.refusal(record, "nothing was changed", now=now)
    raise TransactionError(message, remedy=remedy)


@dataclass
class Inhibition:
    """The caller's own inhibition of gateway changes (its record's token).

    ``advance`` records the next phase and proves the record is still this
    owner's; ``restorable`` says whether a failure now leaves the
    installation as it was (then ``end`` removes the record); ``recovered``:
    the record of an interrupted run this owner took back."""

    state_root: Path
    owner: str
    token: str | None
    phase: str = PHASE_PREPARE
    recovered: bool = False

    @property
    def restorable(self) -> bool:
        return not self.recovered and self.phase in RESTORABLE_PHASES

    def advance(self, phase: str) -> None:
        if self.token is None:
            raise TransactionError("this run no longer holds its gateway inhibition; nothing more was changed")
        try:
            gateway_inhibition.advance(self.state_root, self.token, phase)
        except (gateway_inhibition.InhibitionError, OSError, errors.ClaudeMultiError) as exc:
            raise TransactionError(f"{exc}; nothing more was changed",
                                   remedy="check: claude-multi doctor") from exc
        self.phase = phase

    @contextlib.contextmanager
    def fenced(self, what: str = "nothing was changed") -> Iterator[None]:
        """A check-then-write of this owner, inside the root's writer fence
        with its inhibition checked (only this owner passes)."""

        try:
            with gateway_inhibition.fenced([self.state_root], token=self.token, what=what):
                yield
        except (gateway_inhibition.Inhibited, gateway_inhibition.InhibitionError) as exc:
            raise _transaction_error(exc) from exc

    def end(self) -> None:
        """Remove the record (on success, or when nothing was changed)."""

        if self.token is not None:
            try:
                gateway_inhibition.end(self.state_root, self.token)
            except (gateway_inhibition.InhibitionError, OSError, errors.ClaudeMultiError) as exc:
                raise _transaction_error(exc) from exc
        self.token = None


def begin(state_root: Path | str, *, owner: str, purpose: str, remedy: str | None = None,
          pid: int | None = None) -> Inhibition:
    """Record this owner's inhibition (phase ``prepare``) before its first
    change; stale once the owner process ``pid`` (default: this one) is
    gone or makes no progress for the default bound. Refused while any other
    inhibition is recorded, an interrupted run of the same owner included:
    that one is finished through :func:`recover`."""

    root = Path(state_root)
    try:
        token = gateway_inhibition.begin(root, owner=owner, purpose=purpose, phase=PHASE_PREPARE,
                                         remedy=remedy or REMEDIES[owner],
                                         expiry=gateway_inhibition.owner_process(pid))
    except (gateway_inhibition.InhibitionError, OSError, errors.ClaudeMultiError) as exc:
        raise _transaction_error(exc) from exc
    return Inhibition(root, owner, token)


def recover(state_root: Path | str, owner: str) -> Inhibition | None:
    """Take back ``owner``'s interrupted inhibition (its token and phase), so
    the caller finishes the run and then ends it; None when none is recorded.
    Refused for another owner's record, an unreadable one, or one whose
    owner still runs. Explicit and idempotent: the caller holds the install
    lock, and recovering when nothing was interrupted does nothing."""

    root = Path(state_root)
    try:
        record = gateway_inhibition.recover(root, owner)
    except (gateway_inhibition.InhibitionError, OSError, errors.ClaudeMultiError) as exc:
        raise _transaction_error(exc) from exc
    if record is None:
        return None
    return Inhibition(root, owner, record.token, record.phase or PHASE_PREPARE, recovered=True)


# ------------------------------------------------------------ the running gateway


def version_of(binary: str | Path, install_root: Path | str) -> str | None:
    """The installed version whose directory holds ``binary`` (a resolved
    path), or None when it is not inside ``<install root>/versions``."""

    versions = os.path.realpath(Path(install_root) / VERSIONS)
    real = os.path.realpath(binary)
    prefix = versions + os.sep
    if not real.startswith(prefix):
        return None
    name = real[len(prefix):].split(os.sep, 1)[0]
    return name or None


def _alive(stamp: service.ExecStamp, proc_root: Path, platform: str) -> bool | None:
    if platform == "darwin":
        from claude_multi.platform import darwin_process

        return darwin_process.gateway_pid(stamp)[1]
    from claude_multi.platform import linux_process

    return linux_process.gateway_pid(stamp, proc_root=proc_root, readlink=os.readlink, main_pid=lambda: None)[1]


def protected_versions(install_root: Path | str, state_root: Path | str, *,
                       proc_root: Path | str = "/proc", platform: str | None = None) -> frozenset[str] | None:
    """The installed versions a running gateway executes from (empty when
    none); None when that cannot be known (then nothing may be removed or
    replaced in place)."""

    root = Path(state_root)
    workdir = service.gateway_workdir(root)
    try:
        os.lstat(workdir / service.EXEC_STAMP)
        recorded = True
    except FileNotFoundError:
        recorded = False
    except OSError:
        return None
    lock = posix_fs.lock_held(service.instance_lock_path(root))
    if not recorded:
        # No start record: free lock = nothing runs; a held (or uncheckable)
        # lock without a record is a start in progress or a failed record.
        return frozenset() if lock is False else None
    stamp = service.read_exec_stamp(workdir)
    if stamp is None:
        return None
    version = version_of(stamp.binary, install_root)
    if version is None:
        return frozenset()  # it runs from elsewhere (another installation's files)
    alive = _alive(stamp, Path(proc_root), sys.platform if platform is None else platform)
    if alive is False:
        return frozenset()
    return frozenset({version})  # running, or not provably stopped


# ------------------------------------------------------------ the persistence hold


def hold_reason(state_root: Path | str, environ: Mapping[str, str], *,
                gateway: object | None = None) -> str | None:
    """Why a gateway-changing switch must wait (the persistence hold of a
    gateway that may be running), or None. ``gateway`` is a
    :class:`claude_multi.gateway_lifecycle.Gateway` (default: one built from
    the packaged catalog for ``state_root``)."""

    from claude_multi import gateway_lifecycle

    if gateway is None:
        gateway = _packaged_gateway(state_root, environ)
    try:
        seen = gateway.observe()
        if seen.state == gateway_lifecycle.STOPPED:
            return None
        report = gateway.persistence_hold(seen)
    except (OSError, errors.ClaudeMultiError) as exc:
        return f"the gateway's persistence hold cannot be evaluated ({exc})"
    if report.held:
        from claude_multi import gateway_hold

        return (f"the gateway's persistence hold is active: {gateway_hold.describe(report)} — "
                f"{gateway_hold.remedy(report)}")
    return None


def _packaged_gateway(state_root: Path | str, environ: Mapping[str, str]):
    from claude_multi import catalog, gateway_lifecycle, resources_root

    try:
        docs = catalog.load_raw(resources_root())["docs"]
    except (OSError, KeyError, errors.ClaudeMultiError) as exc:
        raise TransactionError(f"the packaged catalog cannot be read ({exc})") from exc
    providers = docs["providers"]["providers"]
    names = {*providers, *(entry["transport"]["pool"] for entry in providers.values()
                           if entry.get("transport", {}).get("kind") == "oauth-pool")}
    return gateway_lifecycle.Gateway(home=paths.home(environ), state_root=Path(state_root), environ=environ,
                                     gateway_document=docs["gateway"], providers=names)


# ------------------------------------------------------------ Claude Code copies


def prune_copies(environ: Mapping[str, str], *, launcher_version: str,
                 contract: Mapping[str, object] | None = None) -> list[str]:
    """The installer's and the updater's cleanup of Claude Code copies: the
    use-lock prune (:func:`claude_multi.retention.prune_copies`) for the
    release that is current now (``contract``: its packaged native contract,
    by default this package's). Returns the lines to print; a copy a session
    runs is kept and reported in use."""

    from claude_multi import catalog, pin, resources_root, retention

    try:
        if contract is None:
            contract = catalog.load_raw(resources_root())["docs"]["native-contract"]
        outcome = retention.prune_copies(contract, environ, launcher_version=launcher_version)
    except (OSError, KeyError, TypeError, ValueError, errors.ClaudeMultiError) as exc:
        return [f"Claude Code copies were not cleaned up ({exc}); `{pin.SETUP_COMMAND}` does it later."]
    return retention.prune_lines(outcome, environ)


# ------------------------------------------------------------ the command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install_txn", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight")
    pre.add_argument("--state-root", required=True)
    pre.add_argument("--install-root", required=True)
    pre.add_argument("--migrate", action="store_true")
    pre.add_argument("--version")
    pre.add_argument("--readable-format", type=int)
    pre.add_argument("--rollback-from", help="the version a rollback leaves (its refusal names it)")
    pre.add_argument("--bundle", help="the bundle that becomes current (its gateway decides the hold check)")
    pre.add_argument("--token-env", action="store_true", help=f"the caller's inhibition token is in {TOKEN_ENV}")
    pre.add_argument("--recoverable", choices=(OWNER_INSTALLER, OWNER_UPDATER),
                     help="an interrupted run of this owner passes (the caller recovers it under the install lock)")
    keep = commands.add_parser("protected")
    keep.add_argument("--state-root", required=True)
    keep.add_argument("--install-root", required=True)
    owner = commands.add_parser("claim")
    owner.add_argument("--state-root", required=True)
    owner.add_argument("--migrate", action="store_true")
    copies = commands.add_parser("prune-copies")
    copies.add_argument("--version", required=True, help="the release that is current now")
    receipt = commands.add_parser("receipt")
    receipt.add_argument("--install-root", required=True)
    receipt.add_argument("--launcher", action="append", default=[], required=True)
    receipt.add_argument("--path-line", nargs=2, metavar=("FILE", "LINE"))
    links = commands.add_parser("switch")
    links.add_argument("--install-root", required=True)
    links.add_argument("--current", required=True, help="the version that becomes current")
    links.add_argument("--previous", help="the version that becomes previous (default: previous stays)")
    finish = commands.add_parser("finish-switch")
    finish.add_argument("--install-root", required=True)
    return parser


def _absolute(value: str, what: str) -> Path:
    if not os.path.isabs(value):
        raise TransactionError(f"{what} must be an absolute path")
    return Path(value)


def _gateway_changes(bundle: Path, install_root: Path) -> bool:
    from claude_multi import self_update

    new = self_update.read_bundle_manifest(bundle)
    installation = self_update.read_installation(install_root)
    return installation is not None and installation.current.gateway_sha256 != new.gateway_sha256


def preflight(args: argparse.Namespace, environ: Mapping[str, str]) -> None:
    state_root = _absolute(args.state_root, "--state-root")
    install_root = _absolute(args.install_root, "--install-root")
    token = environ.get(TOKEN_ENV) or None if args.token_env else None
    check_ownership(state_root, environ, migrate=args.migrate)
    guard(state_root, token=token, recoverable=args.recoverable)
    if args.readable_format is not None:
        check_state(state_root, args.readable_format, args.version or "this version",
                    rollback_from=args.rollback_from)
    if args.bundle:
        from claude_multi import self_update

        try:
            changes = _gateway_changes(Path(args.bundle), install_root)
        except self_update.UpdateError as exc:
            raise TransactionError(str(exc)) from exc
        if changes:
            reason = hold_reason(state_root, environ)
            if reason:
                raise TransactionError(f"{reason}; nothing was changed",
                                       remedy="keep the gateway running until it is clear "
                                              "(claude-multi gateway status), then run the installer again")


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         stdout=None, stderr=None) -> int:
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    try:
        if args.command == "preflight":
            preflight(args, env)
            return EXIT_OK
        if args.command == "protected":
            found = protected_versions(_absolute(args.install_root, "--install-root"),
                                       _absolute(args.state_root, "--state-root"))
            if found is None:
                err.write("claude-multi installer: whether a running gateway uses an installed version "
                          "cannot be checked; every installed version is kept\n")
                return EXIT_UNKNOWN
            for name in sorted(found):
                out.write(f"{name}\n")
            return EXIT_OK
        if args.command == "claim":
            replaced = claim(_absolute(args.state_root, "--state-root"), env, migrate=args.migrate,
                             token=env.get(TOKEN_ENV) or None)
            if replaced:
                out.write(f"{replaced}\n")
            return EXIT_OK
        if args.command == "prune-copies":
            for line in prune_copies(env, launcher_version=args.version):
                out.write(f"claude-multi: {termtext.visible_text(line)}\n")
            return EXIT_OK
        if args.command in ("switch", "finish-switch"):
            from claude_multi import self_update

            root = _absolute(args.install_root, "--install-root")
            if args.command == "switch":
                self_update.switch(root, current=args.current, previous=args.previous)
                return EXIT_OK
            done = self_update.finish_switch(root)
            if done:
                out.write(f"{termtext.visible_text(done)}\n")
            return EXIT_OK
        if args.command == "receipt":
            from claude_multi import install_receipt

            install_receipt.record(_absolute(args.install_root, "--install-root"),
                                   [_absolute(path, "--launcher") for path in args.launcher],
                                   tuple(args.path_line) if args.path_line else None)
            return EXIT_OK
    except (TransactionError, errors.ClaudeMultiError, OSError) as exc:
        message = termtext.visible_message(exc) if isinstance(exc, errors.ClaudeMultiError) else str(exc)
        err.write(f"claude-multi installer: {message}\n")
        remedy = getattr(exc, "remedy", None)
        if remedy:
            err.write(f"  fix: {termtext.visible_text(remedy)}\n")
        return EXIT_REFUSED
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
