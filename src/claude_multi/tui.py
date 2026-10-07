"""Terminal UI layer for claude-multi: widgets, palettes, and the profile editor.

Everything interactive lives here: a small stdlib-curses widget set (labels,
badges, key bars, checkboxes and groups, select lists, text inputs, modals,
tables), light/dark/mono palette detection (COLORFGBG, then an OSC 11 query,
default dark; NO_COLOR honored), and the profile editor stack
(:class:`ProfileEditorState`, :class:`ProfileEditorScreen`, the binding
picker, the routing preview and named bindings).

Terminal discipline: curses is only ever entered through
:func:`run_curses_on_streams`, which points fds 0/1 at the caller's terminal
streams when needed and always restores them (``try/finally`` plus
``curses.wrapper``'s own teardown).  Every colorized element also carries a
text form (UX section 12): checkboxes render ``[x]``/``[ ]``, badges render
their words, status lines render ``Ready``/``BLOCKED`` verbatim.

The editor state (:class:`ProfileEditorState`) is pure and has no terminal or
persistence dependencies; every store write goes through the callbacks the
cli glue passes in.  ``$EDITOR`` is integrated exactly once: Ctrl+G inside
the editor opens the raw profile JSON (with the terminal suspended and
restored around it).
"""

from __future__ import annotations

from .termtext import CHEATSHEET_HINT, visible_text, visible_message

import contextlib
import copy
import dataclasses
import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # A Python without _curses (or termios) must still import cli
    import curses
except ImportError:
    curses = None  # type: ignore[assignment]
try:
    import termios
except ImportError:
    termios = None  # type: ignore[assignment]

from . import catalog as catalog_mod
from . import profile as profile_mod
from . import settings as settings_mod
from . import strict_json, views


class _NoCursesError(Exception):
    """Never raised; the stand-in for ``curses.error`` when curses is absent."""


# The one curses error class every caller catches (cli.py never imports
# curses itself): ``curses.error`` when curses exists.
CursesError: type[Exception] = curses.error if curses is not None else _NoCursesError


def curses_available() -> bool:
    """Whether this Python has a usable ``curses`` module."""

    return curses is not None


# The curses session's escape delay in milliseconds: a lone
# Esc is delivered promptly instead of after ncurses' 1 s default.
ESC_DELAY_MS = 25

# Screen floors: drawn at rows 1-2, column 2, when a screen is
# below its ``min_size``.  They live only here (views never re-exports them).
TOO_SMALL = "Terminal too small for {what}."
TOO_SMALL_HINT = "Resize to at least {cols}x{rows}, or press Esc to go back."

# The profile schema's ``workflows`` enum, for the profile editor.
WORKFLOWS_VALUES = ("native", "off")


# ---------------------------------------------------------------------------
# The workflow guarantee panel.

WORKFLOW_GUARANTEE_PANEL = (
    "Native workflows (ultracode): {state}\n"
    "  · workflow agents run as the SESSION model by default (lead family;\n"
    "    Settings workflow default binding changes that default), always\n"
    "    acceptEdits — scripts may route stages to other models, so\n"
    "    unobserved workflow output is family-unknown/mixed and never counts\n"
    "    as an independent review verdict\n"
    "  · no cm-role contract; ≤16 concurrent / 1000 per run\n"
    "  · a cm-* agentType keeps its model and effort, but isolation is per\n"
    "    call: a workflow writer must pass isolation:'worktree' itself"
)

WORKFLOW_OFF_NOTE = (
    "off mode: disableWorkflows:true compiled; lead ultracode→xhigh; keyword inert;\n"
    "/deep-research unavailable (documented consequences). The TUI never\n"
    "presents off as \"safer subagents\" — just different."
)


def workflow_guarantee_panel(workflows: str) -> str:
    """The UX section 4 guarantee panel for a workflow mode (text form)."""

    panel = WORKFLOW_GUARANTEE_PANEL.format(
        state="OFF" if workflows == "off" else "ON"
    )
    if workflows == "off":
        panel += "\n" + WORKFLOW_OFF_NOTE
    return panel


# ---------------------------------------------------------------------------
# Palettes: light/dark detection, NO_COLOR, role attributes.

# Fixed pair numbers so role attributes are plain integers (n << 8) that are
# valid before curses is initialized; init_curses_colors assigns the pairs.
_PAIR_BY_ROLE = {"accent": 1, "ok": 2, "warn": 3, "error": 4, "dim": 5}


def _pair_fg(palette_name: str) -> dict[str, int]:
    """Foreground color per role for a palette (built only inside curses)."""

    if palette_name == "light":
        return {
            "accent": curses.COLOR_BLUE,
            "ok": curses.COLOR_GREEN,
            "warn": curses.COLOR_YELLOW,
            "error": curses.COLOR_RED,
            "dim": curses.COLOR_BLACK,
        }
    return {
        "accent": curses.COLOR_CYAN,
        "ok": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "error": curses.COLOR_RED,
        # 8-color fallback only; init_curses_colors overrides with a 256-color
        # gray when the terminal has one. Never black: bold black renders as
        # nearly-invisible dark gray on dark backgrounds.
        "dim": curses.COLOR_WHITE,
    }
_BOLD_ROLES = {
    "dark": frozenset(("accent", "error", "dim")),
    "light": frozenset(("accent", "dim")),
}
_ANSI_SGR = {
    "dark": {
        "accent": "1;36",
        "ok": "32",
        "warn": "33",
        "error": "1;31",
        "dim": "38;5;245",
    },
    "light": {
        "accent": "1;34",
        "ok": "32",
        "warn": "33",
        "error": "31",
        "dim": "38;5;240",
    },
}


@dataclass(frozen=True)
class Palette:
    """Role -> attribute mapping; ``mono`` palettes disable color entirely."""

    name: str  # "dark" | "light" | "mono"
    colors: bool

    def attr(self, role: str) -> int:
        """Curses attribute for a role; 0 for normal/mono (text form stays)."""

        if not self.colors or role in ("normal", "selected"):
            return 0
        pair = _PAIR_BY_ROLE.get(role)
        if pair is None:
            return 0
        attr = pair << 8
        if role in _BOLD_ROLES.get(self.name, ()) and curses is not None:
            attr |= curses.A_BOLD
        return attr

    def ansi(self, text: str, role: str) -> str:
        """ANSI-styled text for line output (doctor badges); mono is verbatim."""

        if not self.colors or role not in _ANSI_SGR.get(self.name, {}):
            return text
        return f"\x1b[{_ANSI_SGR[self.name][role]}m{text}\x1b[0m"


MONO_PALETTE = Palette("mono", False)
DARK_PALETTE = Palette("dark", True)
LIGHT_PALETTE = Palette("light", True)


def background_from_colorfgbg(value: str) -> str | None:
    """COLORFGBG convention: last field is the background color index."""

    if not value:
        return None
    try:
        bg = int(value.split(";")[-1])
    except ValueError:
        return None
    if bg == 7 or bg >= 9:  # white or bright backgrounds read as light
        return "light"
    if 0 <= bg <= 8:
        return "dark"
    return None


_OSC11_RE = re.compile(
    rb"\]11;rgba?:([0-9A-Fa-f]{1,4})/([0-9A-Fa-f]{1,4})/([0-9A-Fa-f]{1,4})"
)


def _channel(hex_digits: bytes) -> float:
    digits = hex_digits.decode("ascii")
    if len(digits) >= 2:
        return int(digits[:2], 16) / 255.0
    return int(digits, 16) / 15.0


def parse_osc11_response(data: bytes) -> str | None:
    """Parse an OSC 11 reply into ``light``/``dark`` by relative luminance."""

    match = _OSC11_RE.search(data)
    if match is None:
        return None
    red, green, blue = (_channel(group) for group in match.groups())
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "light" if luminance > 0.5 else "dark"


def read_hidden_line(stream: Any) -> str:
    """One line from ``stream`` with terminal echo off when it is a
    terminal (``providers set-key``); any other stream is read as is."""

    try:
        descriptor = stream.fileno()
        saved = termios.tcgetattr(descriptor) if termios is not None else None
    except (AttributeError, OSError, ValueError) + ((termios.error,) if termios is not None else ()):
        saved = None
    if saved is None:
        return stream.readline() or ""
    quiet = list(saved)
    quiet[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, quiet)
        return stream.readline() or ""
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def query_background_osc11(
    fd_in: int, fd_out: int, *, timeout: float = 0.15
) -> str | None:
    """Ask the terminal for its background color (OSC 11), best effort.

    Runs the query in raw mode with a short timeout and drains the reply so
    no response bytes leak into a later curses input buffer.  A reply cut
    off by the wait is remembered, so the key reader reads its rest as part
    of it (:func:`read_key`).  Any failure (no termios, no answer, garbage)
    yields ``None`` — the caller defaults.
    """

    if termios is None:
        return None
    try:
        saved = termios.tcgetattr(fd_in)
    except (termios.error, OSError):
        return None
    _carry_reply(None)
    try:
        raw = termios.tcgetattr(fd_in)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(fd_in, termios.TCSANOW, raw)
        try:
            os.write(fd_out, b"\x1b]11;?\x07")
        except OSError:
            return None
        data = bytearray()
        deadline_reads = 4  # reply may arrive in fragments; drain after first
        for _ in range(deadline_reads):
            try:
                ready, _, _ = select.select([fd_in], [], [], timeout)
            except (OSError, ValueError):
                return None
            if not ready:
                break
            try:
                chunk = os.read(fd_in, 256)
            except OSError:
                return None
            if not chunk:
                break
            data.extend(chunk)
            timeout = 0.02  # drain: later fragments get a tiny window
        _carry_reply(_unfinished_reply(bytes(data)))
        return parse_osc11_response(bytes(data))
    except (termios.error, OSError, ValueError):
        return None
    finally:
        try:
            termios.tcsetattr(fd_in, termios.TCSANOW, saved)
        except (termios.error, OSError):
            pass


# A remote login: the terminal's reply to a background query can arrive
# after its short wait, so it is never asked there.
_REMOTE_MARKERS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


def osc_query_allowed(environ: Mapping[str, str], *, no_color: bool = False, query: bool = True) -> bool:
    """Whether the background colour may be asked (OSC 11): a full-screen
    session with colour, on a local terminal whose type is known."""

    if not query or no_color or "NO_COLOR" in environ:
        return False
    if any(environ.get(name) for name in _REMOTE_MARKERS):
        return False
    return environ.get("TERM") not in (None, "", "dumb")


def detect_palette(
    environ: Mapping[str, str] | None = None,
    *,
    no_color: bool = False,
    tty_in: Any = None,
    tty_out: Any = None,
    query: bool = True,
) -> Palette:
    """Resolve the active palette.

    Order: ``no_color``/``NO_COLOR`` forces mono; ``COLORFGBG`` decides when
    present; otherwise an OSC 11 background query on the terminal streams
    (only when :func:`osc_query_allowed`: never in line mode — ``query``
    False —, over SSH or without colour); default dark.  Streams without fds
    simply skip OSC.
    """

    environ = os.environ if environ is None else environ
    if no_color or "NO_COLOR" in environ:
        return MONO_PALETTE
    background = background_from_colorfgbg(environ.get("COLORFGBG", ""))
    if (background is None and tty_in is not None and tty_out is not None
            and osc_query_allowed(environ, no_color=no_color, query=query)):
        try:
            fd_in = tty_in.fileno()
            fd_out = tty_out.fileno()
        except (AttributeError, OSError):
            fd_in = fd_out = None
        if fd_in is not None and fd_out is not None:
            background = query_background_osc11(fd_in, fd_out)
    if background == "light":
        return LIGHT_PALETTE
    return DARK_PALETTE


def _dim_foreground(palette_name: str) -> int:
    """Muted-text gray: 256-color mid gray when the terminal has it.

    245 (#8a8a8a) on dark, 240 (#585858) on light — muted but readable on
    each. 8-color fallback is white on dark palettes and black on light
    ones — never black on dark, and never the old black-on-black trap when
    the terminal has no default-colors support either.
    """

    if getattr(curses, "COLORS", 0) >= 256:
        return 245 if palette_name == "dark" else 240
    return curses.COLOR_WHITE if palette_name == "dark" else curses.COLOR_BLACK


def init_curses_colors(palette: Palette) -> None:
    """Initialize color pairs for a palette inside a live curses session."""

    if curses is None:
        return
    try:
        if not palette.colors or not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except CursesError:
            background = curses.COLOR_BLACK
        foregrounds = _pair_fg(palette.name)
        for role, pair in _PAIR_BY_ROLE.items():
            foreground = foregrounds[role]
            if role == "dim":
                foreground = _dim_foreground(palette.name)
            try:
                curses.init_pair(pair, foreground, background)
            except CursesError:
                continue
    except CursesError:
        return


