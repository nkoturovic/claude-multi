"""Terminal input journeys on a real pseudo-terminal.

A late answer to the background-colour query never closes a screen; an
unknown terminal type gets the line interface with one notice and no
query; Ctrl-C exits 130 with the terminal restored and nothing launched.
Direct without a model, upper- and lower-case keys, ``?`` typed into a
text field, scrolling, the pending-change previews, cancelled recovery
actions and W on Providers and Direct are driven key by key.
"""

from __future__ import annotations

import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_tui_pty import (
    END_KEY,
    FIXTURE_TIMEOUT,
    TESTS_ROOT,
    PTYProcess,
    _line_of,
    _read_after,
    _restored,
)

CARD_STATUS = b"Status  "
SESSIONS_READY = "sessions — project (cwd) · 1 total".encode()
OSC_QUERY = b"\x1b]11;?"
OSC_REPLY = b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\"
TAB = b"\t"
# A local terminal: no remote markers, no colour hint (so the card asks).
LOCAL = {"COLORFGBG": "", "SSH_CONNECTION": "", "SSH_CLIENT": "", "SSH_TTY": ""}
# The fixture runtime's own environment says TERM=dumb; these children name a
# capable terminal there too, so the card asks for the background colour.
CAPABLE = ", environ_extra={'TERM': 'xterm-256color'}"


def _child_code(temp: str, argv: list[str], *, runtime_kwargs: str = "", setup: str = "", after: str = "") -> str:
    return f"""
import sys
from pathlib import Path
import _tui_fixture as fx
from claude_multi import catalog, sessions
from claude_multi.cli import entry, session_facts
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = fx.fixture_runtime(Path({temp!r}), launch_callback=fake{runtime_kwargs})
{setup}
with fx.hermetic(runtime):
    code = entry.main({argv!r}, runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout,
                      interactive=True)
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


_RECORD = """
record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, follow={follow})
transcript = (Path(runtime.environ['HOME']) / '.claude' / 'projects'
              / session_facts._native_project_slug(record['cwd']) / (record['runtime_session_id'] + '.jsonl'))
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
{pending}
before = fx.canonical(runtime.session_store.load(fx.FIXED_ID))
"""
_PENDING = """
record = runtime.session_store.load(fx.FIXED_ID)
record['pending'] = {'requested_at': record['created_at'], 'kind': 'profile',
                     'profile': next(n for n in sorted(runtime.catalog.seed_profiles) if n != record['profile']),
                     'follow': True, 'document': None, 'reasons': ['native agents: explore replace → native']}
