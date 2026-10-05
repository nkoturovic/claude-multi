"""``claude-multi uninstall``: the one removal path (the installer's
``--uninstall`` runs it too).

The plan classifies every file under the product's roots before anything is
removed: the program (the installer's launchers and PATH lines, only as its
receipt proves them, and the release root), the Claude Code copies, the
session state, your setup, the credentials (asked for separately, with a
typed phrase) and what is never removed (kept backups, the previous release
kept for rollback, a key file outside the product's folders, everything of
Claude Code's own). Files are removed one by one and a folder only once it
is empty, so a folder holding a kept file is never removed.

Each root is checked before it is listed: a root that is a link, or not a
folder, is left as it is with everything it points to, and the release root
must lie inside the home folder and outside the Nix store. Removal opens
every folder again from its checked root without following a link, so a
folder replaced by a link after the plan keeps what it points to.

Credentials win over every other class: the key files (the environment's,
the one the supervised gateway reads and the default, by path and by file
identity), the gateway's keys and configuration and the account sign-ins.
When the key-file pointer cannot be read, which file holds the keys is not
known, and nothing is planned.

The installer's receipt (``~/.local/share/claude-multi/install/installer.json``)
authorizes a launcher's removal only with the SHA-256 it recorded (and the
file still matching it, checked again right before it is removed), and a
PATH edit only as the exact line it recorded, present exactly once. A
missing, malformed or older receipt without those proofs authorizes
nothing: the launchers and shell files are kept and named.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claude_multi import errors, paths, secret_store, sessions, strict_json
from claude_multi.platform import posix_fs
from claude_multi.setup import model, texts

CLASSES = ("program", "claude", "sessions", "setup", "credentials", "never")
CREDENTIAL_FILES = frozenset({"api-key", "previous-key", "config.yaml"})
SHIM_NAMES = ("claude-multi-hook", "claude-multi-hook-3", "claude-multi-gateway-token")
POINTER_FILE = "secret-file.json"
LOCK_SUFFIX = ".lock"
# How long uninstall waits for a launch or another writer of the session
# state, or of a store it removes, to finish before it refuses (seconds).
LOCK_WAIT = 3.0
_LOCK_POLL = 0.05
_SHA = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ReceiptError(model.SetupError):
    """The installer receipt is unreadable or invalid (it authorizes nothing)."""


class UninstallRefused(model.SetupError):
    """The plan cannot be made safely; nothing was removed."""


# ------------------------------------------------------------------ the receipt


@dataclass(frozen=True)
class Launcher:
    path: Path
    sha256: str  # the content the installer wrote (removal only while the file still has it)


@dataclass(frozen=True)
class PathEdit:
    file: Path
    line: str  # the exact line the installer appended (removal deletes that line only)


@dataclass(frozen=True)
class Receipt:
    format: int
    launchers: tuple[Launcher, ...]
    path_edits: tuple[PathEdit, ...]


def _home_path(text: Any, environ: Mapping[str, str]) -> Path:
    if not isinstance(text, str) or not text or "\0" in text:
        raise ReceiptError("a path in the receipt is not a text")
    path = secret_store.expand(text, environ)
    if not path.is_absolute():
        raise ReceiptError(f"{text}: not an absolute path")
    home = Path(os.path.realpath(paths.home(environ)))
    if home not in Path(os.path.realpath(path)).parents:
        raise ReceiptError(f"{text}: not under your home folder")
    return path


def parse_receipt(document: Any, environ: Mapping[str, str]) -> Receipt:
    """The receipt's launchers and PATH lines, read with the installer's own
    schema (:func:`claude_multi.install_receipt.parse`: strict, launchers
    with their sha256, lines exactly as written); every path must also lie
    under your home folder. Anything else raises :class:`ReceiptError`."""

    from claude_multi import install_receipt

    try:
        receipt = install_receipt.parse(document)
    except install_receipt.ReceiptError as exc:
        raise ReceiptError(str(exc)) from exc
    launchers = tuple(Launcher(_home_path(item.path, environ), item.sha256) for item in receipt.launchers)
    edits = tuple(PathEdit(_home_path(item.file, environ), item.line) for item in receipt.path_lines)
    return Receipt(install_receipt.FORMAT, launchers, edits)


def read_receipt(environ: Mapping[str, str]) -> Receipt | None:
    """The installer's receipt, or None when there is none; a receipt that
    exists but cannot be used raises :class:`ReceiptError`."""

    from claude_multi import install_receipt

    target = paths.installer_receipt(environ)
    if not os.path.lexists(target):
        return None
    try:
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ReceiptError("the installer receipt is not a regular file you own")
        if info.st_size > install_receipt.MAX_BYTES:
            raise ReceiptError("the installer receipt is larger than a receipt can be")
        document = strict_json.loads(target.read_bytes())
    except OSError as exc:
        raise ReceiptError(f"the installer receipt cannot be read ({exc.strerror})") from exc
    except ValueError as exc:
        raise ReceiptError("the installer receipt is not valid JSON") from exc
    return parse_receipt(document, environ)


def file_sha256(path: Path) -> str | None:
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------ roots


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class Root:
    """A folder files are removed through: checked when the plan is made and
    opened again at removal, where it must still be the same folder."""

    path: Path
    identity: tuple[int, int]
    follow: bool = False  # the folder itself may be reached through a link (a launcher's folder)


def check_root(path: Path, environ: Mapping[str, str], kept: list[str]) -> Root | None:
    """``path`` as a root, or None (absent; or a link, not a folder or
    unreadable — then named in ``kept`` and left as it is)."""

    shown = paths.display(path, environ)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        kept.append(texts.UNINSTALL_ROOT_UNREADABLE.format(path=shown, reason=exc.strerror or type(exc).__name__))
        return None
    if stat.S_ISLNK(info.st_mode):
        kept.append(texts.UNINSTALL_ROOT_LINK.format(path=shown))
        return None
    if not stat.S_ISDIR(info.st_mode):
        kept.append(texts.UNINSTALL_ROOT_NOT_FOLDER.format(path=shown))
        return None
    return Root(path, _identity(info))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def release_problem(install_root: Path, environ: Mapping[str, str]) -> str | None:
    """Why the release root is not removed, or None: it must resolve inside
    the home folder and outside the Nix store."""

    from claude_multi.setup import external

    real = os.path.realpath(install_root)
    home = os.path.realpath(paths.home(environ))
    if external.is_store_path(install_root) or real == home or not _inside(real, home):
        return texts.UNINSTALL_RELEASE_OUTSIDE.format(path=paths.display(install_root, environ))
    return None


def retained_release(install_root: Path) -> Path | None:
    """The release the installer keeps for rollback (``previous`` links to
    ``versions/<version>``), or None."""

    try:
        target = os.readlink(install_root / "previous")
    except OSError:
        return None
    versions = install_root / "versions"
    resolved = Path(os.path.normpath(install_root / target))
    if resolved.parent != versions or resolved.name in ("", ".", ".."):
        return None
    return resolved


# ------------------------------------------------------------------ classification


def protected(path: Path, environ: Mapping[str, str]) -> bool:
    """Kept backups and rollback material: never removed by uninstall."""

    name = path.name
    if ".signed-out." in name or name.startswith(("auth.pre-", "auth.replaced.")):
        return True
    if name == "pinned-clients" or "pinned-clients" in path.parts:
        return True
    if path.parent.name == "profiles" and name.startswith(".") and (".pre-reseed-" in name or ".removed-" in name):
        return True
    if path.parent.name == "sessions" and name.endswith(".v3.json"):
        return True
    return False


def _protected_under(path: Path, root: Path, environ: Mapping[str, str]) -> bool:
    relative = path.relative_to(root)
    return any(protected(root / Path(*relative.parts[: index + 1]), environ) for index in range(len(relative.parts)))


@dataclass(frozen=True)
class KeyFiles:
    """The key files (:func:`secret_store.key_file_inventory`) by path, by
    resolved path and by file identity, so another name for one of them
    (a link, a hard link) is a credential too."""

    files: tuple[Path, ...]
    names: frozenset[str]
    identities: frozenset[tuple[int, int]]

    @classmethod
    def of(cls, files: Iterable[Path]) -> "KeyFiles":
        found = tuple(files)
        names: set[str] = set()
        identities: set[tuple[int, int]] = set()
        for file in found:
            names.add(os.path.normpath(os.path.abspath(file)))
            names.add(os.path.realpath(file))
            for probe in (os.stat, os.lstat):
                try:
                    identities.add(_identity(probe(file)))
                except OSError:
                    pass
        return cls(found, frozenset(names), frozenset(identities))

    def holds(self, path: Path) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if _identity(info) in self.identities or os.path.normpath(os.path.abspath(path)) in self.names:
            return True
        return stat.S_ISLNK(info.st_mode) and os.path.realpath(path) in self.names


@dataclass(frozen=True)
class Entry:
    cls: str
    path: Path
    note: str = ""
    root: Root | None = None  # the checked folder the file is removed through
    sha256: str | None = None  # a launcher: the content the receipt proves
    identity: tuple[int, int] | None = None  # a launcher: the file that was checked
    shown: Path | None = None  # what the lists name instead of the file (its kept folder)
    label: str = "backups"  # the "Never removed" line it is listed on


@dataclass
class UninstallPlan:
    channel: str
    program: list[Entry] = field(default_factory=list)
    path_edits: list[PathEdit] = field(default_factory=list)
    claude: list[Entry] = field(default_factory=list)
    sessions: list[Entry] = field(default_factory=list)
    setup: list[Entry] = field(default_factory=list)
    credentials: list[Entry] = field(default_factory=list)
    never: list[Entry] = field(default_factory=list)
    kept_program: list[str] = field(default_factory=list)  # why a launcher or PATH line stays
    kept: list[str] = field(default_factory=list)  # roots left as they are, and why
    key_names: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    record_count: int = 0
    profile_count: int = 0
    own_providers: int = 0
    keep_setup: bool = False
    data_root: Root | None = None
    install_root: Root | None = None
    claude_root: Root | None = None
    state_root: Root | None = None
    config_roots: tuple[Root, ...] = ()

    def removable(self, *, credentials: bool) -> list[Entry]:
        found = [*self.program, *self.claude, *self.sessions, *self.setup]
        return found + (list(self.credentials) if credentials else [])

    def classes(self) -> dict[Path, str]:
        """The class the plan gave each file it lists."""

        return {entry.path: entry.cls for group in (self.program, self.claude, self.sessions, self.setup,
                                                    self.credentials, self.never) for entry in group}

    def roots(self) -> list[Root]:
        """Every root, innermost first (the order emptied folders go)."""

        found = [self.install_root, self.claude_root, self.state_root, self.data_root, *self.config_roots]
        return [root for root in found if root is not None]


def _walk(root: Path) -> Iterable[Path]:
    """Every entry under ``root`` (links are entries, never followed)."""

    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        path = root / name
        yield path
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            yield from _walk(path)


def _files(root: Path) -> list[Path]:
    found = []
    for path in _walk(root):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            found.append(path)
    return found


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _launcher_entry(launcher: Launcher) -> Entry | None:
    folder = launcher.path.parent
    try:
        folder_info = os.stat(folder)
        info = os.lstat(launcher.path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return Entry("program", launcher.path, "launcher", root=Root(folder, _identity(folder_info), follow=True),
                 sha256=launcher.sha256, identity=_identity(info))


def build_plan(runtime: Any, *, keep_setup: bool = False) -> UninstallPlan:
    """Classify everything under the product's roots (reads only); raises
    :class:`UninstallRefused` when the key files cannot be known."""

    from claude_multi.setup import external, signin

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    channel = external.channel(runtime)
    plan = UninstallPlan(channel, keep_setup=keep_setup)
    try:
        keys = KeyFiles.of(secret_store.key_file_inventory(environ))
    except secret_store.SecretStoreError as exc:
        raise UninstallRefused(texts.UNINSTALL_KEY_FILE_UNKNOWN.format(problem=str(exc)), remedy=exc.remedy) from exc
    data = paths.data_root(environ)
    # The account sign-ins: the credential inventory's folder under the data root.
    accounts = Path(paths.home(environ)) / secret_store.ACCOUNT_RECORDS
    state_root = Path(runtime.session_store.root)
    config_roots = list(dict.fromkeys([paths.gateway_config_dir(environ), paths.config_root(dict(environ))]))
    product_roots = [data, state_root, *config_roots]
    install_root = data / "install"
    owned = external.owned_claude_dir(environ)
    plan.data_root = check_root(data, environ, plan.kept)
    if plan.data_root is not None:
        plan.install_root = check_root(install_root, environ, plan.kept)
        plan.claude_root = check_root(owned, environ, plan.kept)
    plan.state_root = check_root(state_root, environ, plan.kept)
    plan.config_roots = tuple(root for root in (check_root(path, environ, plan.kept) for path in config_roots)
                              if root is not None)
    seen: set[Path] = set()

    def place(path: Path, root: Root, cls: str, note: str = "") -> None:
        """File ``path`` as ``cls``, unless it is a kept backup or a credential."""

        seen.add(path)
        if _protected_under(path, root.path, environ):
            plan.never.append(Entry("never", path, root=root))
        elif keys.holds(path):
            plan.credentials.append(Entry("credentials", path, "the API-key file", root=root))
        else:
            getattr(plan, cls).append(Entry(cls, path, note, root=root))

    # The program: the installer's launchers and PATH lines, and its release root.
    if channel == "bundle" or os.path.lexists(install_root):
        try:
            receipt = read_receipt(environ)
        except ReceiptError as exc:
            receipt = None
            plan.kept_program.append(f"{exc} — launchers and PATH lines are left as they are")
        if receipt is None and not plan.kept_program:
            plan.kept_program.append(texts.UNINSTALL_NO_RECEIPT)
        for launcher in (receipt.launchers if receipt is not None else ()):
            shown = paths.display(launcher.path, environ)
            if not os.path.lexists(launcher.path):
                continue
            entry = _launcher_entry(launcher)
            if entry is None or file_sha256(launcher.path) != launcher.sha256:
                plan.kept_program.append(texts.UNINSTALL_CHANGED_WRAPPER.format(path=shown))
            else:
                plan.program.append(entry)
        for edit in (receipt.path_edits if receipt is not None else ()):
            shown = paths.display(edit.file, environ)
            if _line_count(edit.file, edit.line) != 1:
                if os.path.lexists(edit.file):
                    plan.kept_program.append(texts.UNINSTALL_PATH_AMBIGUOUS.format(file=shown))
                continue
            plan.path_edits.append(edit)
        release = plan.install_root
        problem = release_problem(install_root, environ) if release is not None else None
        if problem is not None:
            plan.kept.append(problem)
            plan.install_root = release = None
        if release is not None:
            retained = retained_release(install_root)
            for path in _files(install_root):
                if path == install_root / "previous" or (retained is not None and _under(path, retained)):
                    seen.add(path)
                    plan.never.append(Entry("never", path, texts.UNINSTALL_RETAINED, root=release,
                                            shown=retained or path, label="previous release"))
                else:
                    place(path, release, "program")
    elif channel == "nix":
        plan.kept_program.append(texts.UNINSTALL_NIX)
    # The Claude Code copies.
    if plan.claude_root is not None:
        for path in _files(owned):
            place(path, plan.claude_root, "claude")
    # The data root's other entries.
    if plan.data_root is not None:
        for path in _files(data):
            if _under(path, install_root) or _under(path, owned) or path in seen:
                continue
            relative = path.relative_to(data)
            if accounts in path.parents and not _protected_under(path, data, environ):
                seen.add(path)
                plan.credentials.append(Entry("credentials", path, root=plan.data_root))
            elif relative.parts[0] == "nix" and channel == "nix":
                seen.add(path)
                plan.never.append(Entry("never", path, "the Nix service link", root=plan.data_root))
            elif relative.parts[0] == "nix":
                place(path, plan.data_root, "program")
            else:
                place(path, plan.data_root, "sessions")
    # The state root.
    if plan.state_root is not None:
        for path in _files(state_root):
            if path in seen:
                continue
            if keep_setup and not _protected_under(path, state_root, environ) and not keys.holds(path):
                seen.add(path)
                continue
            place(path, plan.state_root, "sessions")
    try:
        plan.record_count = sum(1 for name in os.listdir(state_root / "sessions")
                                if name.endswith(".json") and not name.endswith(".v3.json"))
    except OSError:
        plan.record_count = 0
    # Your setup and the credentials in the config folders.
    for root in plan.config_roots:
        for path in _files(root.path):
            if path in seen:
                continue
            relative = path.relative_to(root.path)
            if _protected_under(path, root.path, environ) or keys.holds(path):
                place(path, root, "setup")
            elif path.name in CREDENTIAL_FILES or path.name.startswith("management-key") \
                    or relative.parts[0] == "secrets" or relative == Path(POINTER_FILE):
                seen.add(path)
                plan.credentials.append(Entry("credentials", path, root=root))
            elif keep_setup:
                seen.add(path)
            else:
                place(path, root, "setup")
                if relative.parts[0] == "profiles" and path.suffix == ".json" and not path.name.startswith("."):
                    plan.profile_count += 1
                if relative.parts[0] == "providers.d" and path.suffix == ".json":
                    plan.own_providers += 1
    # A key file outside the product's folders stays where it is.
    real_roots = [os.path.realpath(root) for root in product_roots]
    for file in keys.files:
        if not os.path.lexists(file):
            continue
        given = Path(os.path.normpath(os.path.abspath(file)))
        real = os.path.realpath(file)
        if any(_inside(real, root) for root in real_roots) or any(entry.path == given for entry in plan.never):
            continue
        plan.never.append(Entry("never", given, "your key file (it stays where it is)", label="key file"))
    names: set[str] = set()
    for file in keys.files:
        try:
            if os.path.lexists(file):
                names.update(secret_store.read_env_file(file))
        except secret_store.SecretStoreError:
            continue
    plan.key_names = tuple(sorted(names))
    accounts: list[str] = []
    for provider_id, pool in signin.ACCOUNT_POOLS.items():
        try:
            found = signin.accounts(runtime, provider_id)
        except errors.ClaudeMultiError:
            found = ()
        accounts.extend(f"{texts.ACCOUNT_KINDS[pool]} ({name})" for name in found)
    plan.accounts = tuple(accounts)
    return plan


def _line_count(file: Path, line: str) -> int:
    try:
        info = os.lstat(file)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return 0
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for item in text.split("\n") if item == line)


def _size(entries: Iterable[Entry]) -> int:
    total = 0
    for entry in entries:
        try:
            total += os.lstat(entry.path).st_size
        except OSError:
            pass
    return total


def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):.0f} MB"


def plan_lines(runtime: Any, plan: UninstallPlan) -> list[str]:
    environ = runtime.environ
    show = lambda path: paths.display(path, environ)  # noqa: E731
    lines = [texts.UNINSTALL_PLAN_HEAD, "Stops and removes"]
    lines.append(f"  {'gateway':<17} the running gateway (stopped safely) and its service")
    program = [entry for entry in plan.program if entry.note == "launcher"]
    parts: list[str] = []
    install_root = paths.data_root(environ) / "install"
    if any(_under(entry.path, install_root) for entry in plan.program):
        parts.append(show(install_root))
    if program:
        parts.append(show(program[0].path) + (f" (+{len(program) - 1})" if len(program) > 1 else ""))
    if plan.path_edits:
        parts.append("PATH lines in " + ", ".join(show(edit.file) for edit in plan.path_edits))
    if parts:
        lines.append(f"  {'program':<17} " + ", ".join(parts))
    for note in plan.kept_program:
        lines.append(f"  {'program':<17} kept: {note}")
    if plan.claude:
        lines.append(f"  {'Claude Code copy':<17} {show(paths.data_root(environ) / 'claude')} "
                     f"({_mb(_size(plan.claude))})")
    if plan.sessions:
        lines.append(f"  {'sessions':<17} {show(runtime.session_store.root)} ({plan.record_count} session records; "
                     "your conversations stay in ~/.claude)")
    if plan.setup:
        lines.append(f"  {'your setup':<17} {show(paths.config_root(dict(environ)))}: {plan.profile_count} "
                     f"profiles, settings, {plan.own_providers} providers you added")
    if plan.keep_setup:
        lines.append(f"  {'kept':<17} your setup and session state (--keep-setup)")
    if plan.credentials:
        lines.append("Asks separately (typed confirmation)")
        what = []
        if plan.key_names:
            what.append("API keys: " + ", ".join(plan.key_names))
        if plan.accounts:
            what.append("sign-ins: " + ", ".join(plan.accounts))
        if any(entry.path.name in ("api-key", "previous-key") for entry in plan.credentials):
            what.append("the local gateway key")
        lines.append(f"  {'credentials':<17} " + ("; ".join(what) or f"{len(plan.credentials)} file(s)"))
    lines.append("Never removed")
    labels: dict[str, set[str]] = {}
    for entry in plan.never:
        labels.setdefault(entry.label, set()).add(show(_top(entry, runtime)))
    for label in sorted(labels, key=lambda item: (item != "backups", item)):
        shown = sorted(labels[label])
        lines.append(f"  {label:<17} " + ", ".join(shown[:3]) + (", …" if len(shown) > 3 else ""))
    for note in plan.kept:
        lines.append(f"  {'kept':<17} {note}")
    lines.append(f"  {'Claude Code':<17} ~/.claude, ~/.claude.json, your conversations, your own claude install")
    return lines


def _top(entry: Entry, runtime: Any) -> Path:
    """The kept entry to name: the backup folder rather than each file in it."""

    if entry.shown is not None:
        return entry.shown
    for parent in [entry.path, *entry.path.parents]:
        if protected(parent, runtime.environ):
            return parent
    return entry.path


# ------------------------------------------------------------------ sessions


@dataclass(frozen=True)
class SessionCheck:
    """Which sessions may still run: ``live`` (a record without a recorded
    end, or one a background or running Claude Code process names by its
    id or an alias) and ``unknown`` (why that cannot be told)."""

    live: tuple[str, ...]
    unknown: tuple[str, ...]

    def names(self) -> list[str]:
        return [*(item[:8] for item in self.live), *self.unknown]


def session_check(runtime: Any) -> SessionCheck:
    """The destructive-operation liveness check (read-only): background
    liveness with its uncertainty, the process table and every record's own
    state; whatever cannot be read counts as possibly running."""

    store = runtime.session_store
    unknown: list[str] = []
    live: list[str] = []
    daemon = runtime.background_liveness()
    if not daemon.known:
        unknown.append(f"(background sessions cannot be checked: {daemon.reason})")
    scan = sessions.proc_session_scan(runtime.proc_root)
    if not scan.known:
        unknown.append(f"(running sessions cannot be checked: {scan.reason})")
    directory = Path(store.root) / "sessions"
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = []
    except OSError as exc:
        names = []
        unknown.append(f"(the session records cannot be listed: {exc.strerror or type(exc).__name__})")
    for name in names:
        stem = name[: -len(".json")] if name.endswith(".json") else ""
        if not sessions.UUID4.fullmatch(stem):
            continue
        try:
            raw, _bytes = store.load_raw(stem)
            view = sessions._lifecycle_view(raw)
            ids = sessions.record_session_ids(view)
        except (errors.ClaudeMultiError, OSError, ValueError, KeyError, TypeError):
            unknown.append(f"{stem[:8]} (its record cannot be read)")
            continue
        running = bool(scan.ids & ids) or any(item.startswith(prefix) for item in ids for prefix in daemon.prefixes)
        if running or view.get("last_event_source") != "end":
            live.append(stem)
    return SessionCheck(tuple(live), tuple(unknown))


@dataclass(frozen=True)
class Held:
    """A lock file this run holds until removal ends: the file it locked
    (``identity``), the checked root it goes through, and whether this run
    made it (it did not exist before)."""

    path: Path
    root: Root | None
    identity: tuple[int, int] | None
    created: bool
    release: Callable[[], None] = field(repr=False, compare=False)


def _store_targets(runtime: Any, environ: Mapping[str, str]) -> list[Path]:
    """The lock targets of the stores in your setup: profiles, named
    bindings, choices, settings, preferences and the providers you added
    (each store's own definition)."""

    from claude_multi import choices, operator as operator_mod, profile as profile_mod, settings as settings_mod

    return [runtime.profiles.lock_target, profile_mod.bindings_path(environ), choices.path(environ),
            settings_mod.settings_path(environ), settings_mod.preferences_path(environ),
            operator_mod.ledger_path(environ)]


def _root_of(plan: UninstallPlan, path: Path) -> Root | None:
    """The innermost root of the plan that ``path`` lies under (None: none)."""

    found = [root for root in plan.roots() if not root.follow and _under(path, root.path) and path != root.path]
    return max(found, key=lambda root: len(root.path.parts), default=None)


def store_locks(runtime: Any, plan: UninstallPlan, *, credentials: bool) -> list[tuple[Path, Root]]:
    """Every lock file removal must hold, with the root it goes through:
    each lock file the plan removes, and — when your setup goes — the lock
    of each store in it, also before any writer made that file, so a writer
    that starts later waits instead of writing while its store goes."""

    found: dict[Path, Root] = {}
    for entry in plan.removable(credentials=credentials):
        if entry.root is not None and not entry.root.follow and entry.path.name.endswith(LOCK_SUFFIX):
            found.setdefault(entry.path, entry.root)
    if not plan.keep_setup:
        environ = {**runtime.environ, "HOME": str(runtime.home)}
        for target in _store_targets(runtime, environ):
            lock = target.with_name(target.name + LOCK_SUFFIX)
            root = _root_of(plan, lock)
            if root is not None:
                found.setdefault(lock, root)
    return sorted(found.items())


def _lock_file(root: Root, path: Path) -> Held | None:
    """Lock ``path`` exclusively without waiting (``BlockingIOError`` while
    someone else holds it), opened through its checked root without
    following a link; a missing file is made (0600). None when its folder
    is gone, or when it is not a regular file (not a lock anyone takes)."""

    try:
        folder = _open_folder(root, path.relative_to(root.path).parent.parts)
    except FileNotFoundError:
        return None
    try:
        try:
            info = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            return None
        created = info is None
        flags = os.O_RDONLY | os.O_CLOEXEC | _NOFOLLOW
        try:
            descriptor = os.open(path.name, flags | (os.O_CREAT | os.O_EXCL if created else 0), 0o600,
                                 dir_fd=folder)
        except FileExistsError:
            created = False
            descriptor = os.open(path.name, flags, dir_fd=folder)
        try:
            held = os.fstat(descriptor)
            if not stat.S_ISREG(held.st_mode):
                os.close(descriptor)
                return None
            posix_fs.lock_descriptor(descriptor, shared=False, blocking=False)
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(folder)

    def release() -> None:
        try:
            posix_fs.unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    return Held(path, root, _identity(held), created, release)


class Fence:
    """What keeps other claude-multi writers out while files go: the session
    state's migration lock, exclusively (no launch, live lineup change or
    record write runs meanwhile), the gateway's start lock (nothing starts
    it), and the lock of every store whose files go (:meth:`stores`), all
    held until removal ends. A lock file is removed last, still held, and
    only as the plan classified it (:meth:`remove_own`)."""

    def __init__(self, plan: UninstallPlan) -> None:
        self.plan = plan
        self.locks: list[Held] = []

    def _keep(self, lock: Any, root: Root | None, *, existed: bool, expected: Path | None = None) -> None:
        path = Path(lock.lock_path)
        created = not existed and (expected is None or path == expected)
        self.locks.append(Held(path, root, lock.identity(), created, lock.release))

    def sessions(self) -> str | None:
        """Take the migration lock (bounded wait); a refusal, or None."""

        plan = self.plan
        if plan.state_root is None:
            return None
        existed = os.path.lexists(sessions.migration_lock(plan.state_root.path).lock_path)
        try:
            lock = sessions.acquire_exclusive_bounded(plan.state_root.path, timeout=LOCK_WAIT)
        except sessions.MigrationBusyError:
            return texts.UNINSTALL_BUSY
        except (OSError, errors.ClaudeMultiError) as exc:
            return texts.UNINSTALL_STATE_LOCK.format(reason=getattr(exc, "strerror", None) or str(exc))
        self._keep(lock, plan.state_root, existed=existed)
        return None

    def gateway(self, runtime: Any, *, what: str = "nothing was removed") -> str | None:
        """Hold the gateway's start lock, with the gateway still stopped and
        no inhibition recorded; a refusal, or None."""

        from claude_multi import service
        from claude_multi.setup import external

        expected = service.start_lock_path(Path(runtime.session_store.root))
        existed = os.path.lexists(expected)
        lock, refusal = external.hold_gateway_starts(runtime, what=what)
        if lock is not None:
            path = Path(lock.lock_path)
            self._keep(lock, _root_of(self.plan, path), existed=existed, expected=expected)
        return refusal

    def stores(self, runtime: Any, *, credentials: bool, what: str = "nothing was removed") -> str | None:
        """Take the lock of every store whose files go (:func:`store_locks`;
        ``credentials``: the credentials may go too), waiting a short,
        bounded time for each; a store still busy refuses before any of its
        files goes (a refusal naming it, or None)."""

        held = self.held()
        deadline = time.monotonic() + LOCK_WAIT
        for path, root in store_locks(runtime, self.plan, credentials=credentials):
            if path in held:
                continue
            shown = paths.display(path, runtime.environ)
            while True:
                try:
                    taken = _lock_file(root, path)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return texts.UNINSTALL_STORE_BUSY.format(path=shown, rest=what)
                    time.sleep(_LOCK_POLL)
                    continue
                except _Changed:
                    return texts.UNINSTALL_STORE_MOVED.format(path=shown, rest=what)
                except OSError as exc:
                    return texts.UNINSTALL_STORE_LOCK.format(path=shown, reason=exc.strerror or type(exc).__name__,
                                                             rest=what)
                break
            if taken is not None:
                self.locks.append(taken)
        return None

    def held(self) -> frozenset[Path]:
        return frozenset(held.path for held in self.locks)

    def remove_own(self, result: "Result", environ: Mapping[str, str], *, credentials_removed: bool) -> None:
        """Remove the lock files this run holds, while still held, as the
        plan classified them: one this run made, or one the plan removes
        whose file is still the one held. A file the plan counts among your
        credentials goes only after the typed phrase, a never-removed one
        never; such a file stays and is named. A file the plan does not
        list (your kept setup, or one another program made since) stays."""

        classes = self.plan.classes()
        for held in self.locks:
            cls = classes.get(held.path)
            shown = paths.display(held.path, environ)
            if cls == "never" or (cls == "credentials" and not credentials_removed):
                result.kept.append(texts.UNINSTALL_KEPT_HELD.format(
                    path=shown, what="one of your credentials" if cls == "credentials" else "never removed"))
                continue
            if (cls is None and not held.created) or held.root is None or held.identity is None:
                continue
            _remove(Entry(cls or "sessions", held.path, root=held.root, identity=held.identity), result, environ,
                    own=True, report=cls is not None)

    def release(self) -> None:
        while self.locks:
            self.locks.pop().release()


# ------------------------------------------------------------------ removal


@dataclass
class Result:
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # held files the plan keeps (named in the remains)


class _Changed(Exception):
    """A folder on the way to a file is not the one the plan checked."""


def _open_folder(root: Root, parts: Iterable[str]) -> int:
    """The folder at ``root`` + ``parts``, opened without following a link
    below the root; the root must still be the folder the plan checked."""

    try:
        descriptor = os.open(root.path, _DIRECTORY | (0 if root.follow else _NOFOLLOW))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _Changed() from exc
        raise
    try:
        if _identity(os.fstat(descriptor)) != root.identity:
            raise _Changed()
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY | _NOFOLLOW, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _Changed() from exc
                raise
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _same_launcher(folder: int, name: str, info: os.stat_result, entry: Entry) -> bool:
    """The launcher is still the regular file the plan checked, with the
    content the receipt records."""

    if not stat.S_ISREG(info.st_mode) or _identity(info) != entry.identity:
        return False
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | _NOFOLLOW, dir_fd=folder)
    except OSError:
        return False
    try:
        if _identity(os.fstat(descriptor)) != entry.identity:
            return False
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest() == entry.sha256


