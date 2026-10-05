"""Widget-layer tests for claude-multi's TUI (curses test double, no terminal).

The :class:`FakeWindow` double implements the exact window protocol the
widgets use (``getmaxyx``/``addstr``/``erase``/``refresh``/``get_wch``/
``move``/``keypad``) against an in-memory cell grid, so screens and widgets
are driven with scripted keys and asserted on rendered text and attributes.
"""

from __future__ import annotations

import curses
import io
import os
import pty
import threading
import unittest
import unittest.mock

from claude_multi import tui

# Scripted key helpers: characters are fed to get_wch verbatim; special keys
# use the real curses constants (available without initscr).
ENTER = "\n"
ESC = "\x1b"
TAB = "\t"
BACKSPACE = "\x7f"
CTRL_G = "\x07"
CTRL_O = "\x0f"
DOWN = curses.KEY_DOWN
UP = curses.KEY_UP
LEFT = curses.KEY_LEFT
RIGHT = curses.KEY_RIGHT
HOME = curses.KEY_HOME
END = curses.KEY_END
BTAB = curses.KEY_BTAB
DELETE = curses.KEY_DC


class FakeWindow:
    """In-memory curses window: records cells + attrs, replays scripted keys."""

    def __init__(self, keys=(), *, height=30, width=90):
        self.height = height
        self.width = width
        self.keys = list(keys)
        self.grid: dict[tuple[int, int], tuple[str, int]] = {}
        self.frames: list[str] = []
        self.cursor_pos: tuple[int, int] | None = None
        self.keypad_flag = False

    def getmaxyx(self):
        return (self.height, self.width)

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("addstr out of bounds")
        for offset, ch in enumerate(text):
            if x + offset >= self.width:
                raise curses.error("addstr past right edge")
            self.grid[(y, x + offset)] = (ch, attr)

    def erase(self):
        self.grid = {}

    def refresh(self):
        self.frames.append(self.text())

    def get_wch(self):
        if not self.keys:
            if getattr(self, "_timeout_armed", False):
                raise curses.error("no input")
            raise AssertionError("FakeWindow key script exhausted")
        return self.keys.pop(0)

    def timeout(self, ms):
        # Model curses: with a timeout armed, an empty queue raises
        # curses.error instead of blocking forever; -1 blocks again.
        self._timeout_armed = ms >= 0

    def move(self, y, x):
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("move out of bounds")
        self.cursor_pos = (y, x)

    def keypad(self, flag):
        self.keypad_flag = flag

    # -- assertions ---------------------------------------------------------

    def line(self, y: int) -> str:
        return "".join(
            self.grid.get((y, x), (" ", 0))[0] for x in range(self.width)
        ).rstrip()

    def text(self) -> str:
        return "\n".join(self.line(y) for y in range(self.height))

    def attr_at(self, y: int, x: int) -> int:
        return self.grid.get((y, x), (" ", 0))[1]

    def find(self, needle: str) -> list[tuple[int, int]]:
        found = []
        for y in range(self.height):
            line = self.line(y)
            if needle in line:
                found.append((y, line.index(needle)))
        return found


class ReadKeyTests(unittest.TestCase):
    def feed(self, values):
        win = FakeWindow(values)
        return [tui.read_key(win) for _ in values]

    def test_character_and_control_mapping(self):
        kinds = self.feed(["a", ENTER, TAB, ESC, BACKSPACE, CTRL_G, DOWN, BTAB])
        self.assertEqual(
            [(k.kind, k.ch) for k in kinds],
            [
                ("char", "a"),
                ("enter", ""),
                ("tab", ""),
                ("esc", ""),
                ("backspace", ""),
                ("ctrl", "g"),
                ("down", ""),
                ("btab", ""),
            ],
        )

    def test_unknown_special_key(self):
        (key,) = self.feed([curses.KEY_F2])
        self.assertEqual(key.kind, "unknown")

    def test_f1_is_the_help_key_of_text_fields(self):
        (key,) = self.feed([curses.KEY_F1])
        self.assertEqual(key.kind, "f1")

    def test_unicode_character(self):
        (key,) = self.feed(["ö"])
        self.assertEqual((key.kind, key.ch), ("char", "ö"))


