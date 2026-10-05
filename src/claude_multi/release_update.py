"""``claude-multi update`` and the card's U: one journey for every channel.

An installed release (the ``bundle`` channel) updates itself::

    check   fetch the latest release's MANIFEST.json, SHA256SUMS and its
            signature (a few kilobytes), verify the signature with the
            release key this installation carries, compare versions
    plan    what changes: size, Claude Code, state format (and whether a
            rollback stays possible), gateway restart
    confirm y/N on a terminal, or --yes
    apply   download, verify, put the incoming Claude Code in place, switch
            (self_update.apply under the install lock and this run's
            gateway inhibition)

An equal version is "up to date": nothing is planned or replaced.
``--rollback`` returns to the previous version (refused once the state moved
past what it reads). Each step is a phase of the run's gateway inhibition
(owner ``updater``); a run interrupted after it started changing the
installation keeps it, and the next update or rollback finishes that run
before anything else. The cleanup removes old versions and the Claude Code
copies no installed release needs (a copy in use is kept and reported). The Nix package and a source checkout are updated
where they came from; this command says how and changes nothing there.

The checks before any change: the running launcher is the installation's
``current`` version, the state root belongs to the installed release
(:func:`claude_multi.install_txn.check_ownership`), the signature verifies
with the packaged release key (:func:`claude_multi.trust.release_signers`;
there is no override), and downloads use :class:`HttpsTransport` (https
only, hosts fixed by the release location, size caps, the certificate trust
of :mod:`claude_multi.tls`) or a local release directory.

Exit codes: 0 done or nothing to do, 1 refused, not ready or failed,
2 usage, 3 declined, 130 cancelled.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

from claude_multi import errors, install_txn, installs, paths, self_update, strict_json, termtext, tls, trust

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_DECLINED, EXIT_CANCELLED = 0, 1, 2, 3, 130
TIMEOUT = 30.0
AGE_NOTICE_DAYS = 30
# Release hosts that hand downloads to another host of the same service.
REDIRECT_HOSTS: dict[str, frozenset[str]] = {
    "github.com": frozenset({"objects.githubusercontent.com", "release-assets.githubusercontent.com"}),
}
MAX_REDIRECTS = 5
_CHUNK = 1 << 20

NIX_LINES = (
    "This claude-multi is the Nix package: update it through your flake "
    "(nix flake update claude-multi, then rebuild or reinstall the package).",
    "Nothing was changed. A rollback is your Nix generation rollback; state is only ever migrated forward, "
    "so an older generation refuses state a newer one upgraded.",
)
SOURCE_LINES = (
    "This claude-multi runs from a source checkout: update the checkout (git pull) instead.",
    "Nothing was changed.",
)
OTHER_LINES = (
    "This claude-multi was not installed by the claude-multi installer, so it cannot update itself.",
    "Nothing was changed. Update it the way you installed it, or install a release with install.sh.",
)


class TransportError(self_update.UpdateError):
    """A release file could not be fetched."""


# ------------------------------------------------------------ transport


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: frozenset[str]) -> None:
        super().__init__()
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # urllib's redirect hook
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or (target.hostname or "").lower() not in self.allowed:
            raise TransportError(f"the release server redirected to {target.scheme}://{target.hostname}, "
                                 "which is not a release host; nothing was downloaded")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https(url: str, what: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise self_update.UpdateError(f"{what} must be an https URL without credentials: {url!r}")
    return parts


class HttpsTransport:
    """Release files over https: ``base_url`` (``{version}`` substituted) for
    a version's files, ``latest_url`` for the latest release's
    ``MANIFEST.json`` and checksums. Redirects stay on https and on the
    release host (plus its known download host); every response is capped
    at the size the caller allows."""

    def __init__(self, base_url: str, latest_url: str | None = None, *,
                 environ: Mapping[str, str] | None = None, timeout: float = TIMEOUT,
                 opener: Callable[..., Any] | None = None) -> None:
        self.base_url = base_url
        self.latest_url = latest_url or base_url
        hosts: set[str] = set()
        for url, what in ((self.base_url, "the release location"), (self.latest_url, "the latest-release location")):
            host = (_https(url.replace("{version}", "0.0.0"), what).hostname or "").lower()
            hosts.add(host)
            hosts |= REDIRECT_HOSTS.get(host, frozenset())
        self.hosts = frozenset(hosts)
        self.timeout = timeout
        self._environ = os.environ if environ is None else environ
        self._opener = opener

    def url(self, name: str, version: str | None) -> str:
        if "/" in name or name.startswith("."):
            raise self_update.UpdateError(f"not a release file name: {name!r}")
        base = self.latest_url if version is None else self.base_url.replace("{version}", version)
        return base.replace("{version}", version or "").rstrip("/") + "/" + urllib.parse.quote(name)

    def _open(self, url: str):
        if self._opener is not None:
            return self._opener(url, timeout=self.timeout)
        handlers = [urllib.request.HTTPSHandler(context=tls.context(self._environ)), _Redirects(self.hosts),
                    urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, headers={"User-Agent": "claude-multi-update"})
        return opener.open(request, timeout=self.timeout)

    def _response(self, name: str, version: str | None):
        url = self.url(name, version)
        try:
            response = self._open(url)
        except TransportError:
            raise
        except urllib.error.HTTPError as exc:
            raise TransportError(f"the release server answered {exc.code} for {name}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"cannot reach the release server for {name}: {reason}") from exc
        final = urllib.parse.urlsplit(response.geturl() if hasattr(response, "geturl") else url)
        if final.scheme != "https" or (final.hostname or "").lower() not in self.hosts:
            response.close()
            raise TransportError(f"{name} came from {final.hostname}, which is not a release host")
        return response

    def fetch(self, name: str, version: str | None, limit: int) -> bytes:
        with contextlib.closing(self._response(name, version)) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise TransportError(f"the release {name} is larger than {limit} bytes")
        return data

    def download(self, name: str, version: str, destination: Path, size: int) -> None:
        copied = 0
        with contextlib.closing(self._response(name, version)) as response, open(destination, "wb") as out:
            while chunk := response.read(_CHUNK):
                copied += len(chunk)
                if copied > size:
                    raise TransportError(f"{name} is larger than the {size} bytes the release lists")
                out.write(chunk)


def release_location(bundle: Path) -> tuple[str | None, str | None]:
    """``(base_url, latest_url)`` the installed bundle names (both may be None)."""

    try:
        document = strict_json.load(bundle / self_update.MANIFEST)
    except (OSError, strict_json.StrictJSONError):
        return None, None
    release = document.get("release") if isinstance(document, dict) else None
    if not isinstance(release, dict):
        return None, None
    base, latest = release.get("base_url"), release.get("latest_url")
    return (base if isinstance(base, str) and base else None,
            latest if isinstance(latest, str) and latest else None)


# ------------------------------------------------------------ the journey


Ask = Callable[[str], "bool | None"]


@dataclass
class Journey:
    """What one update run acts on, with its seams.

    ``running``: the installation tree the launcher runs from (None when
    unknown); ``hold``, ``protected``, ``prune_copies`` and ``acquire_pin``
    are the safety callbacks :func:`claude_multi.self_update.apply` requires.
    """

    environ: Mapping[str, str]
    state_root: Path
    install_root: Path
    channel: str | None
    running: Path | None
    hold: Callable[[], str | None]
    protected: Callable[[], frozenset[str] | None]
    prune_copies: self_update.PruneCopies
    acquire_pin: self_update.AcquirePin | None = None
    platform: str | None = None
    signers: Callable[[], Any] = trust.release_signers
    transport: Callable[[str | None, str | None, Path], self_update.Transport] | None = None
    today: Callable[[], datetime.date] = field(default=lambda: datetime.datetime.now(datetime.timezone.utc).date())


def _display(journey: Journey, path: Path) -> str:
    return paths.display(path, journey.environ)


def channel_lines(channel: str | None) -> tuple[str, ...]:
    if channel == "nix":
        return NIX_LINES
    if channel == "source":
        return SOURCE_LINES
    return OTHER_LINES


def release_age(release_date: str, today: datetime.date) -> int | None:
    try:
        return (today - datetime.date.fromisoformat(release_date)).days
    except ValueError:
        return None


def age_text(version: str, release_date: str, today: datetime.date) -> str:
    days = release_age(release_date, today)
    if days is None or days < 0:
        return f"claude-multi {version} (released {release_date})"
    unit = "day" if days == 1 else "days"
    return f"claude-multi {version} was released {release_date}, {days} {unit} ago"


def installed_release(journey: Journey) -> self_update.Installation:
    """The installation this launcher is, validated (refusals raise)."""

    if journey.channel != "bundle":
        raise self_update.UpdateError(channel_lines(journey.channel)[0])
    installation = self_update.read_installation(journey.install_root)
    if installation is None:
        raise self_update.UpdateError(f"no installed release in {_display(journey, journey.install_root)}",
                                      remedy="install one with install.sh (sh install.sh --repair restores a damaged one)")
    current = os.path.realpath(journey.install_root / self_update.CURRENT)
    if journey.running is None or os.path.realpath(journey.running) != current:
        where = _display(journey, journey.running) if journey.running is not None else "an unknown place"
        raise self_update.UpdateError(
            f"this launcher runs from {where}, not from the installed release's current version "
            f"({_display(journey, journey.install_root / self_update.CURRENT)}); nothing was changed",
            remedy="run the claude-multi on your PATH (~/.local/bin/claude-multi)")
    try:
        install_txn.check_ownership(journey.state_root, journey.environ)
    except install_txn.TransactionError as exc:
        raise self_update.UpdateError(str(exc), remedy=exc.remedy) from exc
    return installation


def _verifier(journey: Journey) -> self_update.Verifier:
    try:
        signers = journey.signers()
    except trust.TrustError as exc:
        raise self_update.UpdateError(f"this release cannot verify updates: {exc}",
                                      remedy="install a newer release with its installer") from exc

    def verify(sums: bytes, signature: bytes) -> None:
        trust.verify(sums, signature, signers)

    return verify


def _transport(journey: Journey, installation: self_update.Installation, *, from_dir: str | None,
               base_url: str | None) -> tuple[self_update.Transport, str]:
    if from_dir is not None:
        directory = Path(from_dir)
        if not directory.is_dir():
            raise self_update.UpdateError(f"--from-dir {from_dir} is not a directory")
        return self_update.DirectoryTransport(directory), _display(journey, directory)
    if journey.transport is not None:
        return journey.transport(base_url, None, installation.current.path), "the release server"
    if base_url is not None:
        transport = HttpsTransport(base_url, base_url, environ=journey.environ)
        return transport, urllib.parse.urlsplit(base_url).hostname or base_url
    base, latest = release_location(installation.current.path)
    if base is None:
        raise self_update.UpdateError(
            "this release names no download location, so it cannot check for updates by itself",
            remedy="download the newer release and run: claude-multi update --from-dir DIR")
    transport = HttpsTransport(base, latest, environ=journey.environ)
    return transport, urllib.parse.urlsplit(latest or base).hostname or base


@contextlib.contextmanager
def _transaction(journey: Journey, purpose: str) -> Iterator[install_txn.Inhibition]:
    """This run's gateway inhibition (owner ``updater``) around the install
    lock's work: recorded before the first change, ended on success or when
    nothing was changed; a failure after the installation started changing
    keeps it (:class:`claude_multi.self_update.Interrupted`) for the next
    run to finish."""

    try:
        held = install_txn.begin(journey.state_root, owner=install_txn.OWNER_UPDATER, purpose=purpose)
    except install_txn.TransactionError as exc:
        raise self_update.UpdateError(str(exc), remedy=exc.remedy) from exc
    try:
        try:
            install_txn.check_ownership(journey.state_root, journey.environ)
            install_txn.guard(journey.state_root, token=held.token)
        except install_txn.TransactionError as exc:
            raise self_update.UpdateError(str(exc), remedy=exc.remedy) from exc
        yield held
    except BaseException as exc:
        if held.restorable:
            held.end()
            raise
        cancelled = isinstance(exc, KeyboardInterrupt)
        cause = "it was cancelled" if cancelled else termtext.visible_text(str(exc) or type(exc).__name__)
        raise self_update.Interrupted(
            f"{purpose} stopped in its {held.phase} step ({cause}); gateway changes stay paused until it is "
            "finished", phase=held.phase, cancelled=cancelled,
            remedy="run claude-multi update again: it finishes the interrupted run first") from exc
    else:
        held.end()


def _recover(journey: Journey, out: TextIO) -> None:
    """Finish an interrupted update or rollback of this installation before
    anything else (the record says ``updater``): under the install lock,
    take its inhibition back, put back what it moved aside, remove what it
    left half made, and end it. Another owner's record is left alone (the
    transaction then refuses with that owner's remedy)."""

    from claude_multi import gateway_inhibition

    record = gateway_inhibition.read(journey.state_root)
    if record is None or not record.readable or record.owner != install_txn.OWNER_UPDATER:
        return
    with self_update.install_lock(journey.install_root):
        try:
            held = install_txn.recover(journey.state_root, install_txn.OWNER_UPDATER)
        except install_txn.TransactionError as exc:
            raise self_update.UpdateError(str(exc), remedy=exc.remedy) from exc
        if held is None:
            return
        _write(out, f"Finishing an earlier update that stopped in its {held.phase} step.")
        restored = self_update.tidy(journey.install_root)
        if restored:
            _write(out, f"Put back {', '.join(restored)}, which it had moved aside.")
        # Links it changed only halfway go back to the pair it recorded before
        # changing them (an unreadable record raises: the inhibition stays).
        switched = self_update.finish_switch(journey.install_root)
        if switched:
            _write(out, switched[:1].upper() + switched[1:] + ".")
        self_update.read_installation(journey.install_root)
        try:
            held.end()
        except install_txn.TransactionError as exc:
            raise self_update.UpdateError(str(exc), remedy=exc.remedy) from exc
        _write(out, "The earlier update is finished; gateway changes are no longer paused.")