def _remove(entry: Entry, result: Result, environ: Mapping[str, str], *, skip: frozenset[Path] = frozenset(),
            own: bool = False, report: bool = True) -> None:
    shown = paths.display(entry.path, environ)
    if entry.path in skip:
        return
    if entry.root is None:
        result.failed.append(texts.UNINSTALL_MOVED.format(path=shown))
        return
    try:
        folder = _open_folder(entry.root, entry.path.relative_to(entry.root.path).parent.parts)
    except FileNotFoundError:
        return
    except _Changed:
        result.failed.append(texts.UNINSTALL_MOVED.format(path=shown))
        return
    except OSError as exc:
        result.failed.append(f"{shown} ({exc.strerror})")
        return
    try:
        name = entry.path.name
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            return
        if entry.sha256 is not None and not _same_launcher(folder, name, info, entry):
            result.failed.append(texts.UNINSTALL_CHANGED_SINCE_PLAN.format(path=shown))
            return
        if entry.sha256 is None and entry.identity is not None and _identity(info) != entry.identity:
            result.failed.append(texts.UNINSTALL_CHANGED_SINCE_PLAN.format(path=shown))
            return
        if not own and name.endswith(LOCK_SUFFIX) and stat.S_ISREG(info.st_mode):
            # A lock file goes only while this run holds it (Fence): whether
            # someone takes it right after a check cannot be known.
            result.failed.append(texts.UNINSTALL_LOCK_BUSY.format(path=shown))
            return
        os.unlink(name, dir_fd=folder)
        if report:
            result.removed.append(shown)
    except FileNotFoundError:
        pass
    except OSError as exc:
        result.failed.append(f"{shown} ({exc.strerror})")
    finally:
        os.close(folder)


