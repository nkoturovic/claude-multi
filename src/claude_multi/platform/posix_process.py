"""POSIX process primitives for the on-demand gateway: existence, signals, a spawn.

Stdlib only, no policy: callers decide which process may be signalled (only
one proven to be their gateway) and what a spawn's outcome means. Nothing
here retries or sleeps.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Mapping, Sequence

# The spawned starter forks the gateway and exits at once; its own exit code
# says whether that happened. It never waits for the gateway's readiness.
DETACH_TIMEOUT = 10.0


def exists(pid: int) -> bool | None:
    """``kill(pid, 0)``: True present (any owner), False absent, None unknown."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def terminate(pid: int) -> bool:
    """Send SIGTERM; False when the process is already gone."""

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def spawn_detached(argv: Sequence[str], *, cwd: str, env: Mapping[str, str], output: int,
                   timeout: float = DETACH_TIMEOUT) -> int | None:
    """Start ``argv`` in a new session (umask 077, stdin from /dev/null, stdout and
    stderr to ``output``) and wait for that starter to exit.

    Returns its exit code, or None when it did not exit within ``timeout``
    (it is then terminated: a starter that never detached is not a gateway).
    """

    process = subprocess.Popen(
        list(argv), stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=cwd,
        env=dict(env), start_new_session=True, umask=0o077, close_fds=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return None