def _write(out: TextIO, line: str) -> None:
    out.write(termtext.visible_text(line) + "\n")


def _refuse(out: TextIO, exc: errors.ClaudeMultiError) -> int:
    _write(out, f"claude-multi update: {exc}")
    if exc.remedy:
        _write(out, f"  fix: {exc.remedy}")
    return EXIT_REFUSED


def _interrupted(out: TextIO, exc: self_update.Interrupted) -> int:
    _refuse(out, exc)
    return EXIT_CANCELLED if exc.cancelled else EXIT_REFUSED


def _confirm(ask: Ask, question: str, assume_yes: bool, out: TextIO) -> int | None:
    """None to proceed, or the exit code that ends the run."""

    if assume_yes:
        return None
    answer = ask(question)
    if answer is None:
        _write(out, "Nothing was changed: confirm on a terminal, or pass --yes.")
        return EXIT_REFUSED
    if not answer:
        _write(out, "Nothing was changed.")
        return EXIT_DECLINED
    return None


def run(journey: Journey, *, mode: str = "update", assume_yes: bool = False, ask: Ask, out: TextIO,
        from_dir: str | None = None, base_url: str | None = None) -> int:
    """One update, check or rollback (``mode``); returns the exit code."""

    if mode not in ("update", "check", "rollback"):
        raise ValueError(mode)
    if journey.channel != "bundle":
        for line in channel_lines(journey.channel):
            _write(out, line)
        return EXIT_REFUSED
    if mode != "check" and (journey.environ.get("CLAUDE_MULTI_MANAGED_ID") or "CLAUDECODE" in journey.environ):
        _write(out, "claude-multi update: changing the installation is refused inside a Claude Code session; "
                    "nothing was changed.")
        _write(out, "  fix: run claude-multi update in your own terminal")
        return EXIT_REFUSED
    try:
        installation = installed_release(journey)
        if mode != "check":
            _recover(journey, out)
            installation = installed_release(journey)
        if mode == "rollback":
            return _rollback(journey, installation, assume_yes=assume_yes, ask=ask, out=out)
        verifier = _verifier(journey)
        transport, where = _transport(journey, installation, from_dir=from_dir, base_url=base_url)
        _write(out, f"Checking {where} for a newer release (MANIFEST.json and its signed checksums; "
                    "nothing is installed yet).")
        result = self_update.check(installation, transport, verifier)
    except KeyboardInterrupt:
        _write(out, "Cancelled; nothing was changed.")
        return EXIT_CANCELLED
    except (self_update.UpdateError, errors.ClaudeMultiError) as exc:
        return _refuse(out, exc)
    current, release = installation.current, result.release
    if result.status == "current":
        _write(out, f"claude-multi {current.version} is up to date (released {current.release_date}).")
        return EXIT_OK
    if result.status == "older":
        _write(out, f"The installed claude-multi {current.version} is newer than the latest release "
                    f"{release.version}; nothing to do.")
        return EXIT_OK
    _write(out, f"claude-multi {release.version} is available (released {release.release_date}); "
                f"{current.version} is installed.")
    if mode == "check":
        _write(out, "Run 'claude-multi update' to see what it changes and install it.")
        return EXIT_OK
    return _update(journey, installation, release, transport, assume_yes=assume_yes, ask=ask, out=out)