runtime.session_store.save(record)
"""
_UNCHANGED = """
after = runtime.session_store.load(fx.FIXED_ID) if runtime.session_store.exists(fx.FIXED_ID) else None
print('UNCHANGED=' + str(after is not None and fx.canonical(after) == before), flush=True)
"""


class _InputCase(unittest.TestCase):
    def child(self, prefix: str, argv=(), *, env=None, **kwargs) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, temp, True)
        child = PTYProcess(_child_code(temp, list(argv), **kwargs), extra_pythonpath=(TESTS_ROOT,),
                           extra_env={**LOCAL, **(env or {})})
        self.addCleanup(child.close)
        return child

    def press(self, child: PTYProcess, key: bytes, needle: bytes) -> None:
        offset = len(child.output)
        child.send(key)
        _read_after(child, offset, needle)

    def quit_card(self, child: PTYProcess) -> bytes:
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)
        return output


class LateColourAnswerTests(_InputCase):
    """The colour query times out quickly; an answer that arrives later is
    read as one sequence, never as Esc followed by text."""

    def _late(self, delay: float) -> None:
        child = self.child(f"claude-multi-pty-osc-{int(delay * 1000)}-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        time.sleep(delay)
        # Under load, the query can finish before curses enters no-echo
        # mode. Wait for the card as well as the minimum reply delay, so
        # this tests a late reply at the key reader, not kernel input echo.
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        child.send(OSC_REPLY)
        # The card is still open and keys still work.
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        output = self.quit_card(child)
        self.assertNotIn(b"rgb:1e1e", output.split(OSC_QUERY, 1)[1])

    def test_late_reply_waits_for_the_card_before_injection(self) -> None:
        child = mock.Mock()
        with mock.patch.object(self, "child", return_value=child), \
                mock.patch.object(self, "press"), mock.patch.object(self, "quit_card", return_value=OSC_QUERY), \
                mock.patch.object(time, "sleep") as sleep:
            self._late(0.2)
        sleep.assert_called_once_with(0.2)
        self.assertEqual(child.method_calls, [mock.call.read_until(OSC_QUERY, FIXTURE_TIMEOUT),
                                             mock.call.read_until(CARD_STATUS, FIXTURE_TIMEOUT),
                                             mock.call.send(OSC_REPLY)])

    def test_an_answer_after_200_ms(self) -> None:
        self._late(0.2)

    def test_an_answer_after_one_second(self) -> None:
        self._late(1.0)

    def test_a_terminator_that_arrives_after_the_card_is_drawn(self) -> None:
        # The reply starts inside the query's wait; its ESC backslash comes
        # only once the card is up, at the key reader.
        child = self.child("claude-multi-pty-osc-split-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(OSC_REPLY[:-2])
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        time.sleep(0.3)
        child.send(OSC_REPLY[-2:])
        # The card is still open and keys still work.
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_a_reply_in_three_fragments_with_a_pause_before_its_terminator(self) -> None:
        # The reply starts inside the query's wait, more of its body comes
        # once the card is up, then a pause, then its ESC backslash: the card
        # stays open and no byte of the reply arrives as a key.
        child = self.child("claude-multi-pty-osc-three-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(b"\x1b]11;rgb:0000/0000/")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        child.send(b"0000")
        time.sleep(0.3)
        child.send(b"\x1b\\")
        time.sleep(0.3)
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_esc_then_enter_after_a_cut_reply_cancels(self) -> None:
        # The reply starts inside the query's wait and its rest never comes;
        # once the card is up, a real Esc and then Enter right after it: the
        # Esc cancels the card, and nothing is launched.
        child = self.child("claude-multi-pty-osc-esc-enter-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(b"\x1b]11;rgb:0000/0000/")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        time.sleep(0.2)
        child.send(b"\x1b")
        child.send(b"\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class TerminalTypeTests(_InputCase):
    def test_an_unknown_terminal_type_gets_the_line_interface_and_one_notice(self) -> None:
        child = self.child("claude-multi-pty-term-", env={"TERM": "xterm-nonexistent-type"})
        child.read_until("Enter launch · q quit: ".encode(), FIXTURE_TIMEOUT)
        child.send(b"q\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count(b"TERM=xterm-nonexistent-type is not known here"), 1)
        self.assertIn(b"TERM=xterm-256color", output)
        self.assertNotIn(OSC_QUERY, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class InterruptTests(_InputCase):
    def test_ctrl_c_on_the_card_exits_130_and_restores_the_terminal(self) -> None:
        child = self.child("claude-multi-pty-ctrl-c-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        # The test terminal is not the child's controlling terminal, so the
        # interrupt Ctrl-C raises is delivered as the signal itself.
        child.process.send_signal(signal.SIGINT)
        code, output, after = child.finish()
        self.assertEqual(code, 130, output)
        self.assertIn(b"nothing was launched", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)


class CardKeyTests(_InputCase):
    def test_keys_work_in_both_cases(self) -> None:
        child = self.child("claude-multi-pty-case-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        for key in (b"s", b"S"):
            self.press(child, key, b"sessions \xe2\x80\x94")
            self.press(child, b"\x1b", CARD_STATUS)
        for key in (b"g", b"G"):
            self.press(child, key, "providers — local status".encode())
            self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_the_help_scrolls_to_its_end(self) -> None:
        child = self.child("claude-multi-pty-scroll-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, END_KEY, b"-- workflow guarantees")
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_a_question_mark_typed_in_a_text_field_is_text(self) -> None:
        child = self.child("claude-multi-pty-field-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"o", "settings — global".encode())
        # The kept environment row: End reaches the last row, then up past
        # the Gateway and Providers rows.
        child.send(END_KEY)
        for _ in range(3):
            child.send(b"k")
        self.press(child, b"\r", "kept environment".encode())
        child.send(b"MCP?")
        self.press(child, b"\r\r", b"0-9")
        self.assertIn(b"not saved, nothing changed", bytes(child.output))
        self.assertNotIn("settings — help".encode(), bytes(child.output))
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)


class DirectTests(_InputCase):
    _NO_LEADS = """