def streams_curses_capable(
    input_stream: Any, output_stream: Any, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether a full-screen curses UI may run on these streams.

    Requires a capable TERM and real ttys on both sides; the actual curses
    entry point still defensively falls back if initscr itself fails.  A
    Python without curses is never capable, so every interactive entry
    degrades to its line flow.
    """

    if curses is None:
        return False
    environ = os.environ if environ is None else environ
    if environ.get("TERM") in (None, "", "dumb"):
        return False
    try:
        if not (input_stream.isatty() and output_stream.isatty()):
            return False
    except (AttributeError, ValueError, OSError):
        return False
    return terminal_known(environ.get("TERM", ""), output_stream)


def terminal_known(term: str, output_stream: Any = None) -> bool:
    """Whether this system has a terminal description for ``term`` (a
    full-screen session needs one); an unknown type keeps the line interface."""

    if curses is None or not term:
        return False
    try:
        fd = output_stream.fileno() if output_stream is not None else -1
    except (AttributeError, OSError, ValueError):
        fd = -1
    try:
        curses.setupterm(term, fd)
    except (CursesError, OSError, ValueError, TypeError):
        return False
    return True


TERM_NOTICE = ("claude-multi: the terminal type TERM={term} is not known here, so the line interface is "
               "used; if your terminal supports it, run with TERM=xterm-256color for the full screen")
TERM_UNSET_NOTICE = ("claude-multi: TERM is not set, so the line interface is used; if your terminal supports "
                     "it, run with TERM=xterm-256color for the full screen")


def term_notice(environ: Mapping[str, str], input_stream: Any, output_stream: Any) -> str | None:
    """The one line explaining why a terminal falls back to the line
    interface (an unset or unknown ``TERM``), or None. ``dumb`` is a choice
    and gets none; so do streams that are not terminals."""

    if curses is None:
        return None
    try:
        if not (input_stream.isatty() and output_stream.isatty()):
            return None
    except (AttributeError, ValueError, OSError):
        return None
    term = environ.get("TERM")
    if term == "dumb":
        return None
    if not term:
        return TERM_UNSET_NOTICE
    if terminal_known(term, output_stream):
        return None
    shown = shlex.quote(visible_text(term)[:40])
    return TERM_NOTICE.format(term=shown)


# ---------------------------------------------------------------------------
# Key normalization: curses codes (and the test double) become Key values.


@dataclass(frozen=True)
class Key:
    kind: str  # char|enter|tab|esc|up|down|left|right|home|end|backspace|
    # delete|resize|btab|pageup|pagedown|f1|ctrl|unknown
    ch: str = ""


_KEY_KINDS_CACHE: dict[int, str] | None = None


def _key_kinds() -> dict[int, str]:
    """curses key code -> Key kind; built on first use (no import-time curses)."""

    global _KEY_KINDS_CACHE
    if _KEY_KINDS_CACHE is None:
        _KEY_KINDS_CACHE = {
            curses.KEY_UP: "up",
            curses.KEY_DOWN: "down",
            curses.KEY_LEFT: "left",
            curses.KEY_RIGHT: "right",
            curses.KEY_HOME: "home",
            curses.KEY_END: "end",
            curses.KEY_BACKSPACE: "backspace",
            curses.KEY_DC: "delete",
            curses.KEY_ENTER: "enter",
            curses.KEY_BTAB: "btab",
            curses.KEY_RESIZE: "resize",
            curses.KEY_PPAGE: "pageup",
            curses.KEY_NPAGE: "pagedown",
            curses.KEY_F1: "f1",
        }
    return _KEY_KINDS_CACHE


# A terminal's reply to a colour query (OSC 10/11) that arrives after the
# query stopped waiting: ESC ] 1 0|1 ; <colour> BEL|ESC \\. Each byte must
# follow within OSC_GAP_MS, the whole reply fits in OSC_MAX_CHARS; anything
# else is input and is delivered unchanged.
OSC_GAP_MS = 80
OSC_MAX_CHARS = 64
# A reply that paused inside its body is read on across further pauses, but
# not for ever: a key that comes this long after the reply last moved (or
# after the key reader started waiting for its rest) is input.
OSC_CARRY_SECONDS = 2.0
_OSC_PREFIXES = ("]11;", "]10;")
_OSC_BODY = frozenset("rgbaRGBA:/0123456789abcdefABCDEF")
# Keys read ahead while checking for a reply, per window (held with the
# window itself, so an id is never reused while keys wait), delivered next.
_PUSHBACK: dict[int, tuple[Any, list[Any]]] = {}


def _pending(win: Any) -> list[Any]:
    entry = _PUSHBACK.get(id(win))
    return entry[1] if entry is not None and entry[0] is win else []


def _next_value(win: Any) -> Any:
    pending = _pending(win)
    if pending:
        value = pending.pop(0)
        if not pending:
            _PUSHBACK.pop(id(win), None)
        return value
    return win.get_wch()


def _push_back(win: Any, values: Sequence[Any]) -> None:
    if values:
        pending = _pending(win)
        _PUSHBACK[id(win)] = (win, [*values, *pending])


def _peek(win: Any) -> Any:
    """The next input within :data:`OSC_GAP_MS`, or None."""

    if _pending(win):
        return _next_value(win)
    try:
        win.timeout(OSC_GAP_MS)
    except (AttributeError, CursesError):
        return None
    try:
        return win.get_wch()
    except CursesError:
        return None
    finally:
        try:
            win.timeout(-1)
        except CursesError:
            pass


def _swallow_osc_reply(win: Any) -> bool:
    """After an ESC: consume a colour-query reply and return True, or read
    nothing (pushing back what was read ahead) and return False."""

    read: list[Any] = []
    while len(read) < max(len(prefix) for prefix in _OSC_PREFIXES):
        value = _peek(win)
        if value is None:
            _push_back(win, read)
            return False
        read.append(value)
        text = "".join(item for item in read if isinstance(item, str))
        if len(text) != len(read) or not any(prefix.startswith(text) for prefix in _OSC_PREFIXES):
            _push_back(win, read)
            return False
    # Inside a reply: its body, then BEL or ESC backslash. A key that cannot
    # belong to the reply ends it and is delivered as input. A reply whose
    # rest is late is remembered, so its terminator is never read as Esc.
    text = "".join(read)
    for _count in range(OSC_MAX_CHARS):
        value = _peek(win)
        if value is None:
            _carry_reply(text)
            return True
        if value == "\x07":
            return True
        if value == "\x1b":
            after = _peek(win)
            if after is None:
                _carry_reply(text + "\x1b")
            elif after != "\\":
                _push_back(win, [after])
            return True
        if not (isinstance(value, str) and value in _OSC_BODY):
            _push_back(win, [value])
            return True
        text += value
    return True


def _reply_progress(text: str) -> str:
    """Where ``text`` (what followed an ESC) stands in a colour-query reply:
    ``partial`` (the start of one), ``complete`` (a whole reply with its
    terminator) or ``invalid`` (not a reply)."""

    size = len(_OSC_PREFIXES[0])
    if len(text) < size:
        return "partial" if any(prefix.startswith(text) for prefix in _OSC_PREFIXES) else "invalid"
    if text[:size] not in _OSC_PREFIXES:
        return "invalid"
    body = text[size:]
    for index, char in enumerate(body):
        if char == "\x07":
            return "complete" if index == len(body) - 1 else "invalid"
        if char == "\x1b":
            rest = body[index + 1:]
            return "partial" if not rest else "complete" if rest == "\\" else "invalid"
        if char not in _OSC_BODY:
            return "invalid"
    return "partial" if len(body) <= OSC_MAX_CHARS else "invalid"


def _unfinished_reply(data: bytes) -> str | None:
    """What followed the ESC of a colour-query reply in ``data`` that
    stops before its terminator, or None (no reply, or a whole one)."""

    text = data.decode("latin-1")
    for index, char in enumerate(text):
        if char == "\x1b" and _reply_progress(text[index + 1:]) == "partial":
            return text[index + 1:]
    return None


# The start of a colour-query reply whose rest had not arrived when its
# reader stopped (the query's wait, or the key reader's gap): the next key
# read continues that reply (:func:`_finish_carried_reply`). One terminal,
# so one process-wide value, with the time the reply last moved (empty until
# the key reader first waits for its rest).
_REPLY_TAIL: list[str] = []
_REPLY_SINCE: list[float] = []
_clock = time.monotonic


def _carry_reply(text: str | None, *, since: float | None = None) -> None:
    _REPLY_TAIL[:] = [] if text is None else [text]
    _REPLY_SINCE[:] = [] if text is None or since is None else [since]


def _finish_carried_reply(win: Any) -> None:
    """Read the rest of a reply that began before this read: the keys that
    continue it are consumed, also across further pauses before its
    terminator (the reply stays open, and none of its bytes is ever
    delivered as a key). A key that cannot belong to it is delivered alone.
    An Esc is the reply's terminator only when a backslash follows it: any
    other key after it is delivered after the Esc, and so is an Esc nothing
    follows; a key that comes :data:`OSC_CARRY_SECONDS` after the reply
    last moved is input."""

    text = _REPLY_TAIL[0] if _REPLY_TAIL else None
    if text is None:
        return
    since = _REPLY_SINCE[0] if _REPLY_SINCE else _clock()
    _carry_reply(None)
    try:
        value = _next_value(win)
    except BaseException:
        _carry_reply(text, since=since)
        raise
    # An Esc read inside the reply, kept aside until the next key shows
    # whether it starts the terminator.
    escape = text.endswith("\x1b")
    text = text.removesuffix("\x1b")
    if _clock() - since > OSC_CARRY_SECONDS:
        _push_back(win, ["\x1b", value] if escape else [value])  # the reply never finished: this is input
        return
    while True:
        if escape:
            if value != "\\":
                _push_back(win, ["\x1b", value])  # not a terminator: the Esc was a key, then this one
            return
        if not isinstance(value, str):
            # A resize or a function key: delivered, the reply may still follow.
            _carry_reply(text, since=since)
            _push_back(win, [value])
            return
        progress = _reply_progress(text + value)
        if progress == "complete":
            return
        if progress == "invalid":
            _push_back(win, [value])
            return
        if value == "\x1b":
            escape = True
        else:
            text += value
        value = _peek(win)
        if value is None:
            if escape:
                _push_back(win, ["\x1b"])  # an Esc no backslash follows is a key
            else:
                _carry_reply(text, since=_clock())  # paused inside its body: read on at the next key
            return


def read_key(win: Any) -> Key:
    """Read one key via ``get_wch`` (unicode-safe) and normalize it.

    Note: ncurses deliberately delivers an Alt-chord as ESC followed by its
    tail character (ESCDELAY disambiguation applies only to keypad function
    sequences). Any Python-level re-merging heuristic misclassifies fast
    human input as chords, so screens treat ESC as cancel and an immediately
    following printable as fresh input — terminal-standard behavior. The
    curses session sets a 25 ms escape delay (unless ``ESCDELAY`` is set), so
    a lone Esc is delivered promptly. The one exception is a terminal's late
    reply to the background colour query (:func:`_swallow_osc_reply`): it is
    read as a whole and never reaches a screen as Esc and typed text, also
    when its rest arrives after the reader that read its start stopped
    waiting, in as many pieces as it takes (:func:`_finish_carried_reply`).
    """

    while _REPLY_TAIL and not _pending(win):
        _finish_carried_reply(win)
    value = _next_value(win)
    if value == "\x1b" and _swallow_osc_reply(win):
        return read_key(win)
    if isinstance(value, str):
        if value in ("\n", "\r"):
            return Key("enter")
        if value == "\t":
            return Key("tab")
        if value == "\x1b":
            return Key("esc")
        if value == "\x7f":
            return Key("backspace")
        if len(value) == 1 and ord(value) < 32:
            return Key("ctrl", chr(ord(value) + 96))
        return Key("char", value)
    kind = _key_kinds().get(value)
    if kind is None:
        return Key("unknown", str(value))
    return Key(kind)


def safe_add(win: Any, row: int, col: int, text: str, attr: int = 0) -> None:
    """Bounds-checked addstr; writing the last cell raises in curses, so clip.

    The text is sanitized with :func:`visible_text`: this is the single
    curses drawing helper, so no external control bytes ever reach the
    terminal through a widget. Styling travels via curses attributes, never
    via embedded ANSI, so sanitizing the text cannot mangle launcher styling.
    """

    try:
        height, width = win.getmaxyx()
    except (AttributeError, CursesError):
        return
    if row < 0 or row >= height or col >= width - 1:
        return
    if col < 0:
        text = text[-col:]
        col = 0
    clipped = visible_text(text)[: max(0, width - col - 1)]
    if not clipped:
        return
    try:
        win.addstr(row, col, clipped, attr)
    except CursesError:
        pass


@dataclass
class Label:
    """Static text line with an optional palette role."""

    text: str
    role: str = "normal"

    def draw(self, win: Any, row: int, col: int, width: int, palette: Palette) -> None:
        safe_add(win, row, col, self.text, palette.attr(self.role))


@dataclass
class Badge:
    """Colored status token; the words themselves are the text form."""

    text: str
    role: str = "accent"

    def draw(self, win: Any, row: int, col: int, palette: Palette) -> int:
        safe_add(win, row, col, self.text, palette.attr(self.role))
        return len(self.text)


class KeyBar:
    """Footer hint bar: ``Enter launch · E edit · Esc cancel`` with accented keys.

    Draws at ``col 2``; when the bindings do not fit one row the bar wraps
    upward (using the row above the given one) instead of clipping keys —
    the exit binding is always fully visible.
    """

    def __init__(self, bindings: Sequence[tuple[str, str]]):
        self.bindings = list(bindings)

    def text(self) -> str:
        return " · ".join(f"{key} {label}" for key, label in self.bindings)

    def _rows_needed(self, width: int) -> int:
        col = 2
        rows = 1
        for index, (key, label) in enumerate(self.bindings):
            entry = len(key) + 1 + len(label) + (3 if index else 0)
            if col + entry > width - 2 and index:
                rows += 1
                col = 2 + len(key) + 1 + len(label)
                continue
            col += entry
        return rows

    def rows(self, width: int) -> int:
        """Rows the bar will occupy when drawn (capped at 2, wrapping upward)."""

        return min(self._rows_needed(width), 2)

    def draw(self, win: Any, row: int, palette: Palette, col: int = 2) -> None:
        height, width = win.getmaxyx()
        used = self.rows(width)
        if self._rows_needed(width) > used:
            # Overflow beyond two rows: the contract is that the exit
            # binding is never clipped; the help binding (? — the discovery
            # mechanism) shares the guaranteed bottom-row spot when the
            # screen put it immediately before the exit. Middle bindings
            # compact behind an ellipsis.
            origin = max(0, row - (used - 1))
            x = col
            index = 0
            protected = (
                self.bindings[-2:]
                if len(self.bindings) >= 2 and self.bindings[-2][0] == "?"
                else self.bindings[-1:]
            )
            body = self.bindings[: -len(protected)]
            while index < len(body):
                key, label = body[index]
                entry = len(key) + 1 + len(label) + (3 if index else 0)
                if index and x + entry > width - 2:
                    break
                if index:
                    safe_add(win, origin, x, " · ", palette.attr("dim"))
                    x += 3
                safe_add(win, origin, x, key, palette.attr("accent"))
                x += len(key)
                safe_add(win, origin, x, f" {label}", palette.attr("normal"))
                x += 1 + len(label)
                index += 1
            tail = "… · " if index < len(body) else ""
            safe_add(win, origin + 1, col, tail, palette.attr("dim"))
            x = col + len(tail)
            for offset, (key, label) in enumerate(protected):
                if offset:
                    safe_add(win, origin + 1, x, " · ", palette.attr("dim"))
                    x += 3
                safe_add(win, origin + 1, x, key, palette.attr("accent"))
                x += len(key)
                safe_add(win, origin + 1, x, f" {label}", palette.attr("normal"))
                x += 1 + len(label)
            return
        origin = max(0, row - (used - 1))
        current = origin
        for index, (key, label) in enumerate(self.bindings):
            entry = len(key) + 1 + len(label) + (3 if index else 0)
            if index and col + entry > width - 2 and current < origin + used - 1:
                current += 1
                col = 2
            elif index:
                safe_add(win, current, col, " · ", palette.attr("dim"))
                col += 3
            safe_add(win, current, col, key, palette.attr("accent"))
            col += len(key)
            safe_add(win, current, col, f" {label}", palette.attr("normal"))
            col += 1 + len(label)
            if col >= width - 2 and current >= row:
                break


@dataclass
class Checkbox:
    """Single checkbox: ``[x] label``; radio flavor renders ``(•)``/``( )``."""

    label: str
    checked: bool = False
    enabled: bool = True
    radio: bool = False
    note: str = ""

    def marker(self) -> str:
        if self.radio:
            return "(•)" if self.checked else "( )"
        return "[x]" if self.checked else "[ ]"

    def draw(
        self,
        win: Any,
        row: int,
        col: int,
        palette: Palette,
        *,
        focused: bool = False,
        option_cursor: bool = False,
    ) -> int:
        attr = palette.attr("normal")
        if not self.enabled:
            attr = palette.attr("dim")
        if focused or option_cursor:
            attr |= curses.A_REVERSE
        safe_add(win, row, col, self.marker(), palette.attr("accent") | (curses.A_REVERSE if option_cursor else 0))
        safe_add(win, row, col + 4, self.label, attr)
        used = 4 + len(self.label)
        if self.note:
            safe_add(win, row, col + used, f" {self.note}", palette.attr("dim"))
            used += 1 + len(self.note)
        return used


class CheckboxGroup:
    """A focusable group of checkboxes; radio groups keep exactly one checked."""

    def __init__(self, boxes: Sequence[Checkbox], *, horizontal: bool = False):
        self.boxes = list(boxes)
        self.horizontal = horizontal
        self.cursor = 0

    def handle(self, key: Key) -> bool:
        """Move within the group / activate; True when the key was consumed."""

        move_keys = ("left", "right") if self.horizontal else ("up", "down")
        if key.kind == move_keys[0]:
            self.cursor = (self.cursor - 1) % max(1, len(self.boxes))
            return True
        if key.kind == move_keys[1]:
            self.cursor = (self.cursor + 1) % max(1, len(self.boxes))
            return True
        if key.kind in ("char",) and key.ch == " " or key.kind == "enter":
            self.activate(self.cursor)
            return True
        return False

    def activate(self, index: int) -> None:
        if not 0 <= index < len(self.boxes):
            return
        box = self.boxes[index]
        if not box.enabled:
            return
        if box.radio:
            for other in self.boxes:
                other.checked = False
            box.checked = True
        else:
            box.checked = not box.checked

    def checked_indexes(self) -> list[int]:
        return [index for index, box in enumerate(self.boxes) if box.checked]


class TextInput:
    """Single-line edit input with a real cursor.

    Handles printable insertion plus left/right/home/end/backspace/delete;
    navigation keys (up/down/tab/enter/esc) are returned unconsumed so the
    parent form can move focus.
    """

    def __init__(
        self,
        value: str = "",
        *,
        max_length: int | None = None,
        mask: str | None = None,
    ):
        self.value = value
        self.cursor = len(value)
        self.max_length = max_length
        # Render-only echo replacement for secret entry: the value is
        # drawn as mask glyphs and never reaches a rendered frame.
        self.mask = mask
        self.offset = 0  # horizontal scroll: first visible character index

    @staticmethod
    def _combining(char: str) -> bool:
        return unicodedata.category(char) in ("Mn", "Me")

    def _previous(self, index: int) -> int:
        index = max(0, index - 1)
        while index > 0 and self._combining(self.value[index]):
            index -= 1
        return index

    def _next(self, index: int) -> int:
        index = min(len(self.value), index + 1)
        while index < len(self.value) and self._combining(self.value[index]):
            index += 1
        return index

    def _echo(self, text: str) -> str:
        return self.mask * len(text) if self.mask is not None else text

    def _width(self, text: str) -> int:
        # Curses uses display cells: combining marks take none, CJK takes
        # two. Controls use the same inert notation as every drawing helper.
        echo = visible_text(self._echo(text))
        cells = sum(0 if self._combining(char) else
                    2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                    for char in echo)
        # An unattached leading mark is drawn on a space, not on the chrome.
        return cells + (1 if echo and self._combining(echo[0]) else 0)

    def handle(self, key: Key) -> bool:
        if key.kind == "char" and key.ch:
            if key.ch == " " or key.ch.isprintable():
                if self.max_length is None or len(self.value) < self.max_length:
                    self.value = (
                        self.value[: self.cursor] + key.ch + self.value[self.cursor :]
                    )
                    self.cursor += len(key.ch)
                return True
            return False
        if key.kind == "left":
            self.cursor = self._previous(self.cursor)
            return True
        if key.kind == "right":
            self.cursor = self._next(self.cursor)
            return True
        if key.kind == "home":
            self.cursor = 0
            return True
        if key.kind == "end":
            self.cursor = len(self.value)
            return True
        if key.kind == "backspace":
            if self.cursor > 0:
                previous = self._previous(self.cursor)
                self.value = self.value[:previous] + self.value[self.cursor:]
                self.cursor = previous
            return True
        if key.kind == "delete":
            if self.cursor < len(self.value):
                self.value = self.value[:self.cursor] + self.value[self._next(self.cursor):]
            return True
        return False

    def _visible(self, inner_width: int) -> str:
        """A whole-cell slice, including room for the insertion point at end."""

        if inner_width <= 0:
            return ""
        self.offset = min(self.offset, self.cursor)
        needed = self._width(self.value[self.cursor:self._next(self.cursor)]) or 1
        before = self._width(self.value[self.offset:self.cursor])
        while self.offset < self.cursor and before + needed > inner_width:
            next_offset = self._next(self.offset)
            before -= self._width(self.value[self.offset:next_offset])
            self.offset = next_offset
        end, used = self.offset, 0
        while end < len(self.value):
            next_end = self._next(end)
            cells = self._width(self.value[end:next_end])
            if used + cells > inner_width:
                break
            used += cells
            end = next_end
        return self.value[self.offset:end]

    def draw_text(
        self, win: Any, row: int, col: int, width: int, *, attr: int = 0,
        highlight_cursor: bool = False, end_marker: str = " ",
    ) -> tuple[int, int]:
        """Draw the editable viewport; return its actual insertion cell.

        The highlighted cell is a fallback for terminals without curs_set.
        Draw combining sequences together and never cut a wide glyph in half.
        """

        visible = self._visible(width)
        safe_add(win, row, col, " " * width, attr)
        echo = self._echo(visible)
        if echo and self._combining(echo[0]):
            echo = " " + echo
        stop = self.offset + len(visible)
        if len(echo) <= width:
            safe_add(win, row, col, echo, attr)
        else:
            # Many combining marks can exceed safe_add's character-count
            # clip even though their display cells fit. Draw whole units.
            index, x = self.offset, col
            while index < stop:
                next_index = self._next(index)
                text = self.value[index:next_index]
                echo = self._echo(text)
                if echo and self._combining(echo[0]):
                    echo = " " + echo
                safe_add(win, row, x, echo, attr)
                x += self._width(text)
                index = next_index
        position = row, col + self._width(self.value[self.offset:self.cursor])
        if highlight_cursor:
            text = self.value[self.cursor:self._next(self.cursor)] if self.cursor < stop else ""
            echo = self._echo(text) or end_marker
            if self._combining(echo[0]):
                echo = " " + echo
            safe_add(win, *position, echo, attr | curses.A_REVERSE)
        return position

    def draw(
        self,
        win: Any,
        row: int,
        col: int,
        width: int,
        palette: Palette,
        *,
        focused: bool = False,
    ) -> tuple[int, int]:
        """Draw ``[value]`` in ``width`` columns; returns the cursor (y, x)."""

        inner = max(1, width - 2)
        attr = palette.attr("normal") | (curses.A_REVERSE if focused else 0)
        safe_add(win, row, col, "[", palette.attr("dim"))
        position = self.draw_text(win, row, col + 1, inner, attr=attr)
        safe_add(win, row, col + 1 + inner, "]", palette.attr("dim"))
        return position


@dataclass
class SelectItem:
    label: str
    checked: bool | None = None  # None: plain row; bool: multi-select row
    enabled: bool = True
    note: str = ""
    # A non-selectable group label drawn dim-bold; navigation
    # skips it like a disabled row.
    header: bool = False

    @property
    def selectable(self) -> bool:
        return self.enabled and not self.header


class SelectList:
    """Scrollable option list; single-select or multi (checkbox rows).

    ``on_activate(index)`` (single mode, Enter) may return an error string to
    stay open; ``on_toggle``/``on_prefer`` (multi mode) likewise.  ``refresh``
    rebuilds the item list after every mutation so checked markers track
    state.  Returns the activated index (single mode) or None.

    Title, rows and message draw at column 2.  Esc is
    the only exit key (no ``q``).  Up/down/Home/End skip header and
    disabled rows; a list with no selectable row never activates.
    """

    def __init__(
        self,
        title: str,
        items: Sequence[SelectItem],
        *,
        footer: Sequence[tuple[str, str]] = (("Enter", "choose"), ("Esc", "back")),
        multi: bool = False,
        selected: int = 0,
    ):
        self.title = title
        self.items = list(items)
        self.footer = KeyBar(footer)
        self.multi = multi
        self.selected = selected
        self.message = ""
        # Multi-mode toggle state, tracked by the widget itself so callers
        # without an on_toggle callback still get a usable selection:
        # run() returns the sorted toggled indexes on Enter, None on Esc.
        self.toggled: set[int] = set()

    def _selectable(self, index: int) -> bool:
        return 0 <= index < len(self.items) and self.items[index].selectable

    def _settle(self) -> None:
        """Clamp the selection and move it off a header/disabled row (forward first)."""

        self.selected = min(max(self.selected, 0), max(0, len(self.items) - 1))
        if not self.items or self._selectable(self.selected):
            return
        count = len(self.items)
        for step in range(1, count):
            forward = self.selected + step
            if forward < count and self._selectable(forward):
                self.selected = forward
                return
            backward = self.selected - step
            if backward >= 0 and self._selectable(backward):
                self.selected = backward
                return

    def _step(self, direction: int) -> None:
        """Move to the next selectable row in ``direction``, wrapping; stay when none."""

        count = len(self.items)
        for step in range(1, count + 1):
            candidate = (self.selected + direction * step) % max(1, count)
            if self._selectable(candidate):
                self.selected = candidate
                return

    def _edge(self, last: bool) -> None:
        indexes = range(len(self.items) - 1, -1, -1) if last else range(len(self.items))
        for index in indexes:
            if self._selectable(index):
                self.selected = index
                return

    def draw(self, win: Any, palette: Palette) -> None:
        win.erase()
        height, width = win.getmaxyx()
        safe_add(win, 0, 2, self.title, palette.attr("accent") | curses.A_BOLD)
        visible = max(1, height - 5)
        self._settle()
        start = max(
            0, min(self.selected - visible + 1, max(0, len(self.items) - visible))
        )
        for screen_row, index in enumerate(
            range(start, min(len(self.items), start + visible)), 2
        ):
            item = self.items[index]
            if item.header:
                safe_add(
                    win, screen_row, 2, item.label, palette.attr("dim") | curses.A_BOLD
                )
                continue
            focused = index == self.selected
            attr = palette.attr("normal") | (curses.A_REVERSE if focused else 0)
            if not item.enabled:
                attr = palette.attr("dim") | (curses.A_REVERSE if focused else 0)
            marker = ""
            checked = item.checked
            if checked is None and self.multi:
                # Widget-tracked toggles render too: a multi picker without
                # item-level checked state (no refresh callback) must never
                # toggle invisibly (the fetch-mark picker).
                checked = index in self.toggled
            if checked is not None:
                marker = "[x] " if checked else "[ ] "
            cursor = ">" if focused else " "
            safe_add(win, screen_row, 2, f"{cursor} {marker}{item.label}", attr)
            if item.note:
                safe_add(
                    win,
                    screen_row,
                    4 + len(marker) + len(item.label),
                    f"  {item.note}",
                    palette.attr("dim") | (curses.A_REVERSE if focused else 0),
                )
        bar_rows = self.footer.rows(width)
        if self.message:
            safe_add(win, height - 1 - bar_rows, 2, self.message, palette.attr("warn"))
        self.footer.draw(win, height - 1, palette)
        win.refresh()

    def run(
        self,
        win: Any,
        palette: Palette,
        *,
        on_activate: Callable[[int], str | None] | None = None,
        on_toggle: Callable[[int], str | None] | None = None,
        on_prefer: Callable[[int], str | None] | None = None,
        on_help: Callable[[], None] | None = None,
        refresh: Callable[[], Sequence[SelectItem]] | None = None,
        shortcuts: Mapping[str, str] | None = None,
        help_text: str = "",
    ) -> int | list[int] | None:
        # Single mode: Enter returns the index (or None when empty/back).
        # Multi mode: Enter returns the sorted toggled indexes (possibly
        # empty), Esc returns None. Callback callers may ignore the return.
        while True:
            if refresh is not None:
                self.items = list(refresh())
            self.draw(win, palette)
            key = read_key(win)
            self.message = ""
            if key.kind == "char" and shortcuts and key.ch in shortcuts:
                return shortcuts[key.ch]
            if key.kind == "char" and key.ch == "?" and help_text:
                TextView(self.title + " — help", help_text.splitlines(), palette=palette).run(win)
                continue
            if key.kind == "resize":
                continue
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self._step(-1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self._step(1)
            elif key.kind == "home":
                self._edge(last=False)
            elif key.kind == "end":
                self._edge(last=True)
            elif key.kind == "esc":
                return None
            elif key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            elif key.kind == "enter":
                if self.multi:
                    return sorted(self.toggled)
                if not self.items:
                    return None
                if not self._selectable(self.selected):
                    # Only header/disabled rows: nothing to activate.
                    continue
                if on_activate is not None:
                    error = on_activate(self.selected)
                    if error:
                        self.message = error
                        continue
                return self.selected
            elif key.kind == "char" and key.ch == " " and self.multi:
                if not self.items or self.items[self.selected].header:
                    continue
                self.toggled ^= {self.selected}
                if on_toggle is not None:
                    error = on_toggle(self.selected)
                    if error:
                        self.message = error
            elif key.kind == "char" and key.ch in ("p", "P") and self.multi:
                if on_prefer is not None and self.items:
                    error = on_prefer(self.selected)
                    if error:
                        self.message = error
            elif key.kind == "char" and key.ch == "?" and on_help is not None:
                on_help()


_FIRST = object()


class Modal:
    """Centered dialog with focused buttons; Esc cancels (returns None).

    With ``input`` set, a TextInput sits above the buttons; Tab (and
    shift-Tab) cycles focus between the input and the buttons, and the
    caller reads ``modal.input.value`` after a confirming button returns.
    """

    def __init__(
        self,
        title: str,
        lines: Sequence[str] = (),
        *,
        buttons: Sequence[tuple[str, Any]] = (("OK", True), ("Cancel", False)),
        input: TextInput | None = None,
        default: Any = _FIRST,
    ):
        self.title = title
        self.lines = list(lines)
        self.buttons = list(buttons)
        self.input = input
        # 0 input, 1..n buttons; ``default`` names the value of the button
        # focused first (a destructive dialog focuses the one that cancels).
        values = [value for _label, value in self.buttons]
        if input is not None:
            self.focus = 0
        elif default is not _FIRST and default in values:
            self.focus = 1 + values.index(default)
        else:
            self.focus = 1

    def _geometry(self, win: Any) -> tuple[int, int, int, int]:
        height, width = win.getmaxyx()
        buttons_width = sum(len(label) + 4 for label, _ in self.buttons) + 2
        content = max(
            [len(self.title), buttons_width]
            + [len(line) for line in self.lines]
            + ([30] if self.input is not None else [])
        )
        box_w = min(width - 2, content + 6)
        body = len(self.lines) + (2 if self.input is not None else 0) + 2
        box_h = min(height, body + 4)
        top = max(0, (height - box_h) // 2)
        left = max(0, (width - box_w) // 2)
        return top, left, box_h, box_w

    def draw(self, win: Any, palette: Palette) -> None:
        top, left, box_h, box_w = self._geometry(win)
        # Paint the full interior first: underlying screen text must never
        # bleed through the dialog.
        blank = " " * (box_w - 2)
        for row in range(top + 1, top + box_h - 1):
            safe_add(win, row, left + 1, blank)
        horizontal = "+" + "-" * (box_w - 2) + "+"
        safe_add(win, top, left, horizontal, palette.attr("accent"))
        for row in range(top + 1, top + box_h - 1):
            safe_add(win, row, left, "|", palette.attr("accent"))
            safe_add(win, row, left + box_w - 1, "|", palette.attr("accent"))
        safe_add(win, top + box_h - 1, left, horizontal, palette.attr("accent"))
        safe_add(win, top + 1, left + 2, self.title, palette.attr("accent") | curses.A_BOLD)
        row = top + 2
        # Clamp the body slice: below box_h 5 the naive slice goes negative
        # and would index from the list's END, showing the wrong lines on
        # tiny terminals.
        for line in self.lines[: max(0, box_h - 5)]:
            safe_add(win, row, left + 2, line)
            row += 1
        if self.input is not None:
            cursor = self.input.draw(
                win, row, left + 2, box_w - 4, palette, focused=self.focus == 0
            )
            if self.focus == 0:
                try:
                    win.move(*cursor)
                except CursesError:
                    pass
            row += 2
        col = left + 2
        for index, (label, _value) in enumerate(self.buttons):
            focused = self.focus == index + 1
            attr = palette.attr("accent") | (curses.A_REVERSE if focused else 0)
            safe_add(win, top + box_h - 2, col, f"[ {label} ]", attr)
            col += len(label) + 5
        win.refresh()

    def run(
        self,
        win: Any,
        palette: Palette,
        background: Callable[[Any], None] | None = None,
    ) -> Any:
        while True:
            self.draw(win, palette)
            key = read_key(win)
            if key.kind == "resize":
                # The old frame must not linger at its previous geometry, but
                # the screen behind should not stay blank either: repaint the
                # parent first (when given), then redraw the dialog fresh.
                if background is not None:
                    background(win)
                else:
                    win.erase()
                continue
            if key.kind == "esc":
                return None
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind in ("tab", "btab"):
                step = 1 if key.kind == "tab" else -1
                if self.input is not None:
                    # Zone 0 is the input, 1..n the buttons.
                    self.focus = (self.focus + step) % (len(self.buttons) + 1)
                elif self.buttons:
                    # Buttons only: every button is reachable, none is skipped.
                    self.focus = 1 + (self.focus - 1 + step) % len(self.buttons)
                continue
            if self.focus == 0 and self.input is not None:
                if key.kind == "enter" or key.kind == "down":
                    self.focus = 1
                    continue
                self.input.handle(key)
                continue
            if key.kind == "left":
                self.focus = 1 + (self.focus - 2) % len(self.buttons)
                continue
            if key.kind == "right":
                self.focus = 1 + self.focus % len(self.buttons)
                continue
            if key.kind == "enter":
                index = self.focus - 1
                if 0 <= index < len(self.buttons):
                    return self.buttons[index][1]


class Table:
    """Selectable row table with aligned columns.

    Keyword extensions, each defaulting to the plain table:
    ``gap`` (spaces between columns), ``row_roles`` (a palette role per
    row), ``spans`` (row index -> ``(text, role)`` drawn full-width instead
    of cells, never selectable), ``cursor`` (``› `` drawn before the selected
    row; the column block starts two cells right) and ``selectable`` (per
    row).  :meth:`handle` skips spans and non-selectable rows.
    """

    def __init__(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        selected: int = 0,
        max_col_width: int = 36,
        min_widths: Sequence[int] | None = None,
        gap: int = 3,
        row_roles: Sequence[str | None] | None = None,
        spans: Mapping[int, tuple[str, str]] | None = None,
        cursor: bool = False,
        selectable: Sequence[bool] | None = None,
    ):
        self.columns = list(columns)
        self.rows = [list(row) for row in rows]
        self.selected = selected
        self.max_col_width = max_col_width
        self.min_widths = (
            list(min_widths) if min_widths is not None else [8] * len(self.columns)
        )
        self.gap = gap
        self.row_roles = list(row_roles) if row_roles is not None else None
        self.spans = dict(spans) if spans is not None else {}
        self.cursor = cursor
        self.selectable = list(selectable) if selectable is not None else None

    def is_selectable(self, index: int) -> bool:
        """A row the selection may rest on: in range, not a span, not disabled."""

        if not 0 <= index < len(self.rows) or index in self.spans:
            return False
        if self.selectable is not None and index < len(self.selectable):
            return bool(self.selectable[index])
        return True

    def _widths(self, total: int) -> list[int]:
        natural = []
        cell_rows = [row for i, row in enumerate(self.rows) if i not in self.spans]
        for index, column in enumerate(self.columns):
            cells = [len(row[index]) for row in cell_rows if index < len(row)]
            natural.append(min(self.max_col_width, max([len(column), *cells, 1])))
        over = sum(natural) + self.gap * (len(natural) - 1) - total
        while over > 0:
            candidates = [
                i for i in range(len(natural)) if natural[i] > self.min_widths[i]
            ]
            if not candidates:
                break
            widest = max(candidates, key=lambda i: natural[i])
            natural[widest] -= 1
            over -= 1
        return natural

    def _role(self, index: int) -> str:
        if self.row_roles is not None and index < len(self.row_roles):
            return self.row_roles[index] or "normal"
        return "normal"

    def draw(
        self,
        win: Any,
        row: int,
        col: int,
        width: int,
        palette: Palette,
        *,
        max_rows: int | None = None,
        start: int = 0,
    ) -> int:
        """Draw the header and rows ``start`` … ``start + max_rows``; returns rows used.

        Column widths always come from every row, so a scrolled window
        (``start`` > 0) never shifts the columns.
        """

        block = col + (2 if self.cursor else 0)
        widths = self._widths(width - block)
        separator = " " * self.gap
        header = separator.join(
            column.ljust(widths[i])[: widths[i]] for i, column in enumerate(self.columns)
        )
        safe_add(win, row, block, header, palette.attr("dim") | curses.A_BOLD)
        stop = len(self.rows) if max_rows is None else start + max_rows
        shown = self.rows[start:stop]
        for offset, cells in enumerate(shown, 1):
            index = start + offset - 1
            if index in self.spans:
                text, role = self.spans[index]
                safe_add(win, row + offset, col, text, palette.attr(role))
                continue
            line = separator.join(
                (cells[i] if i < len(cells) else "").ljust(widths[i])[: widths[i]]
                for i in range(len(self.columns))
            )
            focused = index == self.selected and self.is_selectable(index)
            attr = palette.attr(self._role(index)) | (curses.A_REVERSE if focused else 0)
            if self.cursor and focused:
                safe_add(win, row + offset, col, "› ", palette.attr("accent"))
            safe_add(win, row + offset, block, line, attr)
        return 1 + len(shown)

    def _step(self, direction: int) -> bool:
        count = len(self.rows)
        for step in range(1, count + 1):
            candidate = (self.selected + direction * step) % count
            if self.is_selectable(candidate):
                self.selected = candidate
                return True
        return True

    def _edge(self, last: bool) -> bool:
        indexes = range(len(self.rows) - 1, -1, -1) if last else range(len(self.rows))
        for index in indexes:
            if self.is_selectable(index):
                self.selected = index
                break
        return True

    def handle(self, key: Key) -> bool:
        if not self.rows:
            return False
        if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
            return self._step(-1)
        if key.kind == "down" or (key.kind == "char" and key.ch == "j"):
            return self._step(1)
        if key.kind == "home":
            return self._edge(last=False)
        if key.kind == "end":
            return self._edge(last=True)
        return False


# ---------------------------------------------------------------------------
# Curses entry discipline: fds pointed at the caller's streams, always restored.


def _fileno(stream: Any) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def run_curses_on_streams(
    app: Callable[[Any], Any],
    input_stream: Any,
    output_stream: Any,
    *,
    palette: Palette | None = None,
) -> Any:
    """Run ``app(stdscr)`` on the terminal behind the given streams.

    curses always initializes on fds 0/1, so when the caller's streams are
    other tty handles (typically /dev/tty) they are dup2'ed onto 0/1 for the
    duration and restored afterwards, unconditionally.  ``curses.wrapper``
    performs its own terminal-mode restore; KeyboardInterrupt propagates.

    Inside the session the escape delay is 25 ms (unless the
    environment sets ``ESCDELAY``; the environment is never written) and
    terminal flow control (``IXON``) is off, so ``^S`` reaches the screens
    instead of freezing output.  The caller's terminal attributes are
    snapshotted after the dup2 block and restored on fd 0 before the fds
    are restored, so ``IXON`` comes back exactly as it was.
    """

    if curses is None:
        # Unreachable through streams_curses_capable; OSError is what every
        # caller already treats as "fall back to the line flow".
        raise OSError("curses is not available in this Python")
    in_fd = _fileno(input_stream)
    out_fd = _fileno(output_stream)
    if in_fd is None or out_fd is None:
        raise OSError("curses requires streams backed by real file descriptors")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    saved: list[tuple[int, int]] = []
    saved_attrs: list[Any] | None = None
    try:
        if in_fd != 0:
            saved.append((0, os.dup(0)))
            os.dup2(in_fd, 0)
        if out_fd != 1:
            saved.append((1, os.dup(1)))
            os.dup2(out_fd, 1)
        # cf[10]: snapshot the caller's terminal only now that fd 0 is it.
        if termios is not None:
            try:
                if os.isatty(0):
                    saved_attrs = termios.tcgetattr(0)
            except termios.error:
                saved_attrs = None

        def _wrapped(stdscr: Any) -> Any:
            if palette is not None:
                init_curses_colors(palette)
            if "ESCDELAY" not in os.environ:
                try:
                    curses.set_escdelay(ESC_DELAY_MS)
                except (AttributeError, CursesError):
                    pass
            if termios is not None:
                try:
                    attrs = termios.tcgetattr(0)
                    if attrs[0] & termios.IXON:
                        attrs[0] &= ~termios.IXON
                        termios.tcsetattr(0, termios.TCSANOW, attrs)
                        # suspended_curses/reset_prog_mode keep IXON off.
                        curses.def_prog_mode()
                except (termios.error, CursesError):
                    pass
            stdscr.keypad(True)
            return app(stdscr)

        return curses.wrapper(_wrapped)
    finally:
        if saved_attrs is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, saved_attrs)
            except termios.error:
                pass
        for fd, duplicate in saved:
            os.dup2(duplicate, fd)
            os.close(duplicate)


@contextlib.contextmanager
def suspended_curses(win: Any) -> Iterator[None]:
    """Suspend curses around an external program ($EDITOR) and restore.

    Degrades to a plain output region when curses is not active (tests,
    line mode): the terminal state is managed only when there is a curses
    session to manage.
    """

    if curses is None:
        yield
        return
    try:
        curses.def_prog_mode()
        curses.endwin()
    except CursesError:
        yield
        return
    try:
        yield
    finally:
        curses.reset_prog_mode()
        try:
            win.refresh()
        except CursesError:
            pass


def hide_cursor() -> None:
    if curses is None:
        return
    try:
        curses.curs_set(0)
    except CursesError:
        pass


def show_cursor() -> None:
    if curses is None:
        return
    try:
        curses.curs_set(1)
    except CursesError:
        pass


# ===========================================================================
# The profile editor stack.
#
# The stack
# imports ``profile``, ``settings`` and ``views`` only (never ``cli``,
# ``lineup``, ``sessions`` or ``launch``); every store write goes through the
# callbacks the cli glue passes in (:class:`ProfileEditorCallbacks`), so no
# screen here writes anything itself.

PROFILE_EDITOR_HELP = (
    "↑/↓ move. Enter changes the row: General (name, description, primary provider,\n"
    "lead providers, compaction override), Lead and agents (model picker; ← → picks\n"
    "the effort), Native (Explore, Plan, general-purpose, workflows), Review (routing).\n"
    "U unbinds an agent. N edits named bindings. R shows the review routing table.\n"
    "^S saves (^O too); only a valid profile saves. ^G edits the raw JSON in $EDITOR.\n"
    "Checks: ✓ valid · ! warning (never blocks) · ✗ error (Enter jumps to its field).\n"
    "◆ marks a per-profile override of a Settings value. Esc goes back."
)
PROFILE_EDITOR_KEYBAR = (
    ("Enter", "change"),
    ("U", "unbind"),
    ("N", "named bindings"),
    ("R", "routing"),
    ("^S", "save"),
    ("?", "help"),
    ("Esc", "back"),
)
PROFILE_EDITOR_MIN_COLS = 60
# §3.4: top, header, rule, General, Lead, blank, Agents header, blank, Native,
# Review, Checks, message (+ at least three agent rows).
PROFILE_EDITOR_CHROME = 12
PROFILE_FORM_DISCARD_TITLE = "Discard unsaved changes?"
PROFILE_FORM_DISCARD_BODY = "The profile was modified; unsaved edits will be lost."
PROFILE_FORM_NEW_NAME_TITLE = "Save under a new name?"
PROFILE_FORM_NEW_NAME_BODY = "The name changed: {old} → {new}."
# A shipped profile keeps its name: a new name saves a copy.
PROFILE_FORM_SEED_SAVE_BUTTONS = (("Save as new", "new"), ("Cancel", None))
# The profile changed on disk while it was being edited.
PROFILE_FORM_CHANGED_TITLE = "{name} changed since you opened it"
PROFILE_FORM_CHANGED_BODY = ("Another window or command saved {name} while you were editing. Overwrite keeps "
                             "your version; Reload shows the saved one (your edits are lost).")
PROFILE_FORM_CHANGED_BUTTONS = (("Cancel", None), ("Reload", "reload"), ("Overwrite", "overwrite"))
PROFILE_FORM_RELOADED = "reloaded {name} as it is saved now"
# Two profiles that are the fallback for the same provider.
PROFILE_FORM_DUP_FALLBACK = "! {other} is also the fallback for {provider} — keep one (General → primary provider)"
PROFILE_FORM_FIX_ERRORS = "fix {n} error(s) before saving"
PROFILE_FORM_LEAD_UNBIND = "the lead cannot be unbound"
PROFILE_FORM_JSON_NOT_APPLIED = "JSON not applied: {error}"
PROFILE_FORM_SAVED = "saved {name}"
PROFILE_FORM_GENERAL_TITLE = "edit profile — general"
PROFILE_FORM_GENERAL_KEYBAR = (("Enter", "change"), ("^S", "apply"), ("?", "help"), ("Esc", "cancel"))
PROFILE_FORM_GENERAL_HELP = (
    "↑/↓ move between the fields; printable keys edit the name, the description and "
    "the compaction override.\n\n"
    "Enter on primary provider picks one provider (none = no primary). Enter on lead "
    "providers checks the providers the lead may use (none checked = any provider).\n\n"
    "The compaction override is a whole percent from {low} to {high}; leave it empty to "
    "use the Settings value ({percent} %). ^S (or Enter on Apply) applies the changes to "
    "the profile being edited; Esc discards them."
)
PROFILE_FORM_NATIVE_TITLE = "edit profile — native agents & workflows"
PROFILE_FORM_NATIVE_KEYBAR = (("← →", "choose"), ("Enter", "apply"), ("?", "help"), ("Esc", "cancel"))
PROFILE_FORM_NATIVE_HELP = (
    "↑/↓ move between the groups; ← → choose a value. Explore: replace (cm-explorer "
    "answers Explore calls), native or off. Plan: native or off. general-purpose: on or "
    "off. workflows: native or off. Enter (or ^S) applies; Esc discards.\n\n"
    "Workflow mode is a relaunch field: a running session changes it only at its next "
    "resume."
)
BINDING_PICKER_KEYBAR = (("← →", "effort"), ("Enter", "choose"), ("V", "details"), ("?", "help"), ("Esc", "back"))
BINDING_PICKER_HELP = (
    "↑/↓ move; ← → pick effort; Enter binds the model. Single-selector client lines "
    "offer supported native efforts (undeclared efforts warn); gateway efforts need "
    "an existing selector mapping. ultracode is the lead's; a client-effort workflow "
    "default can use only its declared default.\n\n"
    "All valid lines appear, including New, legacy custom and operator models (◇). "
    "Admission badges, qualification and role recommendations do not block a binding. "
    "Providers switched off in G and unusable routes are dimmed with remedies. "
    "V shows full details. The actual shared client window may exceed a provider's "
    "bound; a smaller declaration does not create a per-agent window. "
    "A named row binds the slot to that named binding, so editing the binding moves "
    "every slot that uses it."
)
BINDING_PICKER_MIN_COLS = 50
ROUTING_KEYBAR = (("?", "help"), ("Esc", "back"))
ROUTING_LEGEND = (
    "✓ recognized cross-family   ≈ recognized same-family   ? independence unknown   "
    "° preferred reviewer shares the author's family"
)
ROUTING_HELP = (
    "Who reviews whose change. A normal change goes to the plain reviewer; a "
    "high-stakes change to the strong one. A reviewer of a recognized different family "
    "is preferred (✓); recognized same-family review is allowed and labelled (≈). "
    "Unknown or unrecognized family labels mean independence unknown (?), not proven "
    "cross-family review. "
    "° marks a cell where the preferred reviewer was skipped because it shares the "
    "author's family. The table is derived from the families of the bound models "
    "only; it is read-only."
)
ROUTING_MIN_COLS = 60
NAMED_BINDINGS_TITLE = "named bindings"
NAMED_BINDINGS_KEYBAR = (("Enter", "edit"), ("A", "add"), ("X", "delete"), ("?", "help"), ("Esc", "back"))
NAMED_BINDINGS_HELP = (
    "A named binding is a model and an effort under one name. A profile slot set to a "
    "named binding follows it: editing the binding moves every slot that uses it, in "
    "every profile, and offers to apply the change to the sessions that follow those "
    "profiles.\n\n"
    "Enter picks a new model and effort for the highlighted binding. A adds one "
    "(lowercase letters, digits and dashes). X deletes one that no profile uses."
)
NAMED_BINDINGS_MIN_COLS = 50
NAMED_BINDINGS_READ_ONLY = "named bindings are read-only here"


def _too_small(win: Any, palette: Palette, what: str, rows: int, cols: int) -> None:
    """The too-small text (rows 1–2, column 2); only Esc (and resize) act."""

    win.erase()
    safe_add(win, 1, 2, TOO_SMALL.format(what=what), palette.attr("warn"))
    safe_add(win, 2, 2, TOO_SMALL_HINT.format(cols=cols, rows=rows), palette.attr("dim"))
    win.refresh()


def _reflow(text: str, width: int) -> list[str]:
    """Help text re-flowed to ``width`` (blank-line paragraphs kept)."""

    lines: list[str] = []
    for paragraph in text.split("\n\n"):
        if lines:
            lines.append("")
        lines.extend(
            textwrap.wrap(" ".join(paragraph.split()), max(20, width), break_on_hyphens=False)
        )
    return lines


def _help(win: Any, palette: Palette, title: str, text: str, background: Any = None,
          extra: Sequence[str] = ()) -> None:
    wrap = max(20, min(76, win.getmaxyx()[1] - 8))
    lines = _reflow(text, wrap)
    if extra:
        lines.append("")
        for line in extra:
            lines.extend(textwrap.wrap(line, wrap, subsequent_indent="  ") or [""])
    Modal(title, lines, buttons=(("Close", True),)).run(win, palette, background=background)


def _clip_at(text: str, width: int, col: int) -> str:
    """``text`` clipped so it never reaches past column ``width - 2`` from ``col``."""

    return views.clip(text, max(0, width - 1 - col))


_UNSET: Any = object()
_NATIVE_FIELDS = (
    ("explore", "Explore", ("replace", "native", "off")),
    ("plan", "Plan", ("native", "off")),
    ("general_purpose", "general-purpose", ("on", "off")),
)


@dataclass(frozen=True)
class ProfileEditorOutcome:
    """One save the editor asked for (§4.2 Save).

    ``action`` is ``save`` (the name is unchanged: ``ProfileStore.save``),
    ``new`` (``ProfileStore.new`` without ``seed``) or ``rename`` (the
    profile rename that also moves the default, writing ``document`` under
    the new name).  ``document`` is the profile as the editor holds it.  ``expected`` is the digest of the
    stored profile when the editor loaded it: the write refuses when the
    profile changed since (None: write regardless, the Overwrite choice).
    """

    action: str
    name: str
    document: Mapping[str, Any]
    old_name: str | None = None
    expected: str | None = None


class ProfileChanged(Exception):
    """Raised by ``ProfileEditorCallbacks.save`` when the stored profile
    changed since the editor loaded it: nothing was written."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"{name} changed since it was read")


@dataclass
class ProfileEditorCallbacks:
    """The cli glue's store access for the editor stack (the screens write nothing).

    - ``save(win, outcome)`` writes one :class:`ProfileEditorOutcome` and runs
      the propagation prompt on ``win``; it returns ``(ok, text)``: the error
      to show with every edit kept, or the note to show after the write.
      None: saves are only recorded.
    - ``load_bindings()`` returns ``(bindings, conflict lines)``.
    - ``binding_users(name)`` counts the profiles using ``name``.
    - ``set_binding(win, name, model, effort)`` / ``delete_binding(name)``
      write one named binding (the set runs its propagation on ``win``) and
      return the error text or None.
    - ``reload(name)`` returns the stored profile and its digest (the
      Reload choice after :class:`ProfileChanged`).

    ``save`` raises :class:`ProfileChanged` when the outcome's ``expected``
    digest no longer matches the stored profile.
    """

    save: Callable[[Any, ProfileEditorOutcome], tuple[bool, str]] | None = None
    load_bindings: Callable[[], tuple[Mapping[str, Mapping[str, Any]], Sequence[str]]] | None = None
    binding_users: Callable[[str], int] | None = None
    set_binding: Callable[[Any, str, str, str], str | None] | None = None
    delete_binding: Callable[[str], str | None] | None = None
    reload: Callable[[str], tuple[Mapping[str, Any], str | None]] | None = None


class ProfileEditorState:
    """The profile document being edited (pure; §4.2).

    ``document`` is a deep copy mutated by the setters; ``original`` is what
    was loaded or last saved (dirty = they differ).  ``evaluation`` is
    ``profile.evaluate(document, cat, bindings=…, effective=…, ad_hoc=False)``,
    recomputed after every mutation.  ``origin`` is the stored name the
    document came from (None for a profile that does not exist yet, whose
    save is ``ProfileStore.new``).  ``loaded_digest`` is the stored
    profile's digest when it was loaded (a save refuses when it changed);
    ``fallback_owners`` maps a provider to the other profiles that are its
    fallback (the duplicate-fallback check).
    """

    def __init__(
        self,
        document: Mapping[str, Any],
        *,
        cat: profile_mod.LineupCatalog,
        bindings: Mapping[str, Mapping[str, Any]],
        effective: settings_mod.Effective,
        origin: str | None,
        is_seed: bool,
        seed_update: tuple[int | None, int] | None = None,
        loaded_digest: str | None = None,
        fallback_owners: Mapping[str, Sequence[str]] | None = None,
    ):
        self.document: dict[str, Any] = copy.deepcopy(dict(document))
        self.original: dict[str, Any] = copy.deepcopy(self.document)
        self.cat = cat
        self.bindings: dict[str, dict[str, Any]] = copy.deepcopy(dict(bindings))
        self.effective = effective
        self.origin = origin
        self.is_seed = is_seed
        self.seed_update = seed_update
        self.loaded_digest = loaded_digest
        self.fallback_owners: dict[str, tuple[str, ...]] = {
            provider: tuple(names) for provider, names in (fallback_owners or {}).items()}
        self.message = ""
        self.message_role = "warn"
        self.saved: ProfileEditorOutcome | None = None
        # Every committed rename ``(old, new)`` in order; ``saved`` keeps only the latest write.
        self.renamed: list[tuple[str, str]] = []
        self.evaluation: profile_mod.Evaluation
        self._evaluate()

    # -- facts ---------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.document != self.original

    @property
    def name(self) -> str:
        return str(self.document.get("name", ""))

    def _evaluate(self) -> None:
        self.evaluation = profile_mod.evaluate(
            self.document,
            self.cat,
            bindings=self.bindings,
            effective=self.effective,
            ad_hoc=False,
        )

    def diamond(self) -> bool:
        """A ``compaction_percent`` override that differs from the Settings value."""

        overrides = self.document.get("settings_overrides")
        if not isinstance(overrides, Mapping):
            return False
        value = overrides.get(settings_mod.COMPACTION_PERCENT_KEY)
        return value is not None and value != self.effective.compaction_percent

    def field_row(self, error: str) -> str:
        """The row an evaluation error belongs to (``general``/``lead``/agent id/``native``)."""

        head = error.split(":", 1)[0].strip()
        if head == "lead" or head.startswith("lead."):
            return "lead"
        if head.startswith("agents."):
            rid = head.split(".")[1]
            return rid if rid in catalog_mod.AGENT_ROLE_IDS else "general"
        if head.startswith("native_agents") or head == "workflows":
            return "native"
        return "general"

    def duplicate_fallback(self) -> str | None:
        """The check line when another profile is the fallback for this
        profile's primary provider too (None: no such profile)."""

        provider = self.document.get("primary_provider")
        if not isinstance(provider, str) or not provider:
            return None
        names = self.document.get("name"), self.origin
        others = [name for name in self.fallback_owners.get(provider, ()) if name not in names]
        if not others:
            return None
        return PROFILE_FORM_DUP_FALLBACK.format(other=", ".join(others), provider=provider)

    def reload(self, document: Mapping[str, Any], digest: str | None) -> None:
        """Show the stored profile instead of the edits (the Reload choice)."""

        self.document = copy.deepcopy(dict(document))
        self.original = copy.deepcopy(self.document)
        self.loaded_digest = digest
        self._evaluate()

    def slot_binding(self, slot: str) -> Mapping[str, Any] | None:
        if slot == catalog_mod.LEAD_ROLE:
            lead = self.document.get("lead")
            return lead if isinstance(lead, Mapping) else None
        agents = self.document.get("agents")
        value = agents.get(slot) if isinstance(agents, Mapping) else None
        return value if isinstance(value, Mapping) else None

    # -- setters -------------------------------------------------------------

    def set_bindings(self, bindings: Mapping[str, Mapping[str, Any]]) -> None:
        self.bindings = copy.deepcopy(dict(bindings))
        self._evaluate()

    def _put(self, slot: str, value: dict[str, Any]) -> None:
        if slot == catalog_mod.LEAD_ROLE:
            self.document["lead"] = value
        else:
            agents = self.document.get("agents")
            if not isinstance(agents, dict):
                agents = {}
                self.document["agents"] = agents
            agents[slot] = value
        self._evaluate()

    def set_binding(self, slot: str, model: str, effort: str) -> None:
        self._put(slot, {"effort": effort, "model": model})

    def set_named(self, slot: str, name: str) -> None:
        self._put(slot, {"use": name})

    def unbind(self, slot: str) -> str | None:
        if slot == catalog_mod.LEAD_ROLE:
            return PROFILE_FORM_LEAD_UNBIND
        agents = self.document.get("agents")
        if isinstance(agents, dict):
            agents.pop(slot, None)
        self._evaluate()
        return None

    def set_native(self, field: str, value: str) -> None:
        allowed = {key: values for key, _label, values in _NATIVE_FIELDS}
        if field not in allowed or value not in allowed[field]:
            raise ValueError(f"native_agents.{field}: {value!r} is not one of {allowed.get(field)}")
        native = self.document.get("native_agents")
        if not isinstance(native, dict):
            native = {}
            self.document["native_agents"] = native
        native[field] = value
        self._evaluate()

    def set_workflows(self, value: str) -> None:
        if value not in WORKFLOWS_VALUES:
            raise ValueError(f"workflows: {value!r} is not one of {WORKFLOWS_VALUES}")
        self.document["workflows"] = value
        self._evaluate()

    def set_general(
        self,
        *,
        name: Any = _UNSET,
        description: Any = _UNSET,
        primary_provider: Any = _UNSET,
        lead_providers: Any = _UNSET,
        compaction_override: Any = _UNSET,
    ) -> str | None:
        """Apply the General sub-form; returns the schema problems (nothing applied) or None.

        ``primary_provider`` None removes it; ``lead_providers`` None or empty
        removes it (any provider); ``compaction_override`` None removes the
        override (use Settings).
        """

        candidate = copy.deepcopy(self.document)
        if name is not _UNSET:
            candidate["name"] = name
        if description is not _UNSET:
            if description:
                candidate["description"] = description
            else:
                candidate.pop("description", None)
        if primary_provider is not _UNSET:
            if primary_provider is None:
                candidate.pop("primary_provider", None)
            else:
                candidate["primary_provider"] = primary_provider
        if lead_providers is not _UNSET:
            if lead_providers:
                candidate["lead_providers"] = list(lead_providers)
            else:
                candidate.pop("lead_providers", None)
        if compaction_override is not _UNSET:
            overrides = dict(candidate.get("settings_overrides") or {})
            if compaction_override is None:
                overrides.pop(settings_mod.COMPACTION_PERCENT_KEY, None)
            else:
                overrides[settings_mod.COMPACTION_PERCENT_KEY] = compaction_override
            if overrides:
                candidate["settings_overrides"] = overrides
            else:
                candidate.pop("settings_overrides", None)
        try:
            profile_mod.parse(candidate)
        except profile_mod.ProfileValidationError as exc:
            return "; ".join(exc.errors)
        self.document = candidate
        self._evaluate()
        return None

    def replace_document(self, document: Mapping[str, Any]) -> None:
        """The ``^G`` reload (already through ``profile.parse``)."""

        self.document = copy.deepcopy(dict(document))
        self._evaluate()

    def mark_saved(self, outcome: ProfileEditorOutcome) -> None:
        """After a successful write: the saved document is the new baseline."""

        if outcome.action == "new":
            self.document.pop("seed", None)
            self.is_seed = False
            self.seed_update = None
        elif outcome.action == "rename":
            self.is_seed = False
            if outcome.old_name:
                self._record_rename(outcome.old_name, outcome.name)
        self.original = copy.deepcopy(self.document)
        self.origin = outcome.name
        self.saved = outcome
        self._evaluate()

    def _record_rename(self, old: str, new: str) -> None:
        # The callbacks and the screen both mark one save: record it once.
        if old != new and (not self.renamed or self.renamed[-1] != (old, new)):
            self.renamed.append((old, new))


class _GeneralForm:
    """The General sub-form (§4.2): name, description, primary provider, lead
    providers and the compaction override."""

    what = "the general form"
    FIELDS = ("name", "description", "primary", "lead_providers", "compaction", "apply")
    LABELS = {
        "name": "name",
        "description": "description",
        "primary": "primary provider",
        "lead_providers": "lead providers",
        "compaction": "compaction override",
        "apply": "",
    }

    def __init__(self, state: ProfileEditorState, palette: Palette):
        self.state = state
        self.palette = palette
        document = state.document
        self.inputs = {
            "name": TextInput(str(document.get("name", "")), max_length=64),
            "description": TextInput(str(document.get("description", "")), max_length=256),
        }
        overrides = document.get("settings_overrides") or {}
        percent = overrides.get(settings_mod.COMPACTION_PERCENT_KEY)
        self.inputs["compaction"] = TextInput("" if percent is None else str(percent), max_length=6)
        primary = document.get("primary_provider")
        self.primary: str | None = primary if isinstance(primary, str) else None
        lead_providers = document.get("lead_providers")
        self.lead_providers: list[str] = list(lead_providers) if isinstance(lead_providers, list) else []
        self.focus = 0
        self.message = ""

    def min_size(self, width: int) -> tuple[int, int]:
        bar = KeyBar(PROFILE_FORM_GENERAL_KEYBAR).rows(width)
        return (3 + len(self.FIELDS) + 1 + 1 + bar, PROFILE_EDITOR_MIN_COLS)

    def _value_text(self, field: str) -> str:
        if field == "primary":
            return self.primary or "none"
        if field == "lead_providers":
            return ", ".join(self.lead_providers) if self.lead_providers else "any"
        return ""

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        safe_add(win, 1, 2, PROFILE_FORM_GENERAL_TITLE, palette.attr("accent") | curses.A_BOLD)
        safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        cursor: tuple[int, int] | None = None
        field_col = 24
        for index, field in enumerate(self.FIELDS):
            y = 3 + index
            focused = index == self.focus
            attr = palette.attr("normal") | (curses.A_REVERSE if focused else 0)
            if field == "apply":
                safe_add(win, y + 1, 2, "[ Apply ]", palette.attr("accent") | (curses.A_REVERSE if focused else 0))
                continue
            safe_add(win, y, 2, f"{self.LABELS[field]:<21}", attr if focused else palette.attr("normal"))
            if field in self.inputs:
                box = min(48, width - field_col - 2)
                if field == "compaction":
                    box = 8
                position = self.inputs[field].draw(win, y, field_col, box, palette, focused=focused)
                if focused:
                    cursor = position
                if field == "compaction":
                    low, high = settings_mod.COMPACTION_PERCENT_MIN, settings_mod.COMPACTION_PERCENT_MAX
                    note = (
                        f"empty = Settings ({self.state.effective.compaction_percent} %) · "
                        f"{low}–{high}"
                    )
                    safe_add(win, y, field_col + box + 2, _clip_at(note, width, field_col + box + 2),
                             palette.attr("dim"))
            else:
                text = self._value_text(field)
                safe_add(win, y, field_col, _clip_at(text, width - 9, field_col), attr)
                safe_add(win, y, width - 9, "[Enter]", palette.attr("dim"))
        bar = KeyBar(PROFILE_FORM_GENERAL_KEYBAR)
        if self.message:
            safe_add(win, height - bar.rows(width) - 1, 2, _clip_at(self.message, width, 2), palette.attr("warn"))
        bar.draw(win, height - 1, palette)
        if cursor is not None:
            show_cursor()
            try:
                win.move(*cursor)
            except CursesError:
                pass
        else:
            hide_cursor()
        win.refresh()

    def _pick_primary(self, win: Any) -> None:
        providers = sorted(self.state.cat.providers)
        choices = [None, *providers]
        items = [SelectItem(p or "none", checked=p == self.primary) for p in choices]
        index = SelectList(
            "primary provider",
            items,
            footer=(("Enter", "choose"), ("Esc", "back")),
            selected=choices.index(self.primary) if self.primary in choices else 0,
        ).run(win, self.palette)
        if index is not None:
            self.primary = choices[index]

    def _pick_lead_providers(self, win: Any) -> None:
        providers = sorted(self.state.cat.providers)
        chooser = SelectList(
            "lead providers (none checked = any)",
            [SelectItem(p) for p in providers],
            footer=(("Space", "toggle"), ("Enter", "done"), ("Esc", "back")),
            multi=True,
        )
        chooser.toggled = {i for i, p in enumerate(providers) if p in self.lead_providers}
        picked = chooser.run(win, self.palette)
        if picked is not None:
            self.lead_providers = [providers[i] for i in picked]

    def _apply(self) -> bool:
        text = self.inputs["compaction"].value.strip()
        compaction: int | None = None
        if text:
            try:
                compaction = int(text, 10)
            except ValueError:
                self.message = f"compaction override: {text!r} is not a whole number"
                return False
        error = self.state.set_general(
            name=self.inputs["name"].value.strip(),
            description=self.inputs["description"].value,
            primary_provider=self.primary,
            lead_providers=self.lead_providers or None,
            compaction_override=compaction,
        )
        if error is not None:
            self.message = error
            return False
        return True

    def run(self, win: Any) -> bool:
        """True when the changes were applied to the state; False on Esc."""

        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc":
                hide_cursor()
                return False
            if key.kind == "ctrl" and key.ch == "c":
                hide_cursor()
                return False
            height, width = win.getmaxyx()
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            self.message = ""
            field = self.FIELDS[self.focus]
            if key.kind in ("up", "btab"):
                self.focus = (self.focus - 1) % len(self.FIELDS)
            elif key.kind in ("down", "tab"):
                self.focus = (self.focus + 1) % len(self.FIELDS)
            elif key.kind == "ctrl" and key.ch in ("s", "o"):
                if self._apply():
                    hide_cursor()
                    return True
            elif key.kind == "f1" or (key.kind == "char" and key.ch == "?" and field not in self.inputs):
                low, high = settings_mod.COMPACTION_PERCENT_MIN, settings_mod.COMPACTION_PERCENT_MAX
                _help(win, self.palette, "edit profile — general — help",
                      PROFILE_FORM_GENERAL_HELP.format(
                          low=low, high=high, percent=self.state.effective.compaction_percent),
                      self._draw)
            elif key.kind == "enter":
                if field == "apply":
                    if self._apply():
                        hide_cursor()
                        return True
                elif field == "primary":
                    self._pick_primary(win)
                elif field == "lead_providers":
                    self._pick_lead_providers(win)
                else:
                    self.focus = (self.focus + 1) % len(self.FIELDS)
            elif field in self.inputs:
                self.inputs[field].handle(key)


class _NativeForm:
    """The Native sub-form (§4.2): Explore, Plan, general-purpose, workflows."""

    what = "the native form"

    def __init__(self, state: ProfileEditorState, palette: Palette):
        self.state = state
        self.palette = palette
        native = state.document.get("native_agents") or {}
        self.groups: list[tuple[str, str, tuple[str, ...]]] = [
            *_NATIVE_FIELDS, ("workflows", "workflows", WORKFLOWS_VALUES)
        ]
        self.values = {
            key: (
                str(state.document.get("workflows", "native"))
                if key == "workflows"
                else str(native.get(key, values[0]))
            )
            for key, _label, values in self.groups
        }
        self.focus = 0

    def min_size(self, width: int) -> tuple[int, int]:
        return (3 + len(self.groups) + 1 + KeyBar(PROFILE_FORM_NATIVE_KEYBAR).rows(width),
                PROFILE_EDITOR_MIN_COLS)

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        safe_add(win, 1, 2, PROFILE_FORM_NATIVE_TITLE, palette.attr("accent") | curses.A_BOLD)
        safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        for index, (key, label, values) in enumerate(self.groups):
            y = 3 + index
            focused = index == self.focus
            safe_add(win, y, 2, f"{label:<18}",
                     palette.attr("normal") | (curses.A_REVERSE if focused else 0))
            col = 21
            for value in values:
                box = Checkbox(value, checked=value == self.values[key], radio=True)
                col += box.draw(win, y, col, palette,
                                option_cursor=focused and value == self.values[key]) + 2
        KeyBar(PROFILE_FORM_NATIVE_KEYBAR).draw(win, height - 1, palette)
        win.refresh()

    def _apply(self) -> None:
        for key, _label, _values in _NATIVE_FIELDS:
            self.state.set_native(key, self.values[key])
        self.state.set_workflows(self.values["workflows"])

    def run(self, win: Any) -> bool:
        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return False
            height, width = win.getmaxyx()
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            group_key, _label, values = self.groups[self.focus]
            if key.kind in ("up", "btab"):
                self.focus = (self.focus - 1) % len(self.groups)
            elif key.kind in ("down", "tab"):
                self.focus = (self.focus + 1) % len(self.groups)
            elif key.kind in ("left", "right"):
                current = values.index(self.values[group_key]) if self.values[group_key] in values else 0
                step = -1 if key.kind == "left" else 1
                self.values[group_key] = values[(current + step) % len(values)]
            elif key.kind == "enter" or (key.kind == "ctrl" and key.ch in ("s", "o")):
                self._apply()
                return True
            elif key.kind == "char" and key.ch == "?":
                _help(win, self.palette, "edit profile — native — help", PROFILE_FORM_NATIVE_HELP,
                      self._draw, extra=workflow_guarantee_panel(self.values["workflows"]).splitlines())


class BindingPicker:
    """The binding picker (T §2.1, §4.3) over a ``views.PickerModel``.

    ``run(win)`` returns ``("bind", key, effort)``, ``("use", name)``,
    ``("off",)`` or None (Esc).  ← → cycle the highlighted line's efforts and
    stop at either end; named rows and inert (workflow client-effort) rows
    keep their effort.
    """

    what = "the model picker"

    def __init__(self, model: views.PickerModel, *, palette: Palette | None = None):
        self.model = model
        self.palette = palette or MONO_PALETTE
        self.index = model.selected
        self.efforts: dict[int, str] = {}
        self.offset = 0

    def effort(self, index: int | None = None) -> str:
        index = self.index if index is None else index
        item = self.model.items[index]
        return self.efforts.get(index, item.initial_effort)

    def _chrome(self) -> int:
        # Top, title, (the role subtitle,) rule, the effort line.
        return 5 + (1 if self.model.subtitle else 0)

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=self._chrome(),
            list_rows=len(self.model.items),
            detail_reserve=0,
            bar_rows=KeyBar(BINDING_PICKER_KEYBAR).rows(width),
            cols=BINDING_PICKER_MIN_COLS,
        ).as_tuple()

    def _effort_line(self) -> str:
        if not self.model.items:
            return ""
        item = self.model.items[self.index]
        if item.kind == "line" and not item.selectable:
            # A dimmed row's reason (the first gate refusal) is clipped in
            # the row; the detail line shows it in full (§4.10).
            if item.effort_reasons:
                reason = item.effort_reasons.get(item.initial_effort) or next(iter(item.effort_reasons.values()))
                return f"not agent-eligible: {reason}"
            return item.note.replace("(", "").replace(")", "")
        if item.kind == "off" or not item.selectable:
            return ""
        effort = self.effort()
        if effort in item.effort_reasons:
            return f"{effort}: not eligible — {item.effort_reasons[effort]}"
        if item.kind == "named":
            return f"effort: [{effort}] (named binding)"
        if len(item.efforts) > 1:
            return f"← → effort: [{effort}]"
        return f"effort: [{effort}]"

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = KeyBar(BINDING_PICKER_KEYBAR)
        bar_rows = bar.rows(width)
        safe_add(win, 1, 2, _clip_at(self.model.title, width, 2), palette.attr("accent") | curses.A_BOLD)
        top = 2
        if self.model.subtitle:
            safe_add(win, top, 2, _clip_at(self.model.subtitle, width, 2), palette.attr("dim"))
            top += 1
        safe_add(win, top, 2, views.rule(width), palette.attr("dim"))
        visible = max(1, height - bar_rows - self._chrome())
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + visible:
            self.offset = self.index - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.model.items) - visible)))
        for row, index in enumerate(range(self.offset, min(len(self.model.items), self.offset + visible)), top + 1):
            item = self.model.items[index]
            focused = index == self.index and item.kind != "zero"
            attr = palette.attr(item.role) | (curses.A_REVERSE if focused else 0)
            if focused:
                safe_add(win, row, 2, "›", palette.attr("accent"))
            safe_add(win, row, 4, views.fit_text((item.text,), max(0, width - 5)), attr)
        safe_add(win, height - bar_rows - 1, 2, _clip_at(self._effort_line(), width, 2), palette.attr("accent"))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def _move(self, direction: int) -> None:
        index = self.index
        while 0 <= index + direction < len(self.model.items):
            index += direction
            if self.model.items[index].kind != "zero":
                self.index = index
                return

    def _edge(self, last: bool) -> None:
        indexes = range(len(self.model.items) - 1, -1, -1) if last else range(len(self.model.items))
        for index in indexes:
            if self.model.items[index].kind != "zero":
                self.index = index
                return

    def _cycle(self, step: int) -> None:
        item = self.model.items[self.index]
        if item.kind != "line" or len(item.efforts) < 2:
            return
        current = self.effort()
        position = item.efforts.index(current) if current in item.efforts else 0
        self.efforts[self.index] = item.efforts[min(len(item.efforts) - 1, max(0, position + step))]

    def result(self) -> tuple[str, ...] | None:
        if not self.model.items:
            return None
        item = self.model.items[self.index]
        if not item.selectable or self.effort() in item.effort_reasons:
            return None
        if item.kind == "off":
            return ("off",)
        if item.kind == "named":
            return ("use", item.key)
        return ("bind", item.key, self.effort())

    def run(self, win: Any) -> tuple[str, ...] | None:
        hide_cursor()
        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc":
                return None
            if key.kind == "ctrl" and key.ch == "c":
                return None
            height, width = win.getmaxyx()
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self._move(-1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self._move(1)
            elif key.kind == "home":
                self._edge(last=False)
            elif key.kind == "end":
                self._edge(last=True)
            elif key.kind == "left":
                self._cycle(-1)
            elif key.kind == "right":
                self._cycle(1)
            elif key.kind == "char" and key.ch == "?":
                _help(win, self.palette, f"{self.model.title} — help", BINDING_PICKER_HELP, self._draw)
            elif key.kind == "char" and key.ch in ("v", "V"):
                item = self.model.items[self.index]
                role = ["", self.model.subtitle] if self.model.subtitle else []
                TextView("model — details", [item.text, *item.details, item.note,
                                             item.effort_reasons.get(self.effort(), ""), *role],
                         palette=self.palette).run(win)
            elif key.kind == "enter":
                if self.model.items and (not self.model.items[self.index].selectable or
                                         self.effort() in self.model.items[self.index].effort_reasons):
                    item = self.model.items[self.index]
                    TextView("model not eligible", [item.effort_reasons.get(self.effort()) or item.note or item.text], palette=self.palette).run(win)
                    continue
                chosen = self.result()
                if chosen is not None:
                    return chosen


class RoutingPreviewScreen:
    """The read-only review routing table (T §2.2, §4.4) of one lineup."""

    what = "the routing preview"

    def __init__(self, lineup: profile_mod.ResolvedLineup, *, name: str, palette: Palette | None = None):
        self.lineup = lineup
        self.name = name
        self.palette = palette or MONO_PALETTE
        self.rows = views.routing_rows(lineup)

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=5,
            list_rows=len(self.rows),
            detail_reserve=0,
            bar_rows=KeyBar(ROUTING_KEYBAR).rows(width),
            cols=ROUTING_MIN_COLS,
        ).as_tuple()

    def lines(self, width: int) -> list[tuple[str, str]]:
        """``(text, role)`` of the header, rows and legend (no title/rule)."""

        if self.lineup.is_direct:
            return [(f"  {self.rows[0][0]}", "normal")]
        header = ("author", "normal change", "high-stakes change")
        cells = [header, *self.rows]
        first = max(len(row[0]) for row in cells)
        second = max(len(row[1]) for row in cells)

        def line(row: tuple[str, str, str]) -> str:
            return f"  {row[0]:<{first}}  {row[1]:<{second}}  {row[2]}".rstrip()

        out = [(line(header), "dim")]
        out.extend((line(row), "normal") for row in self.rows)
        legend = textwrap.wrap(ROUTING_LEGEND, max(20, width - 5), break_on_hyphens=False)
        out.extend((f"  {text}", "dim") for text in legend)
        return out

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = KeyBar(ROUTING_KEYBAR)
        safe_add(win, 1, 2, _clip_at(f"review routing — {self.name}", width, 2),
                 palette.attr("accent") | curses.A_BOLD)
        safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        limit = height - bar.rows(width) - 1
        for offset, (text, role) in enumerate(self.lines(width)):
            y = 3 + offset
            if y >= limit:
                break
            safe_add(win, y, 0, _clip_at(text, width, 0), palette.attr(role))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def run(self, win: Any) -> None:
        hide_cursor()
        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return
            if key.kind == "char" and key.ch == "?":
                height, width = win.getmaxyx()
                rows, cols = self.min_size(width)
                if height >= rows and width >= cols:
                    _help(win, self.palette, "review routing — help", ROUTING_HELP, self._draw)


