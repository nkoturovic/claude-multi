"""The shared human guard for operator authority verbs.

Route approval, admission, smoke and every other provider-call verb (and
the legacy ``custom add-*`` paths that introduce an active model or a
credential route) pass one guard, in this order, before any secret access
or side effect:

1. refuse when either Claude-session marker is set (an agent inside a
   session can run commands on its own; a flag is never the approval);
2. require the process's own stdin AND stdout to be real terminals — never
   ``/dev/tty``, which an agent's tool shell may still reach;
3. show the exact host / auth-name / action / call, then read a y/N that
   defaults to no.

Flags never waive the guard, and there is no in-session exception: every
provider call — ``discover`` (T1 and T2 listings, the codex plan listing,
``--all``, the ``--feed`` peek) and qualification — passes this same guard;
there is no in-session y/N and no approval flag. The discover plan text
names every request and each secret by name only. This is a mitigation,
not a hard boundary: an agent that drives a pseudo-terminal can pass a
terminal check (AGENTS.md §6 rule 1).
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Mapping, TextIO

from claude_multi import errors as cli_errors
import claude_multi.cli.streams as streams


SESSION_MARKERS = ("CLAUDE_MULTI_MANAGED_ID", "CLAUDECODE")
_TTY_NAME = "/dev/tty"


class ConsentRefused(cli_errors.CLIError):
    """The guard refused before any secret access or side effect."""


def session_marker(environ: Mapping[str, str]) -> str | None:
    """The first Claude-session variable set in ``environ`` (None outside)."""

    return next((name for name in SESSION_MARKERS if name in environ), None)


def stdio_ttys() -> bool:
    """Seam: this process's stdin AND stdout are real terminals (never /dev/tty)."""

    return streams._stdio_streams_are_ttys()


def guard_text(verb: str, reason: str) -> str:
    return f"{verb}: needs a terminal outside Claude Code sessions ({reason}) — run it in a separate shell"


def require_human(verb: str, environ: Mapping[str, str]) -> None:
    """Refuse inside a Claude session or without terminal stdio (E19)."""

    marker = session_marker(environ)
    if marker is not None:
        raise ConsentRefused(guard_text(verb, f"{marker} set"))
    if not stdio_ttys():
        raise ConsentRefused(guard_text(verb, "stdin/stdout is not a terminal"))


def _answer_stream(input_stream: TextIO | None) -> TextIO:
    """The process stdin, never a ``/dev/tty`` handle opened elsewhere."""

    if input_stream is None or getattr(input_stream, "name", None) == _TTY_NAME:
        return sys.stdin
    return input_stream


def prompt_stream() -> TextIO:
    """Prompts and progress go to stderr (reports own stdout)."""

    return sys.stderr


def confirm(text: str, *, input_stream: TextIO | None) -> bool:
    """Show ``text`` (ending in ``[y/N] ``) and read one answer; default no."""

    callback = getattr(input_stream, "confirm", None)
    if callable(callback):
        return bool(callback(text))
    out = prompt_stream()
    out.write(text)
    out.flush()
    answer = (_answer_stream(input_stream).readline() or "").strip().lower()
    return answer in ("y", "yes")


UPDATE_FETCH_VERB = "repin (release-evidence download)"
SETUP_DOWNLOAD_VERB = "setup --step claude (Claude Code download)"


def update_fetch_consent(environ: Mapping[str, str], ask: Callable[[Any], bool]) -> Callable[[Any], bool]:
    """The ONE per-candidate consent for the signed-manifest download of
    ``claude-multi-dev repin``. Each call passes the human guard (no session
    marker, real stdio terminals) before ``ask`` shows that candidate's
    exact ``release_manifest.RequestPlan``; a "no" (the default) sends
    nothing. An earlier "yes" never covers another candidate: the verifier
    calls this again with the next candidate's plan."""

    def decide(plan: Any) -> bool:
        require_human(UPDATE_FETCH_VERB, environ)
        return bool(ask(plan))

    return decide


def read_secret(prompt: str, *, input_stream: TextIO | None) -> str:
    """Read one secret line from the process stdin with echo off (never argv,
    never /dev/tty); an injected test stream is read as is."""

    out = prompt_stream()
    out.write(prompt)
    out.flush()
    source = _answer_stream(input_stream)
    from claude_multi import tui  # the guarded termios owner (echo off on a terminal)

    value = tui.read_hidden_line(source)
    if source is sys.stdin:
        out.write("\n")
    return (value or "").strip()