def remove_entries(entries: Iterable[Entry], result: Result, environ: Mapping[str, str], *,
                   skip: frozenset[Path] = frozenset()) -> None:
    """Remove each file through its checked root; a file in ``skip`` (a lock
    this run holds) is left for :meth:`Fence.remove_own`."""

    for entry in entries:
        _remove(entry, result, environ, skip=skip)


def _prune(descriptor: int) -> None:
    for name in sorted(os.listdir(descriptor)):
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            child = os.open(name, _DIRECTORY | _NOFOLLOW, dir_fd=descriptor)
        except OSError:
            continue
        try:
            _prune(child)
        except OSError:
            pass
        finally:
            os.close(child)
        try:
            os.rmdir(name, dir_fd=descriptor)
        except OSError:
            pass


def prune_empty(root: Root | None) -> None:
    """Remove empty folders under (and including) ``root``, deepest first;
    a folder holding anything stays, and no link is followed."""

    if root is None:
        return
    try:
        descriptor = _open_folder(root, ())
    except (_Changed, OSError):
        return
    try:
        _prune(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
    try:
        parent = os.open(root.path.parent, _DIRECTORY)
    except OSError:
        return
    try:
        info = os.stat(root.path.name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and _identity(info) == root.identity:
            os.rmdir(root.path.name, dir_fd=parent)
    except OSError:
        pass
    finally:
        os.close(parent)


def remove_path_edit(edit: PathEdit, result: Result, environ: Mapping[str, str]) -> None:
    """Drop the recorded line (present exactly once) and nothing else: every
    other line of the file is kept byte for byte."""

    shown = paths.display(edit.file, environ)
    if _line_count(edit.file, edit.line) != 1:
        result.failed.append(texts.UNINSTALL_PATH_AMBIGUOUS.format(file=shown))
        return
    text = edit.file.read_text(encoding="utf-8")
    items = text.split("\n")
    index = items.index(edit.line)
    kept = items[:index] + items[index + 1:]
    mode = stat.S_IMODE(os.lstat(edit.file).st_mode)
    temporary = edit.file.with_name(f".{edit.file.name}.claude-multi.{os.getpid()}")
    try:
        temporary.write_text("\n".join(kept), encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, edit.file)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        result.failed.append(f"{shown} ({exc.strerror})")
        return
    result.removed.append(f"PATH line in {shown}")


def remaining_lines(runtime: Any, plan: UninstallPlan, *, credentials_removed: bool) -> list[str]:
    environ = runtime.environ
    lines = []
    for entry in plan.never:
        lines.append(f"  {paths.display(_top(entry, runtime), environ)}"
                     + (f" ({entry.note})" if entry.label == "previous release" else ""))
    if plan.credentials and not credentials_removed:
        lines.append("  your credentials: " + ", ".join(sorted({paths.display(e.path.parent, environ)
                                                              for e in plan.credentials})))
    lines.extend(f"  {note}" for note in plan.kept_program)
    lines.extend(f"  {note}" for note in plan.kept)
    return sorted(set(lines))
