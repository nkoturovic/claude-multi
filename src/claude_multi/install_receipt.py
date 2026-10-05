"""The installer's receipt: ``<data root>/install/installer.json``.

``packaging/install.sh`` writes it; ``claude-multi uninstall`` reads it to
remove exactly what the installer created outside the install root. One
schema serves both sides, so the reader is strict and the receipt alone is
the authority for a deletion::

    {"format": 2,
     "launchers": [{"path": "/home/u/.local/bin/claude-multi", "sha256": "<64 hex>"}],
     "path_lines": [{"file": "/home/u/.bashrc",
                     "marker": "# added by the claude-multi installer",
                     "line": "export PATH=\\"$HOME/.local/bin:$PATH\\" # added by the claude-multi installer"}]}

- ``launchers``: every launcher file the installer wrote, with the sha256 of
  the bytes it wrote. A launcher may be removed only while it is still a
  regular file (never a link) with exactly that content: a launcher the user
  replaced or edited is theirs.
- ``path_lines``: every shell-profile line the installer appended. ``line``
  is the whole line as written (it ends with ``marker``); a removal deletes
  only lines equal to it, never a line that merely mentions the marker.

Paths are absolute. Anything else — a missing file, another format (the
hash-less format 1 of development builds included), an unknown key, a
relative path, a malformed digest, a line without its marker — is not a
receipt, and nothing may be deleted on its authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from claude_multi import errors, state, strict_json

NAME = "installer.json"
FORMAT = 2
LAUNCHER_MARK = "# claude-multi installer launcher"
PATH_MARK = "# added by the claude-multi installer"
PATH_LINE = f'export PATH="$HOME/.local/bin:$PATH" {PATH_MARK}'
MAX_BYTES = 64 * 1024
MAX_ENTRIES = 64

_HEX64 = re.compile(r"[0-9a-f]{64}")
_KEYS = frozenset({"format", "launchers", "path_lines"})
_LAUNCHER_KEYS = frozenset({"path", "sha256"})
_LINE_KEYS = frozenset({"file", "marker", "line"})


class ReceiptError(errors.ClaudeMultiError, ValueError):
    """The receipt is missing, unreadable or not this schema."""


@dataclass(frozen=True)
class Launcher:
    path: str
    sha256: str

    def owned(self) -> bool:
        """The file is still the installer's: a regular file (not a link)
        whose bytes hash to the recorded digest."""

        return file_sha256(self.path) == self.sha256


@dataclass(frozen=True)
class PathLine:
    file: str
    marker: str
    line: str

    def present(self) -> bool:
        """The recorded line is still in the file, exactly."""

        try:
            info = os.lstat(self.file)
            if not stat.S_ISREG(info.st_mode):
                return False
            text = Path(self.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return self.line in text.splitlines()


@dataclass(frozen=True)
class Receipt:
    launchers: tuple[Launcher, ...] = ()
    path_lines: tuple[PathLine, ...] = ()

    def document(self) -> dict:
        return {
            "format": FORMAT,
            "launchers": [{"path": item.path, "sha256": item.sha256}
                          for item in sorted(self.launchers, key=lambda item: item.path)],
            "path_lines": [{"file": item.file, "marker": item.marker, "line": item.line}
                           for item in sorted(self.path_lines, key=lambda item: (item.file, item.line))],
        }


def receipt_path(install_root: Path | str) -> Path:
    return Path(install_root) / NAME


def file_sha256(path: Path | str) -> str | None:
    """The sha256 of a regular, non-link file; None otherwise."""

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1 << 16):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _absolute(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or not os.path.isabs(value) or "\n" in value or "\x00" in value:
        raise ReceiptError(f"{NAME}: {what} must be an absolute path")
    if os.path.normpath(value) != value:
        raise ReceiptError(f"{NAME}: {what} is not a normalized path")
    return value


def parse(document: object) -> Receipt:
    """Validate a receipt document (strict: unknown keys refuse)."""

    if not isinstance(document, dict) or set(document) != _KEYS:
        raise ReceiptError(f"{NAME}: not an installer receipt (keys {sorted(_KEYS)})")
    if document["format"] != FORMAT or isinstance(document["format"], bool):
        raise ReceiptError(f"{NAME}: format {document['format']!r} is not format {FORMAT} "
                           "(an older receipt records no launcher digests)")
    launchers_raw, lines_raw = document["launchers"], document["path_lines"]
    if not isinstance(launchers_raw, list) or not isinstance(lines_raw, list):
        raise ReceiptError(f"{NAME}: launchers and path_lines must be lists")
    if len(launchers_raw) > MAX_ENTRIES or len(lines_raw) > MAX_ENTRIES:
        raise ReceiptError(f"{NAME}: too many entries")
    launchers: list[Launcher] = []
    for entry in launchers_raw:
        if not isinstance(entry, dict) or set(entry) != _LAUNCHER_KEYS:
            raise ReceiptError(f"{NAME}: a launcher entry needs exactly path and sha256")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ReceiptError(f"{NAME}: a launcher sha256 must be 64 lowercase hex digits")
        launchers.append(Launcher(_absolute(entry["path"], "a launcher path"), digest))
    lines: list[PathLine] = []
    for entry in lines_raw:
        if not isinstance(entry, dict) or set(entry) != _LINE_KEYS:
            raise ReceiptError(f"{NAME}: a path line entry needs exactly file, marker and line")
        marker, line = entry["marker"], entry["line"]
        if not isinstance(marker, str) or not marker.startswith("# ") or "\n" in marker:
            raise ReceiptError(f"{NAME}: a path line marker must be one comment")
        if not isinstance(line, str) or "\n" in line or "\r" in line or not line.endswith(" " + marker):
            raise ReceiptError(f"{NAME}: a recorded line must be one line ending with its marker")
        lines.append(PathLine(_absolute(entry["file"], "a path line file"), marker, line))
    if len({item.path for item in launchers}) != len(launchers):
        raise ReceiptError(f"{NAME}: a launcher is listed twice")
    return Receipt(tuple(launchers), tuple(lines))


def read(install_root: Path | str) -> Receipt:
    """The receipt under ``install_root`` (:class:`ReceiptError` when there is
    none or it is not this schema)."""

    target = receipt_path(install_root)
    try:
        raw = state.read_private(target)
    except (OSError, errors.ClaudeMultiError) as exc:
        raise ReceiptError(f"{target}: unreadable ({getattr(exc, 'strerror', None) or exc})") from exc
    if len(raw) > MAX_BYTES:
        raise ReceiptError(f"{target}: larger than {MAX_BYTES} bytes")
    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise ReceiptError(f"{target}: not valid JSON ({exc})") from exc
    return parse(document)


def write(install_root: Path | str, receipt: Receipt) -> Path:
    target = receipt_path(install_root)
    state.atomic_write(target, (json.dumps(receipt.document(), indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return target


def record(install_root: Path | str, launchers: Iterable[Path | str],
           path_line: tuple[str, str] | None = None) -> Receipt:
    """Write the receipt after an install or repair: the launcher files as
    they are now, plus ``path_line`` (``(file, line)``) when one was added.

    Path lines of an earlier receipt are kept while they are still present
    (a later run that found ``~/.local/bin`` on PATH adds none). An earlier
    receipt that is not this schema contributes nothing: it cannot vouch for
    its entries.
    """

    try:
        earlier = read(install_root)
    except ReceiptError:
        earlier = Receipt()
    entries: list[Launcher] = []
    for raw in launchers:
        path = os.path.normpath(os.path.abspath(raw))
        digest = file_sha256(path)
        if digest is None:
            raise ReceiptError(f"the launcher {path} is not a regular file")
        entries.append(Launcher(path, digest))
    lines = {(item.file, item.line): item for item in earlier.path_lines if item.present()}
    if path_line is not None:
        file, line = os.path.normpath(os.path.abspath(path_line[0])), path_line[1]
        if not line.endswith(" " + PATH_MARK):
            raise ReceiptError("a recorded PATH line must end with the installer's marker")
        lines[(file, line)] = PathLine(file, PATH_MARK, line)
    receipt = Receipt(tuple(entries), tuple(lines.values()))
    write(install_root, receipt)
    return receipt
