"""The filesystem type under a path, and WSL detection (read-only).

Linux reads the mount table (``/proc/self/mountinfo``: the longest mount
point that contains the path wins); macOS parses ``mount`` output. Nothing
here decides policy: callers refuse a state root on a Windows drive under
WSL, and treat a network filesystem as one whose other hosts' processes are
invisible. An unreadable table is unknown (None), never "local".
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping

MOUNTINFO = Path("/proc/self/mountinfo")
# Network and shared filesystems: locks and process liveness are host-local
# there, so another host's sessions cannot be seen.
NETWORK_TYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "webdav", "davfs", "fuse.sshfs", "sshfs",
    "afs", "ceph", "fuse.ceph", "glusterfs", "fuse.glusterfs", "lustre", "gpfs", "9p", "v9fs",
    "fuse.rclone", "fuse.s3fs",
})
# How WSL mounts Windows drives: DrvFs (WSL 1) and 9p/virtiofs (WSL 2).
WINDOWS_DRIVE_TYPES = frozenset({"drvfs", "9p", "v9fs", "virtiofs"})


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def linux_mounts(mountinfo: Path = MOUNTINFO) -> list[tuple[str, str]] | None:
    """``(mount point, fstype)`` rows, or None when the table is unreadable."""

    try:
        text = mountinfo.read_text(errors="replace")
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not separator or len(fields) < 5 or not tail:
            continue
        rows.append((_unescape(fields[4]), tail[0]))
    return rows


def darwin_mounts(runner: Callable = subprocess.run) -> list[tuple[str, str]] | None:
    """``(mount point, fstype)`` from ``mount`` (``dev on /path (type, ...)``)."""

    try:
        completed = runner(["/sbin/mount"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=5, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    rows = []
    for line in str(completed.stdout).splitlines():
        match = re.match(r"^.+ on (/.*) \(([^,)]+)", line)
        if match:
            rows.append((match.group(1), match.group(2).strip()))
    return rows


def filesystem_type(path: Path | str, *, platform: str, mountinfo: Path = MOUNTINFO,
                    runner: Callable = subprocess.run) -> str | None:
    """The type of the filesystem holding ``path`` (its nearest existing parent)."""

    target = Path(os.path.abspath(path))
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        resolved = os.path.realpath(target)
    except (OSError, ValueError):
        return None
    rows = darwin_mounts(runner) if platform == "darwin" else linux_mounts(mountinfo)
    if not rows:
        return None
    best: tuple[int, str] | None = None
    for point, fstype in rows:
        prefix = point.rstrip("/") + "/"
        if resolved == point or resolved.startswith(prefix) or point == "/":
            if best is None or len(point) >= best[0]:
                best = (len(point), fstype)
    return best[1] if best else None


def wsl(environ: Mapping[str, str], *, proc_root: Path = Path("/proc")) -> str | None:
    """The WSL distro (or "WSL") when running under WSL, else None."""

    if environ.get("WSL_DISTRO_NAME"):
        return environ["WSL_DISTRO_NAME"]
    if (proc_root / "sys/fs/binfmt_misc/WSLInterop").exists():
        return "WSL"
    try:
        release = (proc_root / "sys/kernel/osrelease").read_text(errors="replace").lower()
    except OSError:
        return None
    return "WSL" if "microsoft" in release or "wsl" in release else None