def _update(journey: Journey, installation: self_update.Installation, release: self_update.Release,
            transport: self_update.Transport, *, assume_yes: bool, ask: Ask, out: TextIO) -> int:
    try:
        planned = self_update.plan(installation, release, state=self_update.state_version(journey.state_root),
                                   platform=journey.platform)
    except self_update.UpdateError as exc:
        return _refuse(out, exc)
    _write(out, f"Update plan: {planned.from_version} -> {planned.to_version}")
    for step in planned.steps:
        _write(out, f"  - {step}")
    if planned.rollback_blocked:
        _write(out, f"  ! after this update, 'claude-multi update --rollback' to {planned.from_version} is refused "
                    "once the new version has upgraded your state")
    try:
        stop = _confirm(ask, f"Update claude-multi to {planned.to_version} now?", assume_yes, out)
        if stop is not None:
            return stop
        applied = self_update.apply(
            installation, release, planned, transport, state_root=journey.state_root, hold=journey.hold,
            protected=journey.protected,
            inhibit=lambda: _transaction(journey, f"update claude-multi {planned.from_version} to {planned.to_version}"),
            prune_copies=journey.prune_copies, acquire_pin=journey.acquire_pin)
    except KeyboardInterrupt:
        _write(out, "Cancelled; nothing was changed.")
        return EXIT_CANCELLED
    except self_update.Interrupted as exc:
        return _interrupted(out, exc)
    except (self_update.UpdateError, errors.ClaudeMultiError) as exc:
        return _refuse(out, exc)
    _write(out, f"Updated claude-multi to {applied.version}; {applied.previous} is kept for "
                "'claude-multi update --rollback'.")
    for line in applied.copies:
        _write(out, line)
    if applied.kept:
        reason = ("whether a running gateway uses them cannot be checked" if applied.kept_unknown
                  else "the running gateway executes them")
        _write(out, f"Kept {', '.join(applied.kept)}: {reason}.")
    if applied.restart == self_update.RESTART_GATEWAY:
        _write(out, "The gateway binary changed: a running gateway keeps the old one until it restarts "
                    "(claude-multi gateway restart, between turns).")
    else:
        _write(out, "Only the launcher changed: new sessions use it; the running gateway keeps running.")
    return EXIT_OK


