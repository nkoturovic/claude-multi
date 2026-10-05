"""Always-on live-gateway connect tripwire for the test process.

``tests/__init__.py`` installs it for every run that imports the ``tests``
package (the documented ``discover -s tests -t .`` gate and the Nix sandbox
suite). It wraps ``socket.socket.connect`` and ``connect_ex`` and refuses any
TCP connect to the live gateway ports (8317 gateway, 8316 proxy, and the port
the developer's own ``~/.config/claude-multi/endpoint.json`` configures, read
once at import from the real HOME) on a loopback/unspecified host (``127.0.0.0/8``, ``::1``, ``0.0.0.0``, ``::``,
``localhost``) by raising
``ConnectionRefusedError("test tripwire: live gateway port")`` — before the
real connect runs, so nothing ever reaches the live daemon.

Every refusal is recorded in :data:`HITS`, reported once on stderr at exit,
and — when ``CM_ISOLATION_GUARD_LOG`` is set (the external sitecustomize
tripwire of ``check_fixture_isolation.py`` and the done-gate command) —
appended to that log as one ``BLOCKED`` entry. The guard raises before it
delegates to whatever ``connect`` it wrapped, so when the external tripwire is
also installed the hit is logged exactly once (by this guard), never twice.
Child processes are not covered here; the external sitecustomize tripwire and
the real-binary network namespace cover them.

The listener observer is also guarded: audit events refuse reads
of /proc/net/tcp{,6}, real systemctl MainPID queries, every real systemctl
unit control or unit-file verb and every real ``nix-store --add-root`` (a test
never controls the developer's units or garbage-collector roots). Fake proc roots and
injected runners never reach those events. AssertionError prevents the
observer from degrading a blocked live read into an 'unknown' verdict.

No test binds a fake gateway on 8317/8316 on purpose (every fake server uses
an ephemeral port or a unix socket), so there is deliberately no opt-out.
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import socket
import sys
import traceback
from typing import Any

def configured_ports(environ: Any = os.environ) -> frozenset[int]:
    """The port of the real HOME's gateway endpoint document, if it names one."""

    home = environ.get("HOME") or os.path.expanduser("~")
    try:
        with open(os.path.join(home, ".config", "claude-multi", "endpoint.json"), "rb") as handle:
            port = json.loads(handle.read(65536)).get("port")
    except (OSError, ValueError, AttributeError):
        return frozenset()
    return frozenset({port}) if type(port) is int and 0 < port < 65536 else frozenset()


PORTS = frozenset({8317, 8316}) | configured_ports()
_CONTROL_VERBS = frozenset({"start", "stop", "restart", "reload", "try-restart", "reload-or-restart",
                            "enable", "disable", "kill", "daemon-reload", "reset-failed", "mask", "unmask",
                            "link", "revert", "preset"})
MESSAGE = "test tripwire: live gateway port"
HITS: list[tuple[Any, ...]] = []

_MARK = "_cm_test_tripwire"


def is_live_gateway(address: Any) -> bool:
    """True for a TCP address naming a loopback host on 8317/8316."""

    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = address[0], address[1]
    if not isinstance(port, int) or port not in PORTS:
        return False
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    host = host.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_unspecified


def _record(address: Any) -> None:
    HITS.append(tuple(address))
    log = os.environ.get("CM_ISOLATION_GUARD_LOG")
    if not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"BLOCKED {address!r} pid={os.getpid()} source=tests-guard\n")
            handle.write("".join(traceback.format_stack(limit=12)) + "----\n")
    except OSError:
        pass  # the in-process record and the exit report still stand


def _wrap(original: Any) -> Any:
    def guarded(self: socket.socket, address: Any) -> Any:
        if is_live_gateway(address):
            _record(address)
            raise ConnectionRefusedError(MESSAGE)
        return original(self, address)

    guarded.__name__ = getattr(original, "__name__", "connect")
    guarded.__qualname__ = getattr(original, "__qualname__", guarded.__name__)
    guarded.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(guarded, _MARK, True)
    return guarded


def _listener_observation(event: str, args: tuple) -> None:
    target = None
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.path.normpath(os.fsdecode(args[0]))
        if path in ("/proc/net/tcp", "/proc/net/tcp6"):
            target = path
    elif event == "subprocess.Popen":
        executable, argv = args[:2]
        if (os.path.basename(os.fsdecode(executable)) == "systemctl"
                and not isinstance(argv, (str, bytes))):
            words = [os.fsdecode(arg) for arg in argv]
            if any("MainPID" in word for word in words):
                target = "systemctl MainPID"
            elif _CONTROL_VERBS & set(words):
                target = "systemctl unit control"
        elif (os.path.basename(os.fsdecode(executable)) == "nix-store"
              and not isinstance(argv, (str, bytes))
              and "--add-root" in {os.fsdecode(arg) for arg in argv}):
            target = "nix-store garbage-collector root"
    if target is not None:
        _record(("listener-owner", target))
        # Not OSError: the observer must not swallow the guard as 'unknown'.
        raise AssertionError("test tripwire: live listener observation")


def installed() -> bool:
    return all(
        getattr(getattr(socket.socket, name), _MARK, False)
        for name in ("connect", "connect_ex")
    )


def _report() -> None:
    if HITS:
        print(
            f"test tripwire: refused {len(HITS)} live-gateway connect/observation(s): "
            + ", ".join(repr(hit) for hit in HITS[:5]),
            file=sys.stderr,
        )


def install() -> None:
    """Install the guard once per process (idempotent across module copies)."""

    if not getattr(sys, "_cm_listener_tripwire", False):
        sys.addaudithook(_listener_observation)
        sys._cm_listener_tripwire = True
    for name in ("connect", "connect_ex"):
        current = getattr(socket.socket, name)
        if not getattr(current, _MARK, False):
            setattr(socket.socket, name, _wrap(current))
    if not getattr(install, "_reported", False):
        atexit.register(_report)
        install._reported = True  # type: ignore[attr-defined]
