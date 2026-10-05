"""Linux service-manager backend: systemd user units.

The only module besides the ``service`` facade that names the manager's
commands. It builds argv and hint text, reads unit properties, runs the
three control verbs (:func:`control`) the gateway lifecycle delegates to when
the user installed the supervised service, and the unit-file verbs of that
install (daemon-reload, enable/disable --now, reload); it never decides
gateway policy (ownership and the persistence hold are checked by its
callers). Every call takes an injected ``runner``. An unavailable manager
reads as ``None`` (unknown) or False, never as a stopped unit. It does not
import its facade.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import selectors
import subprocess
import time
from typing import Callable

from claude_multi.platform import observation

VERBS = ("start", "stop", "restart", "reload", "status", "reset-failed")
CONTROL_VERBS = ("start", "stop", "restart")
CONTROL_TIMEOUT = 120


def hint(verb: str, *, unit: str) -> str:
    if verb in VERBS:
        return f"systemctl --user {verb} {unit}"
    if verb == "recover":
        return f"{hint('reset-failed', unit=unit)} && {hint('start', unit=unit)}"
    if verb == "why":
        return f"systemctl --user show -p ActiveState,Result,ExecMainStatus {unit}"
    raise ValueError(f"unsupported gateway service verb: {verb}")


def journal_argv(*, since: str, unit: str) -> tuple[str, ...]:
    return ("journalctl", "--user", "-u", unit, "--since", since, "-o", "cat")


def control(verb: str, unit: str, runner: Callable, *, timeout: float = CONTROL_TIMEOUT,
            no_block: bool = False):
    """One ``systemctl --user start|stop|restart UNIT`` (bounded, no prompt).

    ``no_block`` only enqueues the job (the caller observes the outcome
    within its own deadline).
    """

    if verb not in CONTROL_VERBS:
        raise ValueError(f"unsupported control verb: {verb}")
    return runner(
        ["systemctl", "--user", "--no-ask-password", *(["--no-block"] if no_block else []), verb, unit],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )


# The unit-file verbs of ``gateway service install|uninstall`` (each bounded,
# never prompting); a False result is a refusal, never an exception.
MANAGER_TIMEOUT = 60
UNIT_PROPERTIES = ("LoadState", "FragmentPath", "UnitFileState", "ActiveState")


def _manager_run(args: list[str], runner: Callable, *, timeout: float = MANAGER_TIMEOUT):
    return runner(
        ["systemctl", "--user", "--no-ask-password", *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )


def _manager_ok(args: list[str], runner: Callable) -> bool:
    try:
        return _manager_run(args, runner).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def manager_available(runner: Callable) -> bool:
    """Whether a user service manager answers (a read; nothing changes)."""

    try:
        completed = _manager_run(["show", "--property=Version", "--value"], runner, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(str(completed.stdout).strip())


def daemon_reload(runner: Callable) -> bool:
    return _manager_ok(["daemon-reload"], runner)


def enable_now(unit: str, runner: Callable) -> bool:
    return _manager_ok(["enable", "--now", f"{unit}.service"], runner)


def disable_now(unit: str, runner: Callable) -> bool:
    return _manager_ok(["disable", "--now", f"{unit}.service"], runner)


def reload_unit(unit: str, runner: Callable) -> bool:
    return _manager_ok(["reload", f"{unit}.service"], runner)


def unit_properties(unit: str, runner: Callable) -> dict[str, str] | None:
    """The unit's load, fragment, enablement and activity (None when unreadable)."""

    try:
        completed = runner(
            ["systemctl", "--user", "show", *(f"--property={name}" for name in UNIT_PROPERTIES),
             f"{unit}.service"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    result = {}
    for line in str(completed.stdout).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in UNIT_PROPERTIES:
            result[key] = value.strip()
    return result


def unit_dir(environ) -> str:
    """Where the user's own unit files live (``$XDG_CONFIG_HOME/systemd/user``,
    else ``~/.config/systemd/user``)."""

    config = environ.get("XDG_CONFIG_HOME") or ""
    if not config.startswith("/"):
        config = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(config, "systemd", "user")


def journal_tail(*, unit: str, lines: int, runner: Callable, max_bytes: int = 256 << 10) -> list[str] | None:
    """The unit's last ``lines`` messages (``-o cat``), or None when unreadable.

    Records end at a line feed only: a carriage return, a vertical tab or a
    Unicode line separator inside a message stays in its line, where the
    redactor removes it before matching (split there, a header name and its
    value would land on two lines)."""

    try:
        completed = runner(
            ["journalctl", "--user", "-u", unit, "-n", str(max(1, lines)), "--no-pager", "-o", "cat"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode()
    records = raw[-max_bytes:].decode("utf-8", "replace").split("\n")
    if len(raw) > max_bytes:
        records = records[1:]  # the first piece may be a partial line
    if records and records[-1] == "":
        records = records[:-1]
    return records[-lines:]


QUERY_TIMEOUT = 10


def manager_query(name: str, unit: str, runner: Callable, *, timeout: float = QUERY_TIMEOUT):
    # Keep subsecond ordering against Python's success stamps; the default
    # human-readable format truncates a later pre-exec failure to whole seconds.
    timestamp = ["--timestamp=us+utc"] if name == "ExecReload" else []
    return runner(
        ["systemctl", "--user", "show", f"--property={name}", "--value", *timestamp, unit],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )


def manager_property(name: str, unit: str, runner: Callable, *, timeout: float = QUERY_TIMEOUT) -> str | None:
    try:
        completed = manager_query(name, unit, runner, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def manager_time(value: str) -> datetime.datetime:
    # manager_query pins the command's locale and timezone, not the manager's.
    form = "%a %Y-%m-%d %H:%M:%S" + (".%f" if "." in value else "") + " UTC"
    return datetime.datetime.strptime(value, form).replace(tzinfo=datetime.timezone.utc)


def last_reload_result(*, unit: str, runner: Callable) -> observation.ExecReloadResult | None:
    """Read the last completed ExecReload, including failures before Python ran.

    systemctl's ExecReload record carries wall-clock start/stop times. A boolean
    alone cannot distinguish a new pre-exec failure from a failure superseded
    by a successful config-only init. Unset or malformed records are unavailable.
    """
    raw = manager_property("ExecReload", unit, runner)
    if raw is None:
        return None
    results = []
    for record in re.findall(r"\{([^{}]*)\}", raw):
        match = re.search(
            r"start_time=\[([^\]]+)\]\s*;\s*stop_time=\[([^\]]+)\]"
            r"\s*;\s*pid=[0-9]+\s*;\s*code=(exited|killed|dumped|[123])"
            r"\s*;\s*status=([0-9]+)(?:/[A-Za-z0-9+_-]+)?\s*$", record,
        )
        if match is None:
            return None
        start, stop, code, status = match.groups()
        try:
            started_at, stopped_at = manager_time(start), manager_time(stop)
            failed = code not in ("exited", "1") or int(status) != 0
        except ValueError:
            return None
        if stopped_at < started_at:
            return None
        results.append(observation.ExecReloadResult(started_at, failed))
    return max(results, key=lambda result: (result.started_at, result.failed), default=None)


def journal_records(raw: bytes, *, coverage: str = "bounded",
                    newest_first: bool = False) -> observation.LogWindow:
    """Normalize journald fields; raw JSON, paths and messages are never facts.

    boot/invocation is the process-generation identity, not a binary attestation.
    Missing fields remain unknown, never inferred from the current gateway PID.
    ``newest_first`` input (``journalctl --reverse``) keeps the newest records
    under the record cap; the window is always returned in chronological order.
    """
    records = []
    for line in raw.splitlines():
        if len(records) >= observation.LOG_MAX_RECORDS:
            coverage = "truncated"
            break
        if len(line) > 1 << 20:
            coverage = "incomplete"
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            coverage = "incomplete"
            continue
        if not isinstance(item, dict) or not isinstance(item.get("MESSAGE"), str):
            coverage = "incomplete"
            continue
        message = item["MESSAGE"]
        timestamp = None
        micros = item.get("__REALTIME_TIMESTAMP")
        if isinstance(micros, str) and re.fullmatch(r"[0-9]{1,18}", micros):
            try:
                timestamp = datetime.datetime.fromtimestamp(int(micros) / 1_000_000, datetime.timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        ids = (item.get("_BOOT_ID"), item.get("_SYSTEMD_INVOCATION_ID"))
        instance = ":".join(ids) if all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in ids
        ) else None
        cursor = item.get("__CURSOR")
        if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9;=_.-]{1,512}", cursor):
            cursor = None
        records.append(observation.LogRecord(message, timestamp, instance, cursor))
    if newest_first:
        records.reverse()
    return observation.LogWindow(tuple(records), coverage)


def read_gateway_journal(*, unit: str, max_bytes: int, timeout: float = 10,
                         popen: Callable = subprocess.Popen, since: str = "-24h") -> observation.LogWindow:
    """One hard-bounded journald pass: bytes, records and wall time.

    Never buffer unbounded stdout before slicing it. stderr is discarded; on
    timeout/overflow kill and reap the reader, not the gateway. Even successful
    reads are bounded observations, not journal-continuity attestations.
    The reader runs newest first, so any cap drops the oldest part of the 24 h
    window (the window then says so), never the newest events.
    """
    argv = ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "--reverse",
            "-o", "json",
            "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_INVOCATION_ID"]
    raw = bytearray()
    coverage = "bounded"
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        with popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            coverage = "incomplete"
                            timed_out = True
                            break
                        chunk = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > max_bytes:
                            coverage = "truncated"
                            break
                if coverage == "bounded":
                    try:
                        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                            return observation.LogWindow()
                    except subprocess.TimeoutExpired:
                        coverage, timed_out = "incomplete", True
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, subprocess.SubprocessError):
        return observation.LogWindow()
    if coverage != "bounded":
        # The last record may have been cut in the middle of JSON or UTF-8.
        raw = raw[:max_bytes].rsplit(b"\n", 1)[0] if b"\n" in raw[:max_bytes] else b""
    window = journal_records(bytes(raw), coverage=coverage, newest_first=True)
    if len(window.records) >= observation.LOG_MAX_RECORDS:
        return observation.LogWindow(window.records, "truncated", timed_out)
    return observation.LogWindow(window.records, window.coverage, timed_out)