class NamedBindingsScreen:
    """Named bindings (T §2 ``N``, §4.5): list, edit, add, delete.

    Every write goes through the callbacks (``set_binding`` runs the
    propagation prompt itself); the screen writes nothing.
    """

    what = "named bindings"

    def __init__(
        self,
        cat: profile_mod.LineupCatalog,
        effective: settings_mod.Effective,
        *,
        callbacks: ProfileEditorCallbacks | None = None,
        palette: Palette | None = None,
        bindings: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.cat = cat
        self.effective = effective
        self.callbacks = callbacks or ProfileEditorCallbacks()
        self.palette = palette or MONO_PALETTE
        self._fallback = dict(bindings or {})
        self.index = 0
        self.message = ""
        self.message_role = "warn"
        self.reload()

    def reload(self) -> None:
        if self.callbacks.load_bindings is not None:
            bindings, conflicts = self.callbacks.load_bindings()
        else:
            bindings, conflicts = self._fallback, ()
        self.bindings = {name: dict(value) for name, value in bindings.items()}
        self.conflicts = list(conflicts)
        self.names = sorted(self.bindings)
        self.users = {
            name: (self.callbacks.binding_users(name) if self.callbacks.binding_users else 0)
            for name in self.names
        }
        self.index = min(self.index, max(0, len(self.names) - 1))

    def _row(self, name: str) -> str:
        spec = self.bindings[name]
        model, effort = str(spec.get("model")), str(spec.get("effort"))
        try:
            key = self.cat.resolve_key(model).key
        except catalog_mod.CatalogError:
            key = None
        if key is None or key not in self.cat.lines:
            short = f"{model} (removed)"
        else:
            short = views.short_label(self.cat.lines[key].get("display", key))
        return f"  {name:<16} {short} · {effort}"

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=4 + len(self.conflicts),
            list_rows=max(1, len(self.names)),
            detail_reserve=0,
            bar_rows=KeyBar(NAMED_BINDINGS_KEYBAR).rows(width),
            cols=NAMED_BINDINGS_MIN_COLS,
        ).as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = KeyBar(NAMED_BINDINGS_KEYBAR)
        bar_rows = bar.rows(width)
        safe_add(win, 1, 2, NAMED_BINDINGS_TITLE, palette.attr("accent") | curses.A_BOLD)
        safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        y = 3
        message_row = height - bar_rows - 1
        visible = max(1, message_row - len(self.conflicts) - y)
        start = max(0, min(self.index - visible + 1, max(0, len(self.names) - visible)))
        if not self.names:
            safe_add(win, y, 2, "(no named bindings — A adds one)", palette.attr("dim"))
            y += 1
        for index in range(start, min(len(self.names), start + visible)):
            name = self.names[index]
            focused = index == self.index
            used = f"used by {self.users.get(name, 0)}"
            text = self._row(name)
            room = width - 3 - len(used) - 2
            line = f"{views.clip(text, room):<{room}}  {used}"
            safe_add(win, y, 0, line, palette.attr("normal") | (curses.A_REVERSE if focused else 0))
            y += 1
        for conflict in self.conflicts:
            safe_add(win, y, 0, _clip_at(f"! {conflict}", width, 0), palette.attr("warn"))
            y += 1
        if self.message:
            safe_add(win, message_row, 2, _clip_at(self.message, width, 2), palette.attr(self.message_role))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def _say(self, text: str, role: str = "warn") -> None:
        self.message = text
        self.message_role = role

    def _pick(self, win: Any, current: Mapping[str, Any] | None) -> tuple[str, ...] | None:
        rows = views.line_rows(self.cat, self.effective, custom_ids=frozenset())
        model = views.picker_rows(
            rows, slot="binding", bindings=self.bindings, lcat=self.cat, eff=self.effective,
            current=current,
        )
        return BindingPicker(model, palette=self.palette).run(win)

    def _set(self, win: Any, name: str, current: Mapping[str, Any] | None) -> None:
        if self.callbacks.set_binding is None:
            self._say(NAMED_BINDINGS_READ_ONLY)
            return
        chosen = self._pick(win, current)
        if chosen is None or chosen[0] != "bind":
            return
        error = self.callbacks.set_binding(win, name, chosen[1], chosen[2])
        self.reload()
        if error is not None:
            self._say(error)
            return
        if name in self.names:
            self.index = self.names.index(name)
        self._say(f"saved binding {name}", "accent")

    def _add(self, win: Any) -> None:
        if self.callbacks.set_binding is None:
            self._say(NAMED_BINDINGS_READ_ONLY)
            return
        modal = Modal("new binding — name:", [], buttons=(("OK", True), ("Cancel", False)),
                      input=TextInput("", max_length=32))
        confirmed = modal.run(win, self.palette, background=self._draw)
        name = modal.input.value.strip() if modal.input is not None else ""
        if not confirmed or not name:
            return
        self._set(win, name, None)

    def _delete(self, win: Any) -> None:
        if not self.names:
            return
        if self.callbacks.delete_binding is None:
            self._say(NAMED_BINDINGS_READ_ONLY)
            return
        name = self.names[self.index]
        confirmed = Modal(f"Delete binding {name}?", [], buttons=(("Delete", True), ("Cancel", False))).run(
            win, self.palette, background=self._draw
        )
        if not confirmed:
            return
        error = self.callbacks.delete_binding(name)
        self.reload()
        if error is not None:
            self._say(error)
        else:
            self._say(f"deleted binding {name}", "accent")

    def run(self, win: Any) -> None:
        hide_cursor()
        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return
            height, width = win.getmaxyx()
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            self.message = ""
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.index = max(0, self.index - 1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.index = min(max(0, len(self.names) - 1), self.index + 1)
            elif key.kind == "home":
                self.index = 0
            elif key.kind == "end":
                self.index = max(0, len(self.names) - 1)
            elif key.kind == "enter" and self.names:
                name = self.names[self.index]
                self._set(win, name, self.bindings[name])
            elif key.kind == "char" and key.ch in ("a", "A"):
                self._add(win)
            elif key.kind == "char" and key.ch in ("x", "X"):
                self._delete(win)
            elif key.kind == "char" and key.ch == "?":
                _help(win, self.palette, "named bindings — help", NAMED_BINDINGS_HELP, self._draw)


class ProfileEditorScreen:
    """The profile editor on a curses window.

    ``run(win)`` returns the last :class:`ProfileEditorOutcome` saved while
    the screen was open (None when nothing was saved).  ``^S``/``^O`` save:
    an invalid profile never saves (the message names the error count and
    the focus moves to the first error's row); a changed name asks Save as
    new / Rename / Cancel.  The write itself is ``callbacks.save`` (the cli
    glue); its error text is shown verbatim with every edit kept.  ``^G``
    edits the raw JSON in ``$VISUAL``/``$EDITOR`` under
    :func:`suspended_curses` (the one subprocess the editor starts).
    """

    what = "the profile editor"

    def __init__(
        self,
        state: ProfileEditorState,
        *,
        palette: Palette | None = None,
        callbacks: ProfileEditorCallbacks | None = None,
        environ: Mapping[str, str] | None = None,
        open_in_editor: Callable[[str], int] | None = None,
        suspender: Callable[[Any], contextlib.AbstractContextManager[Any]] | None = None,
        initial_focus: str | None = None,
    ):
        self.state = state
        self.palette = palette or MONO_PALETTE
        self.callbacks = callbacks or ProfileEditorCallbacks()
        self.environ = dict(os.environ if environ is None else environ)
        self.open_in_editor = open_in_editor
        self.suspender = suspender
        self.focus = initial_focus or "general"
        self.agent_offset = 0

    # -- rows ----------------------------------------------------------------

    def rows(self, width: int) -> tuple[views.EditorRow, ...]:
        message = self.state.message
        rows = views.editor_rows(self.state, width=width, message=message, focus=self.focus)
        if message and rows[-1].kind == "message":
            rows = (*rows[:-1], dataclasses.replace(rows[-1], role=self.state.message_role))
        duplicate = self.state.duplicate_fallback()
        if duplicate is not None and not self.state.evaluation.errors:
            rows = tuple(
                dataclasses.replace(row, text=f"{row.text}   {duplicate}", role="warn")
                if row.kind == "checks" else row
                for row in rows
            )
        return rows

    def _focusable(self, width: int = 80) -> list[str]:
        return [row.key for row in self.rows(width) if row.selectable]

    def min_size(self, width: int) -> tuple[int, int]:
        return (
            PROFILE_EDITOR_CHROME + 3 + KeyBar(PROFILE_EDITOR_KEYBAR).rows(width),
            PROFILE_EDITOR_MIN_COLS,
        )

    # -- drawing -------------------------------------------------------------

    def _draw_row(self, win: Any, y: int, row: views.EditorRow, width: int) -> None:
        palette = self.palette
        focused = row.selectable and row.key == self.focus
        focus_attr = curses.A_REVERSE if focused else 0
        if row.kind == "header":
            safe_add(win, y, 2, _clip_at(row.text, width, 2), palette.attr("accent") | curses.A_BOLD)
            if row.suffix and len(row.text) + 4 + len(row.suffix) <= width - 3:
                safe_add(win, y, width - 2 - len(row.suffix), row.suffix, palette.attr(row.suffix_role))
            return
        if row.kind in ("rule", "agents-header"):
            attr = palette.attr("dim") | (curses.A_BOLD if row.kind == "agents-header" else 0)
            safe_add(win, y, 2, row.text, attr)
            return
        if row.kind == "blank":
            return
        if row.kind == "agent":
            if focused:
                safe_add(win, y, 2, "›", palette.attr("accent"))
            text = _clip_at(row.text, width, 4)
            safe_add(win, y, 4, text, palette.attr(row.role) | focus_attr)
            if row.suffix and 4 + len(text) + 3 + len(row.suffix) <= width - 2:
                safe_add(win, y, 4 + len(text) + 3, row.suffix, palette.attr(row.suffix_role))
            return
        if row.kind == "lead" and row.suffix:
            room = width - 2 - len(row.suffix) - 1
            text = views.clip(row.text, max(0, room - 2))
            safe_add(win, y, 2, text, palette.attr(row.role) | focus_attr)
            safe_add(win, y, width - 2 - len(row.suffix), row.suffix, palette.attr("dim"))
            return
        safe_add(win, y, 2, _clip_at(row.text, width, 2), palette.attr(row.role) | focus_attr)

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        need_rows, need_cols = self.min_size(width)
        if height < need_rows or width < need_cols:
            _too_small(win, palette, self.what, need_rows, need_cols)
            return
        win.erase()
        rows = self.rows(width)
        bar = KeyBar(PROFILE_EDITOR_KEYBAR)
        bar_rows = bar.rows(width)
        agents = [row for row in rows if row.kind == "agent"]
        first_agent = rows.index(agents[0])
        head, tail = rows[:first_agent], rows[first_agent + len(agents):]
        visible = max(1, min(len(agents), height - bar_rows - PROFILE_EDITOR_CHROME))
        keys = [row.key for row in agents]
        if self.focus in keys:
            position = keys.index(self.focus)
            if position < self.agent_offset:
                self.agent_offset = position
            elif position >= self.agent_offset + visible:
                self.agent_offset = position - visible + 1
        self.agent_offset = max(0, min(self.agent_offset, len(agents) - visible))
        y = 1
        for row in head:
            self._draw_row(win, y, row, width)
            y += 1
        for row in agents[self.agent_offset:self.agent_offset + visible]:
            self._draw_row(win, y, row, width)
            y += 1
        for row in tail:
            self._draw_row(win, y, row, width)
            y += 1
        bar.draw(win, height - 1, palette)
        win.refresh()

    # -- actions -------------------------------------------------------------

    def _say(self, text: str, role: str = "warn") -> None:
        self.state.message = text
        self.state.message_role = role

    def _move(self, delta: int) -> None:
        keys = self._focusable()
        if not keys:
            return
        position = keys.index(self.focus) if self.focus in keys else 0
        self.focus = keys[max(0, min(len(keys) - 1, position + delta))]

    def _settle_focus(self) -> None:
        keys = self._focusable()
        if self.focus not in keys and keys:
            self.focus = "general" if "general" in keys else keys[0]

    def _pick(self, win: Any, slot: str) -> None:
        state = self.state
        rows = views.line_rows(state.cat, state.effective, custom_ids=frozenset())
        model = views.picker_rows(
            rows, slot=slot, bindings=state.bindings, lcat=state.cat, eff=state.effective,
            current=state.slot_binding(slot),
        )
        chosen = BindingPicker(model, palette=self.palette).run(win)
        if chosen is None:
            return
        if chosen[0] == "bind":
            state.set_binding(slot, chosen[1], chosen[2])
        elif chosen[0] == "use":
            state.set_named(slot, chosen[1])

    def _routing(self, win: Any) -> None:
        lineup = self.state.evaluation.lineup
        if lineup is None:
            self._say("routing table ▸ fix the errors first")
            return
        RoutingPreviewScreen(lineup, name=self.state.name, palette=self.palette).run(win)

    def _named(self, win: Any) -> None:
        state = self.state
        NamedBindingsScreen(
            state.cat, state.effective, callbacks=self.callbacks, palette=self.palette,
            bindings=state.bindings,
        ).run(win)
        if self.callbacks.load_bindings is not None:
            bindings, _conflicts = self.callbacks.load_bindings()
            state.set_bindings(bindings)

    def _enter(self, win: Any) -> None:
        focus = self.focus
        state = self.state
        if focus == "general":
            _GeneralForm(state, self.palette).run(win)
        elif focus == "lead":
            self._pick(win, catalog_mod.LEAD_ROLE)
        elif focus in catalog_mod.AGENT_ROLE_IDS:
            self._pick(win, focus)
        elif focus == "native":
            _NativeForm(state, self.palette).run(win)
        elif focus == "review":
            self._routing(win)
        elif focus == "checks" and state.evaluation.errors:
            self.focus = state.field_row(state.evaluation.errors[0])

    def _save(self, win: Any) -> None:
        state = self.state
        errors = state.evaluation.errors
        if errors:
            self._say(PROFILE_FORM_FIX_ERRORS.format(n=len(errors)))
            self.focus = state.field_row(errors[0])
            return
        name = state.name
        if state.origin is None:
            outcome = ProfileEditorOutcome("new", name, copy.deepcopy(state.document))
        elif name == state.origin:
            outcome = ProfileEditorOutcome("save", name, copy.deepcopy(state.document), state.origin,
                                           state.loaded_digest)
        else:
            # A shipped profile keeps its name: only a copy under the new one.
            buttons = PROFILE_FORM_SEED_SAVE_BUTTONS if state.is_seed else (
                ("Save as new", "new"), ("Rename", "rename"), ("Cancel", None))
            choice = Modal(
                PROFILE_FORM_NEW_NAME_TITLE,
                [PROFILE_FORM_NEW_NAME_BODY.format(old=state.origin, new=name)],
                buttons=buttons,
            ).run(win, self.palette, background=self._draw)
            if choice is None:
                return
            expected = state.loaded_digest if choice == "rename" else None
            outcome = ProfileEditorOutcome(choice, name, copy.deepcopy(state.document), state.origin, expected)
        note = PROFILE_FORM_SAVED.format(name=name)
        if self.callbacks.save is not None:
            try:
                ok, note = self.callbacks.save(win, outcome)
            except ProfileChanged:
                self._changed(win, outcome)
                return
            if not ok:
                self._say(note)
                return
        state.mark_saved(outcome)
        self._say(note, "accent")

    def _changed(self, win: Any, outcome: ProfileEditorOutcome) -> None:
        """The stored profile changed while it was edited: Overwrite writes
        the edits anyway, Reload shows the stored one, Cancel keeps editing."""

        state = self.state
        stored = outcome.old_name or outcome.name
        lines = _reflow(PROFILE_FORM_CHANGED_BODY.format(name=stored), max(30, min(68, win.getmaxyx()[1] - 10)))
        choice = Modal(PROFILE_FORM_CHANGED_TITLE.format(name=stored), lines,
                       buttons=PROFILE_FORM_CHANGED_BUTTONS).run(win, self.palette, background=self._draw)
        if choice == "overwrite":
            forced = dataclasses.replace(outcome, expected=None)
            note = PROFILE_FORM_SAVED.format(name=forced.name)
            if self.callbacks.save is not None:
                try:
                    ok, note = self.callbacks.save(win, forced)
                except ProfileChanged:  # changed again meanwhile: ask again
                    self._changed(win, outcome)
                    return
                if not ok:
                    self._say(note)
                    return
            state.mark_saved(forced)
            self._say(note, "accent")
        elif choice == "reload" and self.callbacks.reload is not None:
            try:
                document, digest = self.callbacks.reload(stored)
            except (profile_mod.ProfileError, OSError) as exc:  # it may have become unloadable
                self._say(str(exc))
                return
            state.reload(document, digest)
            self._say(PROFILE_FORM_RELOADED.format(name=stored), "accent")

    # -- $EDITOR (the only subprocess the editor starts) ---------------------

    def _default_opener(self) -> Callable[[str], int]:
        environ = self.environ

        def open(path: str) -> int:
            editor = environ.get("VISUAL") or environ.get("EDITOR") or "vi"
            # The editor runs with the runtime environment.
            env = dict(environ)
            env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
            return subprocess.call([*shlex.split(editor), path], env=env)

        return open

    def _edit_json(self, win: Any) -> None:
        state = self.state
        before = strict_json.pretty_file_bytes(state.document)
        fd, path = tempfile.mkstemp(prefix="claude-multi-profile-", suffix=".json")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(before)
            suspender = self.suspender or suspended_curses
            opener = self.open_in_editor or self._default_opener()
            try:
                with suspender(win):
                    status = opener(path)
            except OSError as exc:
                self._say(f"cannot start $EDITOR: {exc}")
                return
            if status != 0:
                self._say(f"$EDITOR exited with status {status}; JSON not applied")
                return
            try:
                raw = Path(path).read_bytes()
            except OSError as exc:
                self._say(PROFILE_FORM_JSON_NOT_APPLIED.format(error=exc))
                return
            if raw == before:
                self._say("JSON unchanged.", "accent")
                return
            try:
                document = profile_mod.parse(strict_json.loads(raw))
            except profile_mod.ProfileValidationError as exc:
                self._say(PROFILE_FORM_JSON_NOT_APPLIED.format(error="; ".join(exc.errors)))
                return
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                self._say(PROFILE_FORM_JSON_NOT_APPLIED.format(error=exc))
                return
            state.replace_document(document)
            self._say("JSON applied from $EDITOR.", "accent")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- main loop -----------------------------------------------------------

    def _help(self, win: Any) -> None:
        evaluation = self.state.evaluation
        extra: list[str] = []
        if evaluation.errors:
            extra = [f"✗ {error}" for error in evaluation.errors]
        elif evaluation.lineup is not None:
            extra = [f"! {finding.message}" for finding in evaluation.lineup.warnings]
            duplicate = self.state.duplicate_fallback()
            if duplicate is not None:
                extra.append(duplicate)
            extra += [f"· {finding.message}" for finding in evaluation.lineup.notices]
        if extra:
            extra = ["Checks:", *extra]
        _help(win, self.palette, "edit profile — help", PROFILE_EDITOR_HELP, self._draw, extra=extra)

    def _leave(self, win: Any) -> bool:
        """Esc / ^C: True when the editor may close (clean, or discard confirmed)."""

        if not self.state.dirty:
            return True
        return bool(
            Modal(
                PROFILE_FORM_DISCARD_TITLE,
                [PROFILE_FORM_DISCARD_BODY],
                buttons=(("Discard", True), ("Keep editing", False)),
            ).run(win, self.palette, background=self._draw)
        )

    def run(self, win: Any) -> ProfileEditorOutcome | None:
        hide_cursor()
        while True:
            self._settle_focus()
            self._draw(win)
            key = read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                if self._leave(win):
                    return self.state.saved
                continue
            height, width = win.getmaxyx()
            need_rows, need_cols = self.min_size(width)
            if height < need_rows or width < need_cols:
                continue
            self.state.message = ""
            if key.kind == "up" or key.kind == "btab":
                self._move(-1)
            elif key.kind == "down" or key.kind == "tab":
                self._move(1)
            elif key.kind == "home":
                self._move(-len(catalog_mod.AGENT_ROLE_IDS) - 10)
            elif key.kind == "end":
                self._move(len(catalog_mod.AGENT_ROLE_IDS) + 10)
            elif key.kind == "enter":
                self._enter(win)
            elif key.kind == "ctrl" and key.ch in ("s", "o"):
                self._save(win)
            elif key.kind == "ctrl" and key.ch == "g":
                show_cursor()
                self._edit_json(win)
                hide_cursor()
            elif key.kind == "char" and key.ch in ("r", "R"):
                self._routing(win)
            elif key.kind == "char" and key.ch in ("n", "N"):
                self._named(win)
            elif key.kind == "char" and key.ch in ("u", "U"):
                if self.focus == "lead":
                    self._say(PROFILE_FORM_LEAD_UNBIND)
                elif self.focus in catalog_mod.AGENT_ROLE_IDS:
                    self.state.unbind(self.focus)
            elif key.kind == "char" and key.ch == "?":
                self._help(win)


def run_profile_editor(
    state: ProfileEditorState,
    *,
    input_stream: Any = None,
    output_stream: Any = None,
    environ: Mapping[str, str] | None = None,
    palette: Palette | None = None,
    callbacks: ProfileEditorCallbacks | None = None,
    no_color: bool = False,
    open_in_editor: Callable[[str], int] | None = None,
    initial_focus: str | None = None,
    on_start: Callable[[], None] | None = None,
) -> ProfileEditorOutcome | None:
    """Run the profile editor in its own curses session (the standalone entry, §4.2).

    Returns the last saved outcome (None when nothing was saved, on Esc, or
    on Ctrl+C / SIGINT).  The terminal is always restored.  ``on_start`` is
    called once curses is up, before the first frame: a ``CursesError`` or
    ``OSError`` raised before it means curses could not start here; one
    raised after it came from the session itself (a store write, the
    propagation prompt) and must be reported, never fallen back from.
    """

    environ = dict(os.environ if environ is None else environ)
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    if palette is None:
        palette = detect_palette(environ, no_color=no_color, tty_in=input_stream, tty_out=output_stream)
    screen = ProfileEditorScreen(
        state,
        palette=palette,
        callbacks=callbacks,
        environ=environ,
        open_in_editor=open_in_editor,
        initial_focus=initial_focus,
    )

    def app(stdscr: Any) -> ProfileEditorOutcome | None:
        if on_start is not None:
            on_start()
        return screen.run(stdscr)

    try:
        return run_curses_on_streams(app, input_stream, output_stream, palette=palette)
    except KeyboardInterrupt:
        return state.saved


class TextView:
    """Read-only, wrapped details. No truncation, I/O, or state authority."""

    def __init__(self, title, lines, *, palette=None, confirm=False):
        self.title, self.lines = title, tuple(lines)
        self.palette = palette or MONO_PALETTE
        self.offset = 0
        self.confirm = confirm

    def _wrapped(self, width):
        return [part for line in self.lines
                for part in (textwrap.wrap(visible_text(str(line)), max(1, width - 4),
                                          replace_whitespace=False) or [""])]

    def _draw(self, win):
        height, width = win.getmaxyx()
        win.erase()
        if height < 10 or width < 40:
            _too_small(win, self.palette, "details", 10, 40)
            return
        safe_add(win, 1, 2, self.title, self.palette.attr("accent"))
        safe_add(win, 2, 2, views.rule(width), self.palette.attr("dim"))
        lines = self._wrapped(width)
        self.offset = min(self.offset, max(0, len(lines) - (height - 5)))
        for y, line in enumerate(lines[self.offset:self.offset + height - 5], 3):
            safe_add(win, y, 2, line, self.palette.attr("normal"))
        bar = (("Enter", "declare"), ("Esc", "cancel")) if self.confirm == "declare" else (
            (("y", "proceed"), ("N", "cancel"), ("Esc", "cancel")) if self.confirm else (
            ("Up/Down", "scroll"), ("PgUp/PgDn", "page"), ("Home/End", ""), ("Esc", "back")))
        if self.offset + height - 5 < len(lines):
            safe_add(win, height - 2, 2, "More below — arrows/PgDn scroll; Home/End", self.palette.attr("dim"))
        KeyBar(bar).draw(win, height - 1, self.palette)
        win.refresh()

    def run(self, win):
        hide_cursor()
        while True:
            self._draw(win)
            key = read_key(win)
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return False
            height, width = win.getmaxyx()
            if height < 10 or width < 40:
                continue
            if self.confirm == "declare" and key.kind == "enter":
                return True
            if self.confirm is True and key.kind == "char" and key.ch in "yYnN":
                return key.ch in "yY"
            height, width = win.getmaxyx()
            if key.kind == "up":
                self.offset = max(0, self.offset - 1)
            elif key.kind == "down":
                self.offset += 1
            elif key.kind == "pageup":
                self.offset = max(0, self.offset - max(1, height - 5))
            elif key.kind == "pagedown":
                self.offset += max(1, height - 5)
            elif key.kind == "home":
                self.offset = 0
            elif key.kind == "end":
                self.offset = len(self._wrapped(width))


# What a text step draws in place of a value its field may not show (a key
# typed where only its variable's name belongs).
FIELD_HIDDEN = "(not shown: only A-Z, 0-9 and _ are drawn here — Backspace clears it)"


class OnboardingForm:
    """A draft-only stepper. Fields are (name, label, default, choices).

    Choices are (value, label) pairs. Backspace on a choice/empty input or
    Up returns to the previous step; edits are not declarations or grants.

    ``checks`` maps a field to its check at the field's boundary (the
    problem's value-free text, or None): Enter stays on a field whose value
    it refuses and shows why. ``shown`` maps a field to whether its value
    may be drawn as typed; one that may not is drawn as
    :data:`FIELD_HIDDEN`, cleared by Backspace and, once refused, cleared.
    """

    DRAFT_FOOTER = "Draft only — nothing written before declaration preview."

    def __init__(self, title, fields, *, palette=None, help_text="", footer=DRAFT_FOOTER,
                 checks: Mapping[str, Callable[[str], str | None]] | None = None,
                 shown: Mapping[str, Callable[[str], bool]] | None = None):
        self.title, self.fields, self.help_text = title, tuple(fields), help_text
        self.footer = footer
        self.palette = palette or MONO_PALETTE
        self.index = 0
        self.inputs = {name: TextInput(str(default), max_length=2048)
                       for name, _label, default, _choices in self.fields}
        self.checks = dict(checks or {})
        self.shown = dict(shown or {})
        self.message = ""  # why the current step refused its value
        self._message_step = -1

    def _hidden(self, name: str) -> bool:
        show = self.shown.get(name)
        return show is not None and not show(self.inputs[name].value)

    def _refused(self, name: str) -> bool:
        """Run the field's check; a refused value keeps the form on it."""

        check = self.checks.get(name)
        problem = check(self.inputs[name].value) if check is not None else None
        if problem is None:
            self.message = ""
            return False
        if self._hidden(name):
            self.inputs[name] = TextInput("", max_length=2048)  # never kept, never drawn
        self.message, self._message_step = problem, self.index
        return True

    def _draw(self, win):
        hide_cursor()
        height, width = win.getmaxyx()
        win.erase()
        cursor = None
        floor = max(14, len(self.fields[self.index][3]) + 11)
        if height < floor or width < 50:
            _too_small(win, self.palette, "onboarding", floor, 50)
            return
        name, label, _default, choices = self.fields[self.index]
        safe_add(win, 1, 2, self.title, self.palette.attr("accent"))
        safe_add(win, 2, 2, views.rule(width), self.palette.attr("dim"))
        safe_add(win, 4, 2, f"Step {self.index + 1}/{len(self.fields)} — {label}", self.palette.attr("normal"))
        value = self.inputs[name].value
        if choices:
            for y, (option, text) in enumerate(choices, 6):
                safe_add(win, y, 2, ("> " if value == option else "  ") + text,
                         self.palette.attr("accent" if value == option else "normal"))
        elif self._hidden(name):
            safe_add(win, 6, 2, views.clip(FIELD_HIDDEN, width - 4), self.palette.attr("warn"))
        else:
            cursor = self.inputs[name].draw_text(
                win, 6, 2, width - 4, attr=self.palette.attr("accent"),
                highlight_cursor=True, end_marker="_")
        if self.message and self._message_step == self.index:
            for y, line in enumerate(textwrap.wrap(self.message, max(10, width - 4))[:max(0, height - 13)], 8):
                safe_add(win, y, 2, line, self.palette.attr("warn"))
        safe_add(win, height - 4, 2, self.footer, self.palette.attr("dim"))
        # In a text step ? is typed (a URL query, say): F1 is help there.
        help_key = "?" if choices else "F1"
        KeyBar((("Enter", "next"), ("Backspace", "previous"), (help_key, "help"), ("Esc", "cancel"))).draw(
            win, height - 1, self.palette)
        if cursor is not None:
            try:
                win.move(*cursor)
                show_cursor()
            except CursesError:
                pass
        win.refresh()

    def run(self, win):
        try:
            return self._run(win)
        finally:
            hide_cursor()

    def _run(self, win):
        while True:
            self._draw(win)
            key = read_key(win)
            name, _label, _default, choices = self.fields[self.index]
            field = self.inputs[name]
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return None
            height, width = win.getmaxyx()
            if height < max(14, len(choices) + 11) or width < 50:
                continue
            if key.kind == "f1" or (key.kind == "char" and key.ch == "?" and choices):
                TextView(self.title + " — help", self.help_text.splitlines(), palette=self.palette).run(win)
            elif key.kind == "enter":
                if self._refused(name):
                    continue
                if self.index == len(self.fields) - 1:
                    return {name: value.value for name, value in self.inputs.items()}
                self.index += 1
            elif key.kind == "backspace" and not choices and field.value and self._hidden(name):
                self.inputs[name] = TextInput("", max_length=2048)
            elif key.kind == "backspace" and (choices or not field.value):
                self.index = max(0, self.index - 1)
            elif choices and key.kind in ("up", "down", "left", "right"):
                values = [value for value, _ in choices]
                i = values.index(field.value) if field.value in values else 0
                field.value = values[(i + (1 if key.kind in ("down", "right") else -1)) % len(values)]
            elif key.kind == "up":
                self.index = max(0, self.index - 1)
            elif not choices:
                field.handle(key)