def _rollback(journey: Journey, installation: self_update.Installation, *, assume_yes: bool, ask: Ask,
              out: TextIO) -> int:
    previous = installation.previous
    if previous is None:
        _write(out, "claude-multi update: there is no previous version to roll back to; nothing was changed.")
        return EXIT_REFUSED
    current = installation.current
    _write(out, f"Rollback: {current.version} -> {previous.version} ({current.version} stays installed as the "
                "previous version, so a second rollback returns to it).")
    try:
        stop = _confirm(ask, f"Roll back claude-multi to {previous.version} now?", assume_yes, out)
        if stop is not None:
            return stop
        result = self_update.rollback(
            journey.install_root, state_root=journey.state_root, hold=journey.hold,
            inhibit=lambda: _transaction(journey, f"roll back claude-multi to {previous.version}"),
            confirmed=installation)
    except KeyboardInterrupt:
        _write(out, "Cancelled; nothing was changed.")
        return EXIT_CANCELLED
    except self_update.Interrupted as exc:
        return _interrupted(out, exc)
    except (self_update.UpdateError, errors.ClaudeMultiError) as exc:
        return _refuse(out, exc)
    _write(out, f"Rolled back to claude-multi {result.version}; {result.previous} is now the previous version.")
    if result.restart == self_update.RESTART_GATEWAY:
        _write(out, "The gateway binary changed: a running gateway keeps the code it started with until it "
                    "restarts (claude-multi gateway restart, between turns).")
    return EXIT_OK