class ColourReplyTests(unittest.TestCase):
    """A terminal's late reply to the background colour query is read as a
    whole, never as Esc and typed text; real Esc, Alt chords and the keys
    after a reply are delivered unchanged."""

    def setUp(self):
        tui._carry_reply(None)
        self.addCleanup(tui._carry_reply, None)

    def keys(self, values, win=None):
        win = win or FakeWindow(values)
        out = []
        while win.keys or tui._pending(win):
            out.append((lambda key: (key.kind, key.ch))(tui.read_key(win)))
        return out

    def test_a_bel_terminated_reply_disappears(self):
        reply = list("\x1b]11;rgb:0000/0000/0000\x07")
        self.assertEqual(self.keys([*reply, "q"]), [("char", "q")])

    def test_an_st_terminated_reply_disappears(self):
        reply = list("\x1b]11;rgba:ffff/ffff/ffff/ffff\x1b\\")
        self.assertEqual(self.keys([*reply, "?"]), [("char", "?")])

    def test_a_foreground_reply_disappears_too(self):
        self.assertEqual(self.keys([*"\x1b]10;rgb:1/2/3\x07", ESC]), [("esc", "")])

    def test_a_lone_escape_stays_an_escape(self):
        self.assertEqual(self.keys([ESC]), [("esc", "")])

    def test_escape_then_a_letter_is_two_keys(self):
        self.assertEqual(self.keys([ESC, "x"]), [("esc", ""), ("char", "x")])

    def test_escape_then_a_bracket_that_is_not_a_reply_is_delivered(self):
        self.assertEqual(self.keys([ESC, "]", "a"]), [("esc", ""), ("char", "]"), ("char", "a")])

    def test_a_key_inside_a_reply_ends_it_and_is_delivered(self):
        self.assertEqual(self.keys([*"\x1b]11;rgb:00", "q"]), [("char", "q")])

    def test_a_reply_cut_short_is_bounded(self):
        win = FakeWindow([*"\x1b]11;", *("0" * (tui.OSC_MAX_CHARS + 5)), "z"])
        first = tui.read_key(win)
        # The bound stops the read: what is left is delivered as keys.
        self.assertEqual(first, tui.Key("char", "0"))

    def test_an_escape_that_does_not_terminate_ends_the_reply(self):
        self.assertEqual(self.keys([*"\x1b]11;rgb:0000/0000/0000", ESC, "x"]), [("char", "x")])

    def test_two_escapes_are_two_escapes(self):
        self.assertEqual(self.keys([ESC, ESC]), [("esc", ""), ("esc", "")])

    # The query stopped waiting before the reply ended: its rest arrives at
    # the key reader, after the screen is drawn.

    def test_a_late_st_terminator_is_part_of_the_reply(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/1e1e")
        self.assertEqual(self.keys([ESC, "\\", "?"]), [("char", "?")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_a_late_body_and_bel_are_part_of_the_reply(self):
        tui._carry_reply("]11;rgb:1e")
        self.assertEqual(self.keys([*"1e/1e1e/1e1e\x07", "q"]), [("char", "q")])

    def test_the_rest_of_a_cut_prefix_is_part_of_the_reply(self):
        tui._carry_reply("")
        self.assertEqual(self.keys([*"]11;rgb:0/0/0\x1b\\", "j"]), [("char", "j")])

    def test_a_key_that_cannot_continue_the_reply_is_delivered(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/1e1e")
        self.assertEqual(self.keys(["q", ESC]), [("char", "q"), ("esc", "")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_an_escape_without_its_backslash_stays_an_escape(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/1e1e")
        self.assertEqual(self.keys([ESC]), [("esc", "")])

    def test_a_resize_before_the_terminator_keeps_the_reply_open(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/1e1e")
        self.assertEqual(self.keys([curses.KEY_RESIZE, ESC, "\\", "q"]), [("resize", ""), ("char", "q")])

    def test_body_keys_paused_before_the_terminator_stay_part_of_the_reply(self):
        # A second gap inside the reply (its rest after the card is drawn,
        # then a pause before the terminator): the reply's bytes are never
        # replayed as keys (an "e" in it would open the editor), and the
        # terminator after the pause still closes it.
        tui._carry_reply("]11;rgb:1e1e/1e1e/")
        win = FakeWindow(list("1e1e"))
        tui._finish_carried_reply(win)
        self.assertEqual(tui._pending(win), [])
        self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:1e1e/1e1e/1e1e"])
        win.keys += [ESC, "\\", "?"]
        self.assertEqual(self.keys([], win=win), [("char", "?")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_a_reply_in_three_fragments_never_reaches_a_screen(self):
        # ESC ]11;rgb:0000/0000/ in the query's wait, 0000 at the key reader,
        # a pause, then ESC backslash.
        tui._carry_reply(tui._unfinished_reply(b"\x1b]11;rgb:0000/0000/"))
        win = FakeWindow(list("0000"))
        tui._finish_carried_reply(win)
        self.assertEqual(tui._pending(win), [])
        win.keys += [ESC, "\\", "q"]
        self.assertEqual(self.keys([], win=win), [("char", "q")])

    def test_an_escape_after_a_paused_reply_stays_an_escape(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/")
        win = FakeWindow(list("1e1e"))
        tui._finish_carried_reply(win)
        win.keys += [ESC]
        self.assertEqual(self.keys([], win=win), [("esc", "")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_a_key_after_a_paused_reply_is_delivered_alone(self):
        tui._carry_reply("]11;rgb:1e1e/1e1e/")
        win = FakeWindow(list("1e1e"))
        tui._finish_carried_reply(win)
        win.keys += ["q", "j"]
        self.assertEqual(self.keys([], win=win), [("char", "q"), ("char", "j")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_an_escape_then_a_prompt_key_after_a_cut_reply_are_both_delivered(self):
        # A real Esc read where the reply's terminator could start, and a key
        # right after it that is no backslash: the Esc comes first, then the
        # key, and none of the reply's bytes.
        for key, kind in (("\r", ("enter", "")), ("\n", ("enter", "")), ("q", ("char", "q")),
                          (ESC, ("esc", "")), ("0", ("char", "0"))):
            with self.subTest(key=repr(key)):
                tui._carry_reply(tui._unfinished_reply(b"\x1b]11;rgb:0000/0000/"))
                self.assertEqual(self.keys([ESC, key]), [("esc", ""), kind])
                self.assertEqual(tui._REPLY_TAIL, [])
        tui._carry_reply("]11;rgb:1e1e/1e1e/")
        self.assertEqual(self.keys([*"1e1e", ESC, curses.KEY_RESIZE]), [("esc", ""), ("resize", "")])

    def test_a_carried_escape_is_a_key_unless_a_backslash_follows(self):
        # The query's read (or the key reader's gap) stopped right after an
        # Esc: a backslash next closes the reply, any other key follows the Esc.
        tui._carry_reply(tui._unfinished_reply(b"\x1b]11;rgb:0000/0000/0000\x1b"))
        self.assertEqual(self.keys(["\\", "q"]), [("char", "q")])
        tui._carry_reply(tui._unfinished_reply(b"\x1b]11;rgb:0000/0000/0000\x1b"))
        self.assertEqual(self.keys(["\r"]), [("esc", ""), ("enter", "")])
        tui._carry_reply("]11;rgb:0000/0000/0000\x1b", since=tui._clock() - tui.OSC_CARRY_SECONDS - 1)
        self.assertEqual(self.keys(["\r"]), [("esc", ""), ("enter", "")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_body_keys_long_after_an_unfinished_reply_are_delivered(self):
        # A reply that never finished is not waited for: keys typed later are input.
        tui._carry_reply("]11;rgb:1e1e/1e1e/1e1e", since=tui._clock() - tui.OSC_CARRY_SECONDS - 1)
        self.assertEqual(self.keys(["a", "b"]), [("char", "a"), ("char", "b")])
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_a_reply_paused_inside_its_body_keeps_its_terminator(self):
        win = FakeWindow(list("\x1b]11;rgb:1e1e/1e1e/1e1e"))
        self.assertEqual(tui._next_value(win), ESC)
        self.assertTrue(tui._swallow_osc_reply(win))
        self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:1e1e/1e1e/1e1e"])
        win.keys += [ESC, "\\", "?"]
        self.assertEqual(self.keys([], win=win), [("char", "?")])

    def test_the_unfinished_part_of_a_query_read(self):
        self.assertEqual(tui._unfinished_reply(b"\x1b]11;rgb:1e1e/1e1e/1e1e"), "]11;rgb:1e1e/1e1e/1e1e")
        self.assertEqual(tui._unfinished_reply(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b"), "]11;rgb:1e1e/1e1e/1e1e\x1b")
        self.assertEqual(tui._unfinished_reply(b"\x1b"), "")
        self.assertIsNone(tui._unfinished_reply(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\"))
        self.assertIsNone(tui._unfinished_reply(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07"))
        self.assertIsNone(tui._unfinished_reply(b""))
        self.assertIsNone(tui._unfinished_reply(b"garbage"))


class ColourQueryGuardTests(unittest.TestCase):
    def test_line_mode_ssh_and_no_colour_never_query(self):
        base = {"TERM": "xterm-256color"}
        self.assertTrue(tui.osc_query_allowed(base))
        self.assertFalse(tui.osc_query_allowed(base, query=False))
        self.assertFalse(tui.osc_query_allowed(base, no_color=True))
        self.assertFalse(tui.osc_query_allowed({**base, "NO_COLOR": "1"}))
        for marker in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            with self.subTest(marker=marker):
                self.assertFalse(tui.osc_query_allowed({**base, marker: "x"}))
        self.assertFalse(tui.osc_query_allowed({"TERM": "dumb"}))

    def test_detect_palette_skips_the_query_when_not_allowed(self):
        with unittest.mock.patch.object(tui, "query_background_osc11", side_effect=AssertionError("no query")):
            stream = unittest.mock.Mock()
            stream.fileno.return_value = 0
            palette = tui.detect_palette({"TERM": "xterm-256color", "SSH_TTY": "/dev/pts/9"},
                                         tty_in=stream, tty_out=stream)
            self.assertEqual(palette.name, "dark")
            palette = tui.detect_palette({"TERM": "xterm-256color"}, tty_in=stream, tty_out=stream, query=False)
            self.assertEqual(palette.name, "dark")


class TerminalNoticeTests(unittest.TestCase):
    def tty(self):
        stream = unittest.mock.Mock()
        stream.isatty.return_value = True
        stream.fileno.return_value = -1
        return stream

    def test_an_unknown_type_names_it_quoted_and_suggests_one(self):
        with unittest.mock.patch.object(tui, "terminal_known", return_value=False):
            text = tui.term_notice({"TERM": "no such\x1bterm"}, self.tty(), self.tty())
        self.assertIn("TERM='no such", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("TERM=xterm-256color", text)
        self.assertIn("line interface", text)

    def test_unset_dumb_and_known(self):
        self.assertIn("TERM is not set", tui.term_notice({}, self.tty(), self.tty()))
        self.assertIsNone(tui.term_notice({"TERM": "dumb"}, self.tty(), self.tty()))
        with unittest.mock.patch.object(tui, "terminal_known", return_value=True):
            self.assertIsNone(tui.term_notice({"TERM": "xterm"}, self.tty(), self.tty()))
        pipe = unittest.mock.Mock()
        pipe.isatty.return_value = False
        self.assertIsNone(tui.term_notice({"TERM": "bogus"}, pipe, pipe))

    def test_an_unknown_type_is_not_curses_capable(self):
        with unittest.mock.patch.object(tui, "terminal_known", return_value=False):
            self.assertFalse(tui.streams_curses_capable(self.tty(), self.tty(), {"TERM": "bogus"}))


class ModalDefaultFocusTests(unittest.TestCase):
    def test_enter_picks_the_default_button(self):
        modal = tui.Modal("Forget?", ["x"], buttons=(("Forget", True), ("Cancel", False)), default=False)
        self.assertFalse(modal.run(FakeWindow([ENTER]), tui.MONO_PALETTE))
        modal = tui.Modal("Forget?", ["x"], buttons=(("Forget", True), ("Cancel", False)))
        self.assertTrue(modal.run(FakeWindow([ENTER]), tui.MONO_PALETTE))

    def test_an_unknown_default_focuses_the_first(self):
        modal = tui.Modal("t", [], buttons=(("A", 1), ("B", 2)), default=9)
        self.assertEqual(modal.focus, 1)


class PaletteTests(unittest.TestCase):
    def test_colorfgbg_background_detection(self):
        self.assertEqual(tui.background_from_colorfgbg("15;0"), "dark")
        self.assertEqual(tui.background_from_colorfgbg("0;15"), "light")
        self.assertEqual(tui.background_from_colorfgbg("0;7"), "light")
        self.assertEqual(tui.background_from_colorfgbg("12;8"), "dark")
        self.assertEqual(tui.background_from_colorfgbg("1;14"), "light")
        self.assertIsNone(tui.background_from_colorfgbg(""))
        self.assertIsNone(tui.background_from_colorfgbg("not-a-number"))

    def test_osc11_response_parsing(self):
        self.assertEqual(
            tui.parse_osc11_response(b"\x1b]11;rgb:ffff/ffff/ffff\x07"), "light"
        )
        self.assertEqual(
            tui.parse_osc11_response(b"\x1b]11;rgb:0000/0000/0000\x1b\\"), "dark"
        )
        self.assertEqual(
            tui.parse_osc11_response(b"\x1b]11;rgb:2a2a/2a2a/2a2a\x07"), "dark"
        )
        self.assertEqual(
            tui.parse_osc11_response(b"\x1b]11;rgba:ffff/ffff/ffff/ffff\x07"),
            "light",
        )
        self.assertIsNone(tui.parse_osc11_response(b"garbage"))

    def test_osc11_query_roundtrip_over_pty(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        result: dict[str, str | None] = {}

        def ask():
            result["value"] = tui.query_background_osc11(slave, slave)

        thread = threading.Thread(target=ask)
        thread.start()
        query = os.read(master, 64)
        self.assertIn(b"]11;?", query)
        os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff\x07")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["value"], "light")

    def test_osc11_query_remembers_a_reply_cut_off_by_its_wait(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.addCleanup(tui._carry_reply, None)
        result: dict[str, str | None] = {}

        def ask():
            result["value"] = tui.query_background_osc11(slave, slave)

        thread = threading.Thread(target=ask)
        thread.start()
        self.assertIn(b"]11;?", os.read(master, 64))
        os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff")  # the terminator comes later
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["value"], "light")
        self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:ffff/ffff/ffff"])

    def test_osc11_query_timeout_defaults_none(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.assertIsNone(tui.query_background_osc11(slave, slave, timeout=0.05))

    def test_detect_palette_priority(self):
        self.assertIs(tui.detect_palette({"NO_COLOR": "1"}), tui.MONO_PALETTE)
        self.assertIs(
            tui.detect_palette({}, no_color=True), tui.MONO_PALETTE
        )
        self.assertIs(
            tui.detect_palette({"COLORFGBG": "0;15"}), tui.LIGHT_PALETTE
        )
        self.assertIs(tui.detect_palette({"COLORFGBG": "15;0"}), tui.DARK_PALETTE)
        # No COLORFGBG and streams without fds: default dark, no crash.
        self.assertIs(
            tui.detect_palette({}, tty_in=io.StringIO(), tty_out=io.StringIO()),
            tui.DARK_PALETTE,
        )

    def test_role_attrs_and_mono_text_form(self):
        self.assertEqual(tui.DARK_PALETTE.attr("accent"), (1 << 8) | curses.A_BOLD)
        self.assertEqual(tui.DARK_PALETTE.attr("error"), (4 << 8) | curses.A_BOLD)
        self.assertEqual(tui.LIGHT_PALETTE.attr("accent"), (1 << 8) | curses.A_BOLD)
        self.assertEqual(tui.LIGHT_PALETTE.attr("error"), 4 << 8)
        for role in ("accent", "ok", "warn", "error", "dim"):
            self.assertEqual(tui.MONO_PALETTE.attr(role), 0)
        self.assertEqual(tui.DARK_PALETTE.attr("nonsense"), 0)

    def test_ansi_styling_and_mono_verbatim(self):
        self.assertEqual(tui.DARK_PALETTE.ansi("Ready", "ok"), "\x1b[32mReady\x1b[0m")
        self.assertEqual(
            tui.LIGHT_PALETTE.ansi("BLOCKED", "error"), "\x1b[31mBLOCKED\x1b[0m"
        )
        self.assertEqual(tui.MONO_PALETTE.ansi("Ready", "ok"), "Ready")

    def test_init_curses_colors_without_terminal_is_safe(self):
        # has_colors() raises before initscr; the helper must swallow it.
        tui.init_curses_colors(tui.DARK_PALETTE)
        tui.init_curses_colors(tui.MONO_PALETTE)


class DimVisibilityTests(unittest.TestCase):
    """Muted text must stay readable on dark terminal backgrounds."""

    def test_dim_ansi_is_mid_gray_not_bright_black(self):
        self.assertEqual(
            tui.DARK_PALETTE.ansi("x", "dim"), "\x1b[38;5;245mx\x1b[0m"
        )

    def test_dim_foreground_256_color_gray(self):
        with unittest.mock.patch.object(tui.curses, "COLORS", 256, create=True):
            self.assertEqual(tui._dim_foreground("dark"), 245)
            # Light palettes get a darker muted gray: 245 on white is too
            # faint (~2.7:1 contrast).
            self.assertEqual(tui._dim_foreground("light"), 240)

    def test_dim_foreground_8_color_fallback_never_black_on_dark(self):
        with unittest.mock.patch.object(tui.curses, "COLORS", 8, create=True):
            self.assertEqual(tui._dim_foreground("dark"), curses.COLOR_WHITE)
            self.assertEqual(tui._dim_foreground("light"), curses.COLOR_BLACK)

    def test_init_pair_uses_capability_aware_dim(self):
        pairs = {}

        def fake_init_pair(pair, fg, bg):
            pairs[pair] = (fg, bg)

        with (
            unittest.mock.patch.object(tui.curses, "has_colors", return_value=True),
            unittest.mock.patch.object(tui.curses, "start_color"),
            unittest.mock.patch.object(tui.curses, "use_default_colors"),
            unittest.mock.patch.object(tui.curses, "init_pair", fake_init_pair),
            unittest.mock.patch.object(tui.curses, "COLORS", 256, create=True),
        ):
            tui.init_curses_colors(tui.DARK_PALETTE)
        self.assertEqual(pairs[tui._PAIR_BY_ROLE["dim"]], (245, -1))
        self.assertEqual(
            pairs[tui._PAIR_BY_ROLE["accent"]], (curses.COLOR_CYAN, -1)
        )

    def test_init_pair_dim_falls_back_to_white_on_dark_8_color(self):
        pairs = {}

        def fake_init_pair(pair, fg, bg):
            pairs[pair] = (fg, bg)

        with (
            unittest.mock.patch.object(tui.curses, "has_colors", return_value=True),
            unittest.mock.patch.object(tui.curses, "start_color"),
            unittest.mock.patch.object(
                tui.curses, "use_default_colors", side_effect=curses.error
            ),
            unittest.mock.patch.object(tui.curses, "init_pair", fake_init_pair),
            unittest.mock.patch.object(tui.curses, "COLORS", 8, create=True),
        ):
            tui.init_curses_colors(tui.DARK_PALETTE)
        # No default-colors support: background falls back to black, so the
        # dim foreground must NOT be black (the old black-on-black trap).
        self.assertEqual(
            pairs[tui._PAIR_BY_ROLE["dim"]], (curses.COLOR_WHITE, curses.COLOR_BLACK)
        )

    def test_table_headers_render_with_dim_role(self):
        win = FakeWindow()
        table = tui.Table(("name", "state"), [("alpha", "ok")])
        table.draw(win, 0, 2, 80, tui.DARK_PALETTE)
        self.assertEqual(
            win.attr_at(0, 2), tui.DARK_PALETTE.attr("dim") | curses.A_BOLD
        )
        self.assertIn("name", win.line(0))

    def test_streams_curses_capable(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        self.assertFalse(
            tui.streams_curses_capable(Tty(), Tty(), {"TERM": "dumb"})
        )
        self.assertFalse(tui.streams_curses_capable(Tty(), Tty(), {}))
        self.assertTrue(
            tui.streams_curses_capable(Tty(), Tty(), {"TERM": "xterm-256color"})
        )
        self.assertFalse(
            tui.streams_curses_capable(
                io.StringIO(), Tty(), {"TERM": "xterm-256color"}
            )
        )


class LabelBadgeKeyBarTests(unittest.TestCase):
    def test_label_and_badge_render_with_role_attrs(self):
        win = FakeWindow()
        tui.Label("hello", "dim").draw(win, 0, 0, 80, tui.DARK_PALETTE)
        width = tui.Badge("durable(g2)", "ok").draw(win, 1, 2, tui.DARK_PALETTE)
        self.assertEqual(width, len("durable(g2)"))
        self.assertIn("hello", win.line(0))
        self.assertIn("durable(g2)", win.line(1))
        self.assertEqual(win.attr_at(0, 0), tui.DARK_PALETTE.attr("dim"))
        self.assertEqual(win.attr_at(1, 2), tui.DARK_PALETTE.attr("ok"))

    def test_keybar_text_and_accented_keys(self):
        bar = tui.KeyBar((("Enter", "launch"), ("Esc", "cancel")))
        self.assertEqual(bar.text(), "Enter launch · Esc cancel")
        win = FakeWindow()
        bar.draw(win, 29, tui.DARK_PALETTE)
        self.assertIn("Enter launch · Esc cancel", win.line(29))
        self.assertEqual(win.attr_at(29, 2), tui.DARK_PALETTE.attr("accent"))

    def test_keybar_clips_to_width(self):
        bar = tui.KeyBar(tuple((f"K{i}", f"action-{i}") for i in range(20)))
        win = FakeWindow(width=30)
        bar.draw(win, 29, tui.MONO_PALETTE)  # must not raise


class CheckboxTests(unittest.TestCase):
    def test_checkbox_markers_are_the_text_form(self):
        win = FakeWindow()
        tui.Checkbox("native workflows", checked=True).draw(win, 0, 0, tui.MONO_PALETTE)
        tui.Checkbox("off option", checked=False).draw(win, 1, 0, tui.MONO_PALETTE)
        self.assertIn("[x] native workflows", win.line(0))
        self.assertIn("[ ] off option", win.line(1))

    def test_radio_markers(self):
        win = FakeWindow()
        tui.Checkbox("replace", checked=True, radio=True).draw(
            win, 0, 0, tui.MONO_PALETTE
        )
        self.assertIn("(•) replace", win.line(0))

    def test_group_toggle_and_radio_exclusivity(self):
        boxes = [
            tui.Checkbox("replace", checked=True, radio=True),
            tui.Checkbox("native", radio=True),
            tui.Checkbox("off", radio=True),
        ]
        group = tui.CheckboxGroup(boxes, horizontal=True)
        group.activate(2)
        self.assertEqual(group.checked_indexes(), [2])
        group.activate(1)
        self.assertEqual(group.checked_indexes(), [1])
        plain = tui.CheckboxGroup([tui.Checkbox("a"), tui.Checkbox("b")])
        plain.activate(0)
        plain.activate(1)
        self.assertEqual(plain.checked_indexes(), [0, 1])

    def test_group_navigation_and_disabled_options(self):
        boxes = [tui.Checkbox("a"), tui.Checkbox("b", enabled=False)]
        group = tui.CheckboxGroup(boxes, horizontal=True)
        self.assertTrue(group.handle(tui.Key("right")))
        self.assertEqual(group.cursor, 1)
        group.activate(1)  # disabled: no-op
        self.assertEqual(group.checked_indexes(), [])
        self.assertTrue(group.handle(tui.Key("char", " ")))
        self.assertEqual(group.checked_indexes(), [])
        self.assertFalse(group.handle(tui.Key("up")))


class TextInputTests(unittest.TestCase):
    def drive(self, keys, value=""):
        field = tui.TextInput(value)
        consumed = [field.handle(key) for key in keys]
        return field, consumed

    def test_insert_cursor_and_home_end(self):
        field, _ = self.drive(
            [tui.Key("char", "x"), tui.Key("home"), tui.Key("char", "y"), tui.Key("end"), tui.Key("char", "z")],
            "abc",
        )
        self.assertEqual(field.value, "yabcxz")

    def test_backspace_and_delete(self):
        # cursor starts at end: left, delete removes 'd'; backspace removes 'c'.
        field, _ = self.drive(
            [tui.Key("left"), tui.Key("delete"), tui.Key("backspace")],
            "abcd",
        )
        self.assertEqual(field.value, "ab")
        self.assertEqual(field.cursor, 2)

    def test_backspace_at_start_is_noop_but_consumed(self):
        field, consumed = self.drive([tui.Key("home"), tui.Key("backspace")], "ab")
        self.assertEqual(field.value, "ab")
        self.assertEqual(consumed, [True, True])

    def test_navigation_keys_are_not_consumed(self):
        field, consumed = self.drive(
            [tui.Key("up"), tui.Key("down"), tui.Key("tab"), tui.Key("enter"), tui.Key("esc")],
            "ab",
        )
        self.assertEqual(consumed, [False] * 5)
        self.assertEqual(field.value, "ab")

    def test_max_length(self):
        field, _ = self.drive(
            [tui.Key("char", "a"), tui.Key("char", "b"), tui.Key("char", "c")],
            "",
        )
        self.assertEqual(field.value, "abc")
        limited = tui.TextInput("", max_length=2)
        for ch in "xyz":
            limited.handle(tui.Key("char", ch))
        self.assertEqual(limited.value, "xy")

    def test_draw_reports_cursor_position(self):
        field = tui.TextInput("hello")
        field.handle(tui.Key("left"))
        win = FakeWindow()
        _row, col = field.draw(win, 3, 2, 12, tui.MONO_PALETTE, focused=True)
        self.assertIn("[hello", win.line(3))
        self.assertEqual((3, col), (3, 2 + 1 + 4))

    def test_horizontal_scroll_keeps_cursor_visible(self):
        field = tui.TextInput("x" * 40)
        win = FakeWindow()
        field.handle(tui.Key("home"))
        _row, col = field.draw(win, 0, 0, 12, tui.MONO_PALETTE, focused=True)
        self.assertLessEqual(col, 11)
        self.assertEqual(win.line(0)[1:11], "x" * 10)

    def test_full_field_end_cursor_stays_off_the_closing_bracket(self):
        # Regression: with a full field and the cursor at end-of-input the
        # reported cursor column must be the last field cell, never the "]".
        field = tui.TextInput("x" * 20)  # cursor starts at end
        win = FakeWindow()
        _row, col = field.draw(win, 0, 0, 12, tui.MONO_PALETTE, focused=True)
        # width 12: "[" at 0, 10 inner cells, "]" at 11.
        self.assertEqual(col, 10)
        self.assertEqual(win.line(0)[11], "]")


class SelectListTests(unittest.TestCase):
    def test_single_select_enter_returns_index(self):
        chooser = tui.SelectList(
            "Pick", [tui.SelectItem("a"), tui.SelectItem("b"), tui.SelectItem("c")]
        )
        win = FakeWindow([DOWN, ENTER])
        self.assertEqual(chooser.run(win, tui.MONO_PALETTE), 1)
        self.assertIn("> b", win.line(3))

    def test_esc_cancels(self):
        chooser = tui.SelectList("Pick", [tui.SelectItem("a")])
        win = FakeWindow([ESC])
        self.assertIsNone(chooser.run(win, tui.MONO_PALETTE))

    def test_activate_error_stays_open_with_message(self):
        calls = []

        def activate(index):
            calls.append(index)
            return "nope" if len(calls) == 1 else None

        chooser = tui.SelectList("Pick", [tui.SelectItem("a"), tui.SelectItem("b")])
        win = FakeWindow([ENTER, DOWN, ENTER])
        self.assertEqual(chooser.run(win, tui.MONO_PALETTE, on_activate=activate), 1)
        self.assertEqual(calls, [0, 1])
        self.assertTrue(any("nope" in frame for frame in win.frames))

    def test_multi_toggle_and_refresh(self):
        state = {"a": False, "b": True}

        def refresh():
            return [tui.SelectItem(k, checked=v) for k, v in state.items()]

        def toggle(index):
            key = list(state)[index]
            state[key] = not state[key]
            return None

        chooser = tui.SelectList("Multi", refresh(), multi=True)
        win = FakeWindow([" ", ENTER])
        # Multi-mode Enter returns the widget-tracked toggled indexes;
        # callback callers may ignore it (the editor works via on_toggle).
        self.assertEqual(
            chooser.run(win, tui.MONO_PALETTE, on_toggle=toggle, refresh=refresh),
            [0],
        )
        self.assertEqual(state, {"a": True, "b": True})
        self.assertTrue(any("[x] a" in frame for frame in win.frames))

    def test_prefer_and_help_callbacks(self):
        events = []
        chooser = tui.SelectList(
            "Multi", [tui.SelectItem("a", checked=True)], multi=True
        )
        win = FakeWindow(["p", "?", ENTER])
        chooser.run(
            win,
            tui.MONO_PALETTE,
            on_prefer=lambda i: events.append(("prefer", i)) or None,
            on_help=lambda: events.append(("help",)),
        )
        self.assertEqual(events, [("prefer", 0), ("help",)])

    def test_jk_navigation_and_home_end(self):
        chooser = tui.SelectList(
            "Pick", [tui.SelectItem(str(i)) for i in range(5)], selected=0
        )
        win = FakeWindow(["j", "j", "k", END, HOME, DOWN, ENTER])
        self.assertEqual(chooser.run(win, tui.MONO_PALETTE), 1)


class ModalTests(unittest.TestCase):
    def test_enter_activates_first_button_esc_cancels(self):
        modal = tui.Modal("Confirm", ["Body line"], buttons=(("Yes", True), ("No", False)))
        win = FakeWindow([ENTER])
        self.assertTrue(modal.run(win, tui.MONO_PALETTE))
        text = win.text()
        self.assertIn("Confirm", text)
        self.assertIn("Body line", text)
        self.assertIn("[ Yes ]", text)
        win2 = FakeWindow([ESC])
        self.assertIsNone(
            tui.Modal("Confirm", [], buttons=(("Yes", True),)).run(
                win2, tui.MONO_PALETTE
            )
        )

    def test_left_right_move_button_focus(self):
        modal = tui.Modal("Confirm", [], buttons=(("Yes", True), ("No", False)))
        win = FakeWindow([RIGHT, ENTER])
        self.assertFalse(modal.run(win, tui.MONO_PALETTE))

    def test_tab_cycles_and_input_editing(self):
        modal = tui.Modal(
            "Name",
            ["Target name:"],
            buttons=(("OK", True), ("Cancel", False)),
            input=tui.TextInput(""),
        )
        win = FakeWindow(list("new-name") + [ENTER, ENTER])
        self.assertTrue(modal.run(win, tui.MONO_PALETTE))
        self.assertEqual(modal.input.value, "new-name")
        self.assertIn("new-name", win.text())

    def test_tab_to_cancel(self):
        modal = tui.Modal(
            "Name",
            [],
            buttons=(("OK", True), ("Cancel", False)),
            input=tui.TextInput("seed"),
        )
        win = FakeWindow([TAB, TAB, ENTER])
        self.assertFalse(modal.run(win, tui.MONO_PALETTE))
        self.assertEqual(modal.input.value, "seed")

    def test_modal_draws_border(self):
        modal = tui.Modal("Titled", ["body"])
        win = FakeWindow([ESC])
        modal.run(win, tui.MONO_PALETTE)
        text = win.text()
        self.assertIn("+", text)
        self.assertIn("|", text)


class TableTests(unittest.TestCase):
    def test_columns_align_and_selection_attr(self):
        table = tui.Table(
            ["session", "mode"],
            [["a1b2", "durable(g2)"], ["9c8e", "legacy"]],
            selected=1,
        )
        win = FakeWindow()
        used = table.draw(win, 2, 0, 60, tui.DARK_PALETTE)
        self.assertEqual(used, 3)
        self.assertIn("session", win.line(2))
        self.assertIn("a1b2", win.line(3))
        self.assertIn("durable(g2)", win.line(3))
        mode_col = win.line(4).index("legacy")
        self.assertTrue(win.attr_at(4, mode_col) & curses.A_REVERSE)
        self.assertFalse(win.attr_at(3, 0) & curses.A_REVERSE)

    def test_handle_moves_selection(self):
        table = tui.Table(["c"], [["1"], ["2"], ["3"]])
        self.assertTrue(table.handle(tui.Key("down")))
        self.assertEqual(table.selected, 1)
        table.handle(tui.Key("end"))
        self.assertEqual(table.selected, 2)
        table.handle(tui.Key("up"))
        self.assertEqual(table.selected, 1)
        table.handle(tui.Key("home"))
        self.assertEqual(table.selected, 0)
        self.assertFalse(table.handle(tui.Key("left")))

    def test_columns_shrink_to_width(self):
        table = tui.Table(
            ["averylongcolumnname", "anotherlongcolumnname"],
            [["x" * 60, "y" * 60]],
        )
        win = FakeWindow(width=40)
        table.draw(win, 0, 0, 40, tui.MONO_PALETTE)
        self.assertLessEqual(len(win.line(1)), 40)


class KeyBarOverflowTests(unittest.TestCase):
    """2.14.0: overflow keeps the exit AND the help binding visible."""

    def test_overflow_keeps_help_and_exit(self):
        bar = tui.KeyBar(
            (
                ("Enter", "launch"),
                ("E", "edit"),
                ("D", "details"),
                ("S", "sessions"),
                ("G", "gateway models"),
                ("H", "health"),
                ("?", "help"),
                ("Esc", "cancel"),
            )
        )
        win = FakeWindow(height=24, width=44)
        bar.draw(win, 23, tui.DARK_PALETTE)
        text = win.text()
        self.assertIn("? help", text)
        self.assertIn("Esc cancel", text)
        self.assertIn("…", text)  # middle bindings compacted

    def test_overflow_without_help_protects_exit_only(self):
        bar = tui.KeyBar(
            (("Enter", "launch"), ("D", "details"), ("Esc", "cancel"))
        )
        win = FakeWindow(height=24, width=20)
        bar.draw(win, 23, tui.DARK_PALETTE)
        self.assertIn("Esc cancel", win.text())


class RunCursesOnStreamsTests(unittest.TestCase):
    def test_fd_less_streams_are_rejected(self):
        with self.assertRaises(OSError):
            tui.run_curses_on_streams(
                lambda win: None, io.StringIO(), io.StringIO()
            )


class VisibleTextTests(unittest.TestCase):
    """The ONE visible-text sanitizer escapes every terminal-control
    byte class into a visible form; clean text is byte-identical."""

    def test_escape_becomes_caret_bracket(self):
        self.assertEqual(tui.visible_text("\x1b"), "^[")
        self.assertEqual(tui.visible_text("\x1b[2J"), "^[[2J")

    def test_osc_payload_is_flattened(self):
        payload = "\x1b]8;;https://evil.example\x07click\x1b]8;;\x07"
        self.assertEqual(
            tui.visible_text(payload),
            "^[]8;;https://evil.example^Gclick^[]8;;^G",
        )
        self.assertNotIn("\x1b", tui.visible_text(payload))
        self.assertNotIn("\x07", tui.visible_text(payload))

    def test_c0_controls_and_newlines_use_caret_notation(self):
        self.assertEqual(tui.visible_text("a\nb\tc\rd"), "a^Jb^Ic^Md")
        self.assertEqual(tui.visible_text("\x00\x01\x1f"), "^@^A^_")

    def test_del_and_c1_controls(self):
        self.assertEqual(tui.visible_text("\x7f"), "^?")
        self.assertEqual(tui.visible_text("\x85"), "\\x85")
        self.assertEqual(tui.visible_text("\x9b"), "\\x9b")

    def test_clean_text_is_identical(self):
        for clean in (
            "default",
            "/repo/dot files/πroject",
            "cm-analyst-sol-high · high ★ preferred → durable(g2)",
            "ö",
        ):
            self.assertEqual(tui.visible_text(clean), clean)

    def test_safe_add_never_passes_control_bytes_to_the_window(self):
        win = FakeWindow()
        tui.safe_add(win, 0, 0, "evil\x1b[2J\x07name", 0)
        line = win.line(0)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)
        self.assertIn("evil^[[2J^Gname", line)

    def test_safe_add_still_renders_clean_text_and_attrs(self):
        win = FakeWindow()
        tui.safe_add(win, 1, 2, "durable scope", 7)
        self.assertIn("durable scope", win.line(1))
        self.assertEqual(win.attr_at(1, 2), 7)


if __name__ == "__main__":
    unittest.main()


class ModalRenderTests(unittest.TestCase):
    MONO_PALETTE = tui.MONO_PALETTE
    Modal = tui.Modal
    Key = tui.Key

    def test_modal_paints_interior_no_bleed_through(self) -> None:
        win = FakeWindow()
        # Pre-fill the whole window with background noise.
        for y in range(win.height):
            for x in range(win.width):
                win.grid[(y, x)] = ("Z", 0)
        modal = tui.Modal("Title", ["line one", "line two"], buttons=(("OK", True),))
        modal.draw(win, tui.MONO_PALETTE)
        top, left, box_h, box_w = modal._geometry(win)
        for row in range(top + 1, top + box_h - 1):
            for col in range(left + 1, left + box_w - 1):
                cell = win.grid.get((row, col), (" ", 0))[0]
                self.assertNotEqual(
                    cell, "Z", f"background leaked at {(row, col)}"
                )

    def test_modal_resize_erases_before_redraw(self) -> None:
        win = FakeWindow(keys=[curses.KEY_RESIZE, "\x1b"])
        modal = tui.Modal("Title", ["body"], buttons=(("OK", True),))
        modal.draw(win, tui.MONO_PALETTE)
        frames_before = len(win.frames)
        modal.run(win, tui.MONO_PALETTE)
        # The resize path erased the window: the frame right after erase is
        # the freshly redrawn dialog on an otherwise empty grid, and the old
        # geometry's border characters appear only in the final position.
        self.assertGreater(len(win.frames), frames_before)



    def test_multi_toggle_without_items_checked_still_renders_markers(self):
        # A multi picker built from bare SelectItems (checked=None)
        # must render [x]/[ ] from the widget-tracked toggled set — toggling
        # must never be invisible.
        chooser = tui.SelectList(
            "Multi", [tui.SelectItem("a"), tui.SelectItem("b")], multi=True
        )
        win = FakeWindow([" ", ENTER])
        result = chooser.run(win, tui.MONO_PALETTE)
        self.assertEqual(result, [0])
        self.assertTrue(any("[x] a" in frame for frame in win.frames))
        self.assertTrue(any("[ ] b" in frame for frame in win.frames))


# ---------------------------------------------------------------------------
# Widget foundations, escdelay, ^S flow control, the confirmation panel and
# the curses-less import.


class SelectListColumnTests(unittest.TestCase):
    """Column 2, Esc the only exit, header/disabled skipping."""

    def test_title_rows_and_message_draw_at_column_two(self) -> None:
        chooser = tui.SelectList("Pick one", [tui.SelectItem("alpha", note="n")])
        chooser.message = "a message"
        win = FakeWindow(height=12, width=60)
        chooser.draw(win, tui.MONO_PALETTE)
        self.assertEqual(win.find("Pick one"), [(0, 2)])
        self.assertEqual(win.find("> alpha"), [(2, 2)])
        self.assertEqual(win.line(2), "  > alpha  n")
        (message_row,) = win.find("a message")
        self.assertEqual(message_row[1], 2)
        self.assertEqual(win.line(0)[:2], "  ")
        self.assertEqual(win.line(2)[:2], "  ")

    def test_q_is_not_an_exit_key(self) -> None:
        chooser = tui.SelectList("Pick", [tui.SelectItem("a"), tui.SelectItem("b")])
        win = FakeWindow(["q", "Q", DOWN, ENTER])
        self.assertEqual(chooser.run(win, tui.MONO_PALETTE), 1)
        esc_only = FakeWindow(["q", ESC])
        self.assertIsNone(
            tui.SelectList("Pick", [tui.SelectItem("a")]).run(esc_only, tui.MONO_PALETTE)
        )
        self.assertEqual(esc_only.keys, [])

    def _grouped(self):
        return [
            tui.SelectItem("group one", header=True),
            tui.SelectItem("a"),
            tui.SelectItem("b", enabled=False),
            tui.SelectItem("group two", header=True),
            tui.SelectItem("c"),
        ]

    def test_up_down_home_end_skip_headers_and_disabled_rows(self) -> None:
        items = self._grouped()
        selectable = [i for i, item in enumerate(items) if item.selectable]
        self.assertEqual(len(selectable), 2)
        first, last = selectable
        # Initial selection 0 is a header: settles forward onto the first row.
        self.assertEqual(
            tui.SelectList("G", items).run(FakeWindow([ENTER]), tui.MONO_PALETTE), first
        )
        self.assertEqual(
            tui.SelectList("G", items).run(FakeWindow([DOWN, ENTER]), tui.MONO_PALETTE),
            last,
        )
        # Down from the last selectable row wraps past the leading header.
        self.assertEqual(
            tui.SelectList("G", items).run(
                FakeWindow([DOWN, DOWN, ENTER]), tui.MONO_PALETTE
            ),
            first,
        )
        self.assertEqual(
            tui.SelectList("G", items).run(FakeWindow([UP, ENTER]), tui.MONO_PALETTE),
            last,
        )
        self.assertEqual(
            tui.SelectList("G", items).run(
                FakeWindow([END, "k", "j", HOME, ENTER]), tui.MONO_PALETTE
            ),
            first,
        )

    def test_header_row_is_dim_bold_at_column_two_and_never_focused(self) -> None:
        items = self._grouped()
        win = FakeWindow(height=14, width=60)
        tui.SelectList("G", items).draw(win, tui.DARK_PALETTE)
        (row, col), = win.find("group one")
        self.assertEqual(col, 2)
        self.assertEqual(
            win.attr_at(row, col), tui.DARK_PALETTE.attr("dim") | curses.A_BOLD
        )
        self.assertNotIn(">", win.line(row))

    def test_enter_on_a_list_without_selectable_rows_stays(self) -> None:
        chooser = tui.SelectList(
            "Only headers", [tui.SelectItem("h", header=True), tui.SelectItem("x", enabled=False)]
        )
        win = FakeWindow([ENTER, DOWN, ENTER, ESC])
        self.assertIsNone(chooser.run(win, tui.MONO_PALETTE))
        self.assertEqual(win.keys, [])
        self.assertIsNone(
            tui.SelectList("Empty", []).run(FakeWindow([ENTER]), tui.MONO_PALETTE)
        )

    def test_multi_space_never_toggles_a_header(self) -> None:
        chooser = tui.SelectList(
            "M", [tui.SelectItem("h", header=True), tui.SelectItem("a")], multi=True
        )
        chooser.selected = 0
        self.assertEqual(
            chooser.run(FakeWindow([" ", ENTER]), tui.MONO_PALETTE), [1]
        )


class TableExtensionTests(unittest.TestCase):
    """Gap / row_roles / spans / cursor / selectable, 2.x defaults."""

    ROWS = [["alpha", "one"], ["", ""], ["beta", "two"], ["gamma", "three"]]

    def test_defaults_are_the_2x_layout(self) -> None:
        win = FakeWindow()
        tui.Table(["name", "n"], [["alpha", "one"]]).draw(win, 0, 2, 60, tui.MONO_PALETTE)
        self.assertEqual(win.line(0), "  name    n")
        self.assertEqual(win.line(1), "  alpha   one")

    def test_gap_sets_the_column_separator(self) -> None:
        win = FakeWindow()
        table = tui.Table(["name", "n"], [["alpha", "one"]], gap=1)
        table.draw(win, 0, 2, 60, tui.MONO_PALETTE)
        self.assertEqual(win.line(0), "  name  n")
        self.assertEqual(win.line(1), "  alpha one")

    def test_start_scrolls_rows_without_moving_columns(self) -> None:
        # A scrolled window keeps the widths of every row.
        rows = [["long-name", "1"], ["b", "2"], ["", ""]]
        win = FakeWindow()
        table = tui.Table(["name", "n"], rows, selected=1, cursor=True, spans={2: ("span", "dim")})
        used = table.draw(win, 0, 2, 60, tui.MONO_PALETTE, max_rows=1, start=1)
        self.assertEqual(used, 2)
        self.assertEqual(win.line(0), "    " + "name".ljust(9) + "   n")
        self.assertEqual(win.line(1), "  › " + "b".ljust(9) + "   2")
        self.assertEqual(win.line(2), "")
        win = FakeWindow()
        table.draw(win, 0, 2, 60, tui.MONO_PALETTE, max_rows=5, start=2)
        self.assertEqual(win.line(1), "  span")

    def test_gap_is_used_by_the_width_algorithm(self) -> None:
        wide = tui.Table(["a", "b"], [["x" * 20, "y" * 20]], gap=3, min_widths=[1, 1])
        tight = tui.Table(["a", "b"], [["x" * 20, "y" * 20]], gap=1, min_widths=[1, 1])
        self.assertEqual(sum(wide._widths(40)) + 3, 40)
        self.assertEqual(sum(tight._widths(40)) + 1, 40)

    def test_row_roles_color_whole_rows(self) -> None:
        win = FakeWindow()
        table = tui.Table(
            ["name"], [["alpha"], ["beta"]], row_roles=["warn", None], selected=1
        )
        table.draw(win, 0, 2, 60, tui.DARK_PALETTE)
        self.assertEqual(win.attr_at(1, 2), tui.DARK_PALETTE.attr("warn"))
        self.assertEqual(
            win.attr_at(2, 2), tui.DARK_PALETTE.attr("normal") | curses.A_REVERSE
        )

    def test_spans_draw_full_width_and_are_never_selectable(self) -> None:
        win = FakeWindow()
        table = tui.Table(
            ["name", "n"], self.ROWS, spans={1: ("── a group label ──", "dim")}
        )
        table.draw(win, 0, 2, 60, tui.DARK_PALETTE)
        self.assertEqual(win.line(2), "  ── a group label ──")
        self.assertEqual(win.attr_at(2, 2), tui.DARK_PALETTE.attr("dim"))
        self.assertFalse(table.is_selectable(1))
        self.assertTrue(table.handle(tui.Key("down")))
        self.assertEqual(table.selected, 2)
        table.handle(tui.Key("up"))
        self.assertEqual(table.selected, 0)

    def test_cursor_marks_the_selected_row_and_shifts_the_block(self) -> None:
        win = FakeWindow()
        table = tui.Table(["name", "n"], [["alpha", "one"], ["beta", "two"]], cursor=True, selected=1)
        table.draw(win, 0, 2, 60, tui.MONO_PALETTE)
        self.assertEqual(win.line(0), "    name    n")
        self.assertEqual(win.line(1), "    alpha   one")
        self.assertEqual(win.line(2), "  › beta    two")

    def test_selectable_rows_are_skipped_by_handle(self) -> None:
        table = tui.Table(
            ["name", "n"], self.ROWS, selectable=[True, False, False, True]
        )
        table.handle(tui.Key("down"))
        self.assertEqual(table.selected, 3)
        table.handle(tui.Key("down"))
        self.assertEqual(table.selected, 0)
        table.handle(tui.Key("end"))
        self.assertEqual(table.selected, 3)
        table.handle(tui.Key("home"))
        self.assertEqual(table.selected, 0)
        table.handle(tui.Key("char", "k"))
        self.assertEqual(table.selected, 3)
        win = FakeWindow()
        table.selected = 1
        table.draw(win, 0, 0, 60, tui.DARK_PALETTE)
        self.assertFalse(win.attr_at(2, 0) & curses.A_REVERSE)


class _FakeTermios:
    """Records termios calls (and interleaves them with fd events)."""

    IXON = 0o2000
    ECHO = 0o10
    ICANON = 0o2
    TCSANOW = 0
    error = type("error", (Exception,), {})

    def __init__(self, events, iflag):
        self.events = events
        self.iflag = iflag

    def tcgetattr(self, fd):
        self.events.append(("tcgetattr", fd))
        return [self.iflag, 0, 0, self.ECHO | self.ICANON, 0, 0, []]

    def tcsetattr(self, fd, when, attrs):
        self.events.append(("tcsetattr", fd, attrs[0]))
        if fd == 0:
            self.iflag = attrs[0]


class _FdStream:
    def __init__(self, fd):
        self.fd = fd

    def fileno(self):
        return self.fd


class _SessionHarness:
    """Patches the fd/termios/curses edges of run_curses_on_streams."""

    def __init__(self, testcase, *, iflag, environ, termios_module=True):
        self.events: list[tuple] = []
        self.termios = _FakeTermios(self.events, iflag) if termios_module else None
        self.escdelay_calls: list[int] = []
        events = self.events
        next_dup = iter(range(100, 200))

        def fake_dup(fd):
            new = next(next_dup)
            events.append(("dup", fd, new))
            return new

        patches = [
            unittest.mock.patch.object(tui, "termios", self.termios),
            unittest.mock.patch.object(tui.os, "dup", fake_dup),
            unittest.mock.patch.object(tui.os, "dup2", lambda a, b: events.append(("dup2", a, b))),
            unittest.mock.patch.object(tui.os, "close", lambda fd: events.append(("close", fd))),
            unittest.mock.patch.object(tui.os, "isatty", lambda fd: True),
            unittest.mock.patch.dict(tui.os.environ, environ, clear=False),
            unittest.mock.patch.object(tui.curses, "wrapper", lambda func: func(FakeWindow())),
            unittest.mock.patch.object(
                tui.curses, "set_escdelay", lambda ms: self.escdelay_calls.append(ms), create=True
            ),
            unittest.mock.patch.object(
                tui.curses, "def_prog_mode", lambda: events.append(("def_prog_mode",))
            ),
        ]
        if "ESCDELAY" not in environ:
            saved = tui.os.environ.pop("ESCDELAY", None)
            if saved is not None:
                testcase.addCleanup(tui.os.environ.__setitem__, "ESCDELAY", saved)
        for patcher in patches:
            patcher.start()
            testcase.addCleanup(patcher.stop)

    def run(self, app, in_fd=0, out_fd=1):
        return tui.run_curses_on_streams(app, _FdStream(in_fd), _FdStream(out_fd))


class EscDelayTests(unittest.TestCase):
    """25 ms unless ESCDELAY is set; never written."""

    def test_sets_25_ms_when_the_environment_lacks_escdelay(self) -> None:
        harness = _SessionHarness(self, iflag=0, environ={}, termios_module=False)
        self.assertEqual(harness.run(lambda win: "done"), "done")
        self.assertEqual(harness.escdelay_calls, [tui.ESC_DELAY_MS])
        self.assertEqual(tui.ESC_DELAY_MS, 25)
        self.assertNotIn("ESCDELAY", tui.os.environ)

    def test_environment_escdelay_wins_and_is_not_overridden(self) -> None:
        harness = _SessionHarness(
            self, iflag=0, environ={"ESCDELAY": "600"}, termios_module=False
        )
        harness.run(lambda win: None)
        self.assertEqual(harness.escdelay_calls, [])
        self.assertEqual(tui.os.environ["ESCDELAY"], "600")

    def test_read_key_docstring_states_the_escape_delay(self) -> None:
        self.assertIn("25 ms escape delay", " ".join(tui.read_key.__doc__.split()))


class IxonTests(unittest.TestCase):
    """IXON off in the session, restored exactly."""

    def test_snapshot_after_dup2_and_restore_on_fd0_before_the_fd_restore(self) -> None:
        harness = _SessionHarness(self, iflag=_FakeTermios.IXON | 0o1, environ={})
        seen = {}

        def app(win):
            seen["iflag"] = harness.termios.iflag
            return "ok"

        self.assertEqual(harness.run(app, in_fd=7, out_fd=1), "ok")
        events = harness.events
        dup2_in = events.index(("dup2", 7, 0))
        snapshot = events.index(("tcgetattr", 0))
        self.assertLess(dup2_in, snapshot, events)
        # In the session: IXON cleared on fd 0 and curses told the new mode.
        self.assertEqual(seen["iflag"] & _FakeTermios.IXON, 0)
        self.assertIn(("tcsetattr", 0, 0o1), events)
        self.assertIn(("def_prog_mode",), events)
        # Restore targets fd 0, before fd 0 is pointed back at the saved dup.
        restore = max(
            i for i, event in enumerate(events) if event[0] == "tcsetattr"
        )
        self.assertEqual(events[restore], ("tcsetattr", 0, _FakeTermios.IXON | 0o1))
        (saved_dup,) = [event[2] for event in events if event[0] == "dup" and event[1] == 0]
        self.assertLess(restore, events.index(("dup2", saved_dup, 0)), events)
        self.assertEqual(harness.termios.iflag, _FakeTermios.IXON | 0o1)

    def test_restore_runs_when_the_app_raises(self) -> None:
        harness = _SessionHarness(self, iflag=_FakeTermios.IXON, environ={})

        def app(win):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            harness.run(app)
        self.assertEqual(harness.termios.iflag, _FakeTermios.IXON)
        self.assertEqual(harness.events[-1], ("tcsetattr", 0, _FakeTermios.IXON))

    def test_ixon_already_off_is_left_alone_in_the_session(self) -> None:
        harness = _SessionHarness(self, iflag=0o1, environ={})
        harness.run(lambda win: None)
        in_session = [
            event for event in harness.events if event[0] == "tcsetattr"
        ]
        # Only the finally-restore writes the attributes back.
        self.assertEqual(in_session, [("tcsetattr", 0, 0o1)])
        self.assertNotIn(("def_prog_mode",), harness.events)

    def test_without_termios_the_session_still_runs(self) -> None:
        harness = _SessionHarness(self, iflag=0, environ={}, termios_module=False)
        self.assertEqual(harness.run(lambda win: 5), 5)


class TooSmallTextTests(unittest.TestCase):
    """The floor texts live in tui only."""

    def test_texts(self) -> None:
        self.assertEqual(
            tui.TOO_SMALL.format(what="the launch card"),
            "Terminal too small for the launch card.",
        )
        self.assertEqual(
            tui.TOO_SMALL_HINT.format(cols=48, rows=12),
            "Resize to at least 48x12, or press Esc to go back.",
        )

    def test_views_never_re_exports_them(self) -> None:
        from claude_multi import views

        self.assertFalse(hasattr(views, "TOO_SMALL"))
        self.assertFalse(hasattr(views, "TOO_SMALL_HINT"))

    def test_workflows_values_are_the_profile_enum(self) -> None:
        self.assertEqual(tui.WORKFLOWS_VALUES, ("native", "off"))


class GuaranteePanelTests(unittest.TestCase):
    """The exact guarantee panel lines."""

    CL02_NATIVE = [
        "Native workflows (ultracode): ON",
        "  · workflow agents run as the SESSION model by default (lead family;",
        "    Settings workflow default binding changes that default), always",
        "    acceptEdits — scripts may route stages to other models, so",
        "    unobserved workflow output is family-unknown/mixed and never counts",
        "    as an independent review verdict",
        "  · no cm-role contract; ≤16 concurrent / 1000 per run",
        "  · a cm-* agentType keeps its model and effort, but isolation is per",
        "    call: a workflow writer must pass isolation:'worktree' itself",
    ]

    def test_native_panel_states_the_workflow_guarantees(self) -> None:
        self.assertEqual(tui.workflow_guarantee_panel("native").splitlines(), self.CL02_NATIVE)

    def test_off_panel_appends_the_unchanged_off_note(self) -> None:
        lines = tui.workflow_guarantee_panel("off").splitlines()
        self.assertEqual(lines[0], "Native workflows (ultracode): OFF")
        self.assertEqual(lines[1:9], self.CL02_NATIVE[1:])
        self.assertEqual("\n".join(lines[9:]), tui.WORKFLOW_OFF_NOTE)

    def test_panel_lines_fit_a_modal_at_80_columns(self) -> None:
        for line in self.CL02_NATIVE:
            self.assertLessEqual(len(line), 74, line)


class ModalFocusTests(unittest.TestCase):
    """Enter in the input moves focus to the first button; a second
    Enter activates it (the Settings int-Modal script relies on this)."""

    def _modal(self):
        return tui.Modal(
            "auto-compact at",
            ["current 90 %, default 90 %, range 60–95"],
            buttons=(("Save", "save"), ("Cancel", None)),
            input=tui.TextInput(""),
        )

    def test_enter_in_the_input_focuses_save(self) -> None:
        modal = self._modal()
        self.assertEqual(modal.focus, 0)
        self.assertIsNone(modal.run(FakeWindow(list("85") + [ENTER, ESC]), tui.MONO_PALETTE))
        self.assertEqual(modal.focus, 1)
        self.assertEqual(modal.input.value, "85")

    def test_second_enter_activates_the_first_button(self) -> None:
        modal = self._modal()
        self.assertEqual(
            modal.run(FakeWindow(list("85") + [ENTER, ENTER]), tui.MONO_PALETTE), "save"
        )
        self.assertEqual(modal.input.value, "85")

    def test_the_input_starts_empty(self) -> None:
        modal = self._modal()
        self.assertEqual(modal.run(FakeWindow([ENTER, ENTER]), tui.MONO_PALETTE), "save")
        self.assertEqual(modal.input.value, "")


class CursesAbsentTests(unittest.TestCase):
    """cli imports with curses, _curses and termios blocked."""

    def test_cli_imports_without_curses_or_termios(self) -> None:
        import subprocess
        import sys

        from _layout import REPO_ROOT

        code = (
            "import sys\n"
            "sys.modules.update(curses=None, _curses=None, termios=None)\n"
            "import claude_multi.cli, claude_multi.tui, claude_multi.views\n"
            "import pkgutil, importlib\n"
            "for module in pkgutil.walk_packages(claude_multi.cli.__path__, 'claude_multi.cli.'):\n"
            "    importlib.import_module(module.name)\n"
            "from claude_multi import tui\n"
            "class Tty:\n"
            "    def isatty(self):\n"
            "        return True\n"
            "assert tui.curses_available() is False\n"
            "assert tui.curses is None and tui.termios is None\n"
            "assert tui.streams_curses_capable(Tty(), Tty(), {'TERM': 'xterm-256color'}) is False\n"
            "assert tui.query_background_osc11(0, 1) is None\n"
            "assert issubclass(tui.CursesError, Exception)\n"
            "print('OK')\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")

    def test_no_curses_import_outside_tui(self) -> None:
        import ast

        from _layout import REPO_ROOT

        SRC = REPO_ROOT / "src" / "claude_multi"
        for name in ["views.py", *[str(p.relative_to(SRC)) for p in (SRC / "cli").rglob("*.py")]]:
            tree = ast.parse((REPO_ROOT / "src" / "claude_multi" / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [(node.module or "").split(".")[0]] + [
                        alias.name for alias in node.names
                    ]
                else:
                    continue
                for module in modules:
                    self.assertNotIn(
                        module, ("curses", "_curses", "termios"), f"{name}:{node.lineno}"
                    )

    def test_tui_imports_curses_only_in_the_guarded_block(self) -> None:
        import ast

        from _layout import REPO_ROOT

        tree = ast.parse((REPO_ROOT / "src" / "claude_multi" / "tui.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.Import) and any(
                alias.name in ("curses", "termios") for alias in node.names
            ):
                self.fail(f"unguarded import at tui.py:{node.lineno}")
        guarded = [
            node
            for node in tree.body
            if isinstance(node, ast.Try)
            and any(
                isinstance(stmt, ast.Import)
                and stmt.names[0].name in ("curses", "termios")
                for stmt in node.body
            )
        ]
        self.assertEqual(len(guarded), 2)