lcat = runtime.lineup_catalog()
for provider in sorted({e['provider'] for e in lcat.lines.values() if 'lead' in e['capabilities']}):
    runtime.settings_store.set_provider_enabled(provider, False, catalog=runtime.catalog)
"""

    def test_direct_without_a_model_names_the_fix_and_launches_nothing(self) -> None:
        child = self.child("claude-multi-pty-direct-none-", ["direct"], setup=self._NO_LEADS)
        child.read_until(b"no model can lead a direct session here", FIXTURE_TIMEOUT)
        child.send(b"\r")
        child.send(b"\t")
        offset = len(child.output)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output[offset:])
        self.assertIn(b"EXIT=", output)
        _restored(self, child, after)

    def test_w_is_offered_on_direct_and_providers_while_the_gateway_is_down(self) -> None:
        child = self.child("claude-multi-pty-w-", runtime_kwargs=", gateway_down=True")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"d", "direct session — lead only".encode())
        self.press(child, b"W", "Local gateway".encode())
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"g", "providers — local status".encode())
        self.press(child, b"w", "Local gateway".encode())
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", b"\x1b[")
        output = self.quit_card(child)
        # Both bars named W while the gateway was down.
        self.assertGreaterEqual(output.count(b"W\x1b[39m\x1b(B\x1b[m gateway"), 2)


class SessionPreviewTests(_InputCase):
    def sessions(self, prefix: str, *, follow: bool, pending: bool) -> PTYProcess:
        setup = _RECORD.format(follow=follow, pending=_PENDING if pending else "")
        child = self.child(prefix, ["-r"], setup=setup, after=_UNCHANGED)
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        return child

    def finish_unchanged(self, child: PTYProcess) -> None:
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"UNCHANGED="), "True")
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_follow_previews_the_dropped_change_and_esc_keeps_it(self) -> None:
        child = self.sessions("claude-multi-pty-follow-", follow=False, pending=True)
        self.press(child, b"f", b"follow \xe2\x80\x94 preview for")
        child.read_until(b"dropped", FIXTURE_TIMEOUT)
        self.press(child, b"\x1b", b"nothing changed")
        self.finish_unchanged(child)

    def test_keep_previews_discarding_the_pending_change_and_esc_writes_nothing(self) -> None:
        child = self.sessions("claude-multi-pty-keep-", follow=True, pending=True)
        self.press(child, b"T", b"change lineup")
        for _ in range(3):
            offset = len(child.output)
            child.send(TAB)
            _read_after(child, offset, b"\x1b[")
        child.read_until(b"discard the pending change", FIXTURE_TIMEOUT)
        self.press(child, b"\x1b", b"\x1b[")
        self.finish_unchanged(child)

    def test_recovery_actions_cancel_by_default(self) -> None:
        child = self.sessions("claude-multi-pty-recovery-", follow=True, pending=False)
        self.press(child, b"m", "ended?".encode())
        self.press(child, b"\r", b"nothing changed")
        self.press(child, b"x", b"Forget session")
        self.press(child, b"\r", b"Forget cancelled.")
        self.finish_unchanged(child)


if __name__ == "__main__":
    unittest.main()