# ------------------------------------------------------------ real seams


def bundle_contract(bundle: Path) -> Mapping[str, Any]:
    """The incoming bundle's packaged native contract."""

    for site in sorted(bundle.glob("lib/python3*/site-packages")):
        path = site / "claude_multi" / "data" / "catalog" / "native-contract.json"
        if path.is_file():
            try:
                return strict_json.load(path)
            except (OSError, strict_json.StrictJSONError) as exc:
                raise self_update.UpdateError(f"the new release's Claude Code contract is unreadable ({exc})") from exc
    raise self_update.UpdateError("the new release carries no Claude Code contract; nothing was changed")


def pin_acquirer(environ: Mapping[str, str], *, platform: str | None = None,
                 opener: Callable[..., Any] | None = None) -> self_update.AcquirePin:
    """Put the incoming release's Claude Code in place (consent was given at
    the plan's confirmation), recorded for the incoming launcher version."""

    from claude_multi import acquire, pin

    def run(bundle: Path, release: self_update.Release) -> None:
        contract = bundle_contract(bundle)
        try:
            version = pin.version(contract)
        except (KeyError, TypeError, ValueError, errors.ClaudeMultiError) as exc:
            raise self_update.UpdateError(f"the new release's Claude Code contract is invalid ({exc})") from exc
        if version != release.claude_code:
            raise self_update.UpdateError(f"the new release pins Claude Code {version}, but its manifest says "
                                          f"{release.claude_code}; nothing was changed")
        chosen = platform or pin.host_platform()
        listed = (release.claude_platforms or {}).get(chosen)
        record = pin.platform_record(contract, chosen)
        if listed is not None and record is not None and (
                listed.get("sha256") != record.get("sha256") or listed.get("size") != record.get("size")):
            raise self_update.UpdateError("the new release's Claude Code record differs from its signed manifest; "
                                          "nothing was changed")
        kwargs: dict[str, Any] = {"platform": chosen, "consent": lambda _plan: True,
                                  "launcher_version": release.version}
        if opener is not None:
            kwargs["opener"] = opener
        try:
            outcome = acquire.acquire(contract, environ, **kwargs)
        except acquire.AcquireError as exc:
            raise self_update.UpdateError(f"Claude Code {version} could not be put in place: {exc}; "
                                          "nothing was changed", remedy=exc.remedy) from exc
        if outcome.state == "declined":
            raise self_update.UpdateError(f"Claude Code {version} is not in place; nothing was changed",
                                          remedy=" ".join(outcome.notes) or None)

    return run


