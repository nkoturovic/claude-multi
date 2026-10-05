#!/usr/bin/env python3
"""A stand-in for the gateway program's sign-in commands (tests only).

Used as ``CLAUDE_MULTI_PROXY_BIN``. It reads ``auth-dir`` from the
``--config`` file and behaves like the real sign-in, without any network:

- ``--claude-login [--no-browser]``: prints the address line and a fake
  address, then asks for the callback address; a line containing ``code=``
  writes ``claude-user@example.com.json`` (0600).
- ``--codex-device-login``: prints an address and a code, then writes
  ``codex-user@example.com.json``.
- An interrupt exits 130 and writes nothing.
- ``FAKE_LOGIN_PORT_BUSY=1``: exits with the port-in-use status (13).
- ``FAKE_LOGIN_ARGV``: a file the argument vector is appended to (one JSON
  array per run), so tests can see ``--no-browser``.
- ``FAKE_LOGIN_WAIT=1``: wait for input before writing anything (an
  interrupt then cancels).
- ``FAKE_LOGIN_PLAIN=1``: install no interrupt handler of its own, so the
  interrupt does what the disposition this program started with says (like
  a sign-in program that never handles it); ``FAKE_LOGIN_SELF_INTERRUPT=1``
  then delivers the terminal's interrupt to itself before it writes the
  record. ``FAKE_LOGIN_DISPOSITION``: a file the interrupt disposition it
  started with is written to (``default`` or ``ignored``).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

PORT_IN_USE = 13


def _auth_dir(argv: list[str]) -> Path:
    config = Path(argv[argv.index("--config") + 1])
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("auth-dir:"):
            return Path(line.split(":", 1)[1].strip().strip('"'))
    raise SystemExit("fake login: no auth-dir in the config")


def _write(directory: Path, name: str) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write('{"type": "fake"}\n')


def main(argv: list[str]) -> int:
    disposition = os.environ.get("FAKE_LOGIN_DISPOSITION")
    if disposition:
        ignored = signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        Path(disposition).write_text("ignored" if ignored else "default", encoding="utf-8")
    if os.environ.get("FAKE_LOGIN_PLAIN") != "1":
        signal.signal(signal.SIGINT, lambda *_args: os._exit(130))
    record = os.environ.get("FAKE_LOGIN_ARGV")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    if os.environ.get("FAKE_LOGIN_PORT_BUSY") == "1":
        print("OAuth callback port is already in use", file=sys.stderr)
        return PORT_IN_USE
    directory = _auth_dir(argv)
    if "--claude-login" in argv:
        print("Visit the following URL to continue authentication:")
        print("https://claude.ai/oauth/authorize?fake=1")
        sys.stdout.write("Paste the Claude callback URL (or press Enter to keep waiting): ")
        sys.stdout.flush()
        answer = sys.stdin.readline()
        if "code=" in answer:
            _write(directory, "claude-user@example.com.json")
            print("Claude authentication successful!")
        return 0
    if "--codex-device-login" in argv:
        print("Open https://auth.openai.com/codex/device and enter the code FAKE-CODE")
        sys.stdout.flush()
        if os.environ.get("FAKE_LOGIN_WAIT") == "1":
            sys.stdin.readline()
        if os.environ.get("FAKE_LOGIN_SELF_INTERRUPT") == "1":
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)
        _write(directory, "codex-user@example.com.json")
        print("Codex authentication successful!")
        return 0
    print("fake login: unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