def copies_pruner(environ: Mapping[str, str]) -> self_update.PruneCopies:
    """The updater's cleanup of Claude Code copies for the release now
    current (its packaged contract): the use-lock prune, a copy in use kept
    and reported."""

    def run(bundle: Path, release: self_update.Release) -> list[str]:
        try:
            contract = bundle_contract(bundle)
        except self_update.UpdateError as exc:
            return [f"Claude Code copies were not cleaned up ({exc})."]
        return install_txn.prune_copies(environ, launcher_version=release.version, contract=contract)

    return run


def journey_for(runtime: Any) -> Journey:
    """The journey of a :class:`claude_multi.cli.runtime.Runtime`."""

    from claude_multi import endpoint, pin

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    state_root = runtime.session_store.root
    install_root = installs.bundle_root(environ)

    def hold() -> str | None:
        try:
            gateway = runtime.gateway()
        except (OSError, errors.ClaudeMultiError) as exc:
            return f"the gateway cannot be examined ({exc})"
        return install_txn.hold_reason(state_root, environ, gateway=gateway)

    try:
        platform = pin.host_platform()
    except (errors.ClaudeMultiError, KeyError, ValueError):
        platform = None
    return Journey(
        environ=environ, state_root=state_root, install_root=install_root, channel=endpoint.channel(environ),
        running=installs.install_root(environ), hold=hold,
        protected=lambda: install_txn.protected_versions(install_root, state_root),
        prune_copies=copies_pruner(environ),
        acquire_pin=pin_acquirer(environ, platform=platform), platform=platform)


def terminal_ask(input_stream: TextIO, output_stream: TextIO, *, interactive: bool) -> Ask:
    """y/N on a terminal; None without one (the caller then needs --yes)."""

    def ask(question: str) -> bool | None:
        if not interactive:
            return None
        output_stream.write(f"{question} [y/N] ")
        output_stream.flush()
        answer = input_stream.readline()
        if not answer:
            return False
        return answer.strip().lower() in ("y", "yes")

    return ask


def doctor_lines(runtime: Any, *, today: datetime.date | None = None) -> list[str]:
    """Doctor's offline release facts (no network): the installed release
    and its age, and the certificate trust of the launcher's own requests."""

    from claude_multi import endpoint

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    lines = [f"HTTPS trust (update, Claude Code download): {tls.source(environ)[2]}"]
    if endpoint.channel(environ) != "bundle":
        return lines
    try:
        installation = self_update.read_installation(installs.bundle_root(environ))
    except self_update.UpdateError as exc:
        return [*lines, f"Installed release: unreadable ({exc})"]
    if installation is None:
        return lines
    moment = today or datetime.datetime.now(datetime.timezone.utc).date()
    current = installation.current
    text = age_text(current.version, current.release_date, moment)
    previous = f"; previous {installation.previous.version}" if installation.previous is not None else ""
    lines.append(f"Installed release: {text}{previous} — check for a newer one: claude-multi update --check")
    return lines


def card_hint(runtime: Any, *, today: datetime.date | None = None) -> tuple[str, str] | None:
    """The card's update row ``(version, note)``, or None (no U on this channel)."""

    from claude_multi import __version__, endpoint

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    channel = endpoint.channel(environ)
    if channel == "nix":
        return (__version__, "updated through your Nix flake")
    if channel != "bundle":
        return None
    try:
        installation = self_update.read_installation(installs.bundle_root(environ))
    except self_update.UpdateError:
        installation = None
    if installation is None:
        return (__version__, "installed release unreadable — sh install.sh --repair")
    moment = today or datetime.datetime.now(datetime.timezone.utc).date()
    days = release_age(installation.current.release_date, moment)
    if days is not None and days >= AGE_NOTICE_DAYS:
        return (installation.current.version, f"this release is {days} days old — claude-multi update --check")
    return (installation.current.version, f"released {installation.current.release_date}")
