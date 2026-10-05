"""First-run and failure journeys through the real full-screen UI in a
pseudo-terminal: a bare ``claude-multi`` on a temporary home with nothing
connected, through Get started, a connection of each kind, the profile and
the launch card, to the fake launch.

Every child builds a fixture runtime (no provider has a key and every server
on the network is off), answers the Claude Code copy as verified and the
gateway reload from the runtime's seam, signs in with the fake gateway
login program, and launches through a fake launch callback: Claude Code is
never executed, nothing leaves the machine, and no host session marker
reaches the child. The keys a journey sends are derived from a parent-side
runtime of the same shape (the picker's order, the step list), never from
fixture ids written into the test.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from _layout import FIXTURES_ROOT, REPO_ROOT
from test_tui_pty import FINISH_TIMEOUT, PTYProcess, _read_after, _restored, _set_size

TESTS_ROOT = REPO_ROOT / "tests"
FAKE_LOGIN = FIXTURES_ROOT / "fake_gateway_login.py"
JOURNEY_TIMEOUT = 30.0
SESSION_MARKERS = ("CLAUDE_MULTI_MANAGED_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
# Keys as a terminal in keypad (application) mode sends them.
ESC = b"\x1b"
ENTER = b"\n"
DOWN = b"\x1bOB"
UP = b"\x1bOA"
HOME = b"\x1bOH"
END = b"\x1bOF"
RIGHT = b"\x1bOC"
CTRL_C = b"\x03"
CTRL_S = b"\x13"
BACKSPACE = b"\x7f"


# ------------------------------------------------------------------ the child side


def child_runtime(root: Path, launch: Any, options: dict[str, Any]) -> Any:
    """The journey runtime: a fixture runtime with no key, every keyless
    provider off, the account records directory present (so a sign-in is
    observed) and, for sign-ins, the fake gateway login program."""

    import _tui_fixture as fx
    from claude_multi import operator as operator_mod, state
    from test_screens_card_onboarding import disconnect

    environ = dict(options.get("env") or {})
    login = options.get("login")
    if login:
        environ["CLAUDE_MULTI_PROXY_BIN"] = login
    runtime = fx.fixture_runtime(root, launch_callback=launch, secrets=(), environ_extra=environ)
    with fx.hermetic(runtime):
        runtime.profiles.install_seeds()
        auth = Path(runtime.environ["HOME"]) / runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        state.ensure_private_dir(auth)
        disconnect(runtime)
    # A model admission's smoke request answers from the fixture.
    runtime.qualify_transport = lambda _base, _token, _alias: operator_mod.SmokeOutcome("pass", 200, "ok")
    if options.get("serve_render"):
        serve_render(runtime)
    if options.get("lan_reachable"):
        # A server on your network answers the short reachability probe.
        from claude_multi.cli import gateway_facts

        runtime.lan_probe = lambda url: gateway_facts.LanReachability("reachable", gateway_facts.lan_host(url),
                                                                      "fixture")
    return runtime


def serve_render(runtime: Any) -> None:
    """The fixture gateway serves the current render too (its sentinel and
    every alias), as the running gateway does after the reload that wrote it."""

    from claude_multi.cli import gateway_facts

    real = runtime.served_models_callback
    busy: list[bool] = []

    def served(gateway: Any, token: str) -> Any:
        selectors, status = real(gateway, token)
        if selectors is None or busy:
            return selectors, status
        busy.append(True)
        saved, cache = runtime.served_models_callback, runtime._report_cache
        try:
            runtime.served_models_callback, runtime._report_cache = real, None
            snap = gateway_facts._gateway_snapshot(runtime, token)
        finally:
            runtime.served_models_callback, runtime._report_cache = saved, cache
            busy.pop()
        current = set(snap.expected) | ({snap.sentinel} if snap.sentinel else set())
        return set(selectors) | current, status

    runtime.served_models_callback = served


@contextlib.contextmanager
def child_patches(runtime: Any, options: dict[str, Any]) -> Iterator[contextlib.ExitStack]:
    """Claude Code's copy is verified; the reload answers ``options["reload"]``
    (the stack is yielded so a journey's setup can add its own seams)."""

    import _tui_fixture as fx
    from claude_multi import proxy
    from claude_multi.setup import external

    reload = options.get("reload", "reloaded")
    with contextlib.ExitStack() as stack:
        stack.enter_context(fx.hermetic(runtime))
        if options.get("presets"):
            # The reviewed server presets ship with the product (the fixture
            # assets carry none).
            from _catalog import SHIPPED_ROOT
            from claude_multi import operator as operator_mod

            paths, load = operator_mod.preset_paths, operator_mod.load_preset
            stack.enter_context(mock.patch.object(operator_mod, "preset_paths",
                                                  lambda _root=None: paths(SHIPPED_ROOT)))
            stack.enter_context(mock.patch.object(operator_mod, "load_preset",
                                                  lambda name, _root=None: load(name, SHIPPED_ROOT)))
        for name in ("claude_status", "claude_verified"):
            stack.enter_context(mock.patch.object(external, name, return_value=external.ClaudeStatus(
                "2.1.0", "verified", "verified copy", None)))
        stack.enter_context(mock.patch.object(runtime, "verify_reload", side_effect=lambda sentinel: proxy.ReloadResult(
            reload, f"gateway: {reload}")))
        yield stack


def secret_name(runtime: Any, provider_id: str) -> str:
    """The key name a catalog provider reads (``env:NAME``)."""

    auth = runtime.catalog.docs["providers"]["providers"][provider_id]["transport"]["auth"]
    return str(auth["secret_ref"]).removeprefix("env:")


def put_key(runtime: Any, provider_id: str, value: str) -> None:
    """A key already saved before the journey (written to the key file)."""

    from claude_multi import state

    path = Path(runtime.environ["CLAUDE_MULTI_SECRET_ENV"])
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    lines.append(f"{secret_name(runtime, provider_id)}={value}")
    state.atomic_write(path, ("\n".join(lines) + "\n").encode("ascii"))


def put_key_name(runtime: Any, name: str, value: str) -> None:
    """A key under ``name`` in the key file (a provider you add reads it)."""

    from claude_multi import state

    path = Path(runtime.environ["CLAUDE_MULTI_SECRET_ENV"])
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    lines.append(f"{name}={value}")
    state.atomic_write(path, ("\n".join(lines) + "\n").encode("ascii"))


def say(name: str, value: Any) -> None:
    """A fact the parent reads before it sends keys (``NAME=value``)."""

    print(f"{name}={value}", flush=True)


def mirror_screen(log: Path) -> None:
    """Append every text the full-screen UI draws to ``log`` (one line per
    draw), so the parent can wait for a screen text whatever cursor moves
    curses chose to emit it with."""

    from claude_multi import tui

    real = tui.safe_add

    def drawn(win: Any, row: int, col: int, text: str, attr: int = 0) -> None:
        real(win, row, col, text, attr)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(text.replace("\n", " ") + "\n")

    tui.safe_add = drawn


CHILD = """
import io, json, sys
from pathlib import Path
from unittest import mock
import test_journeys_onboarding as journeys
from claude_multi import choices
from claude_multi.cli import entry
journeys.mirror_screen(Path({screen!r}))

def fake(prepared):
    print('FAKE_LAUNCH=' + str(prepared.target.profile), flush=True)
    return 0

options = json.loads({options!r})
runtime = journeys.child_runtime(Path({temp!r}), fake, options)
with journeys.child_patches(runtime, options) as stack:
{setup}
    code = entry.main({argv!r}, runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True)
print('EXIT=' + str(code), flush=True)
print('DEFAULT=' + str(choices.read(runtime.environ).get('default_profile')), flush=True)
{after}
raise SystemExit(code)
"""


# ------------------------------------------------------------------ the parent side


class JourneyPTY(PTYProcess):
    """A PTY child whose environment carries no Claude Code session marker;
    ``expect`` waits for a text the UI drew (the screen mirror), ``expect_out``
    for raw terminal output (a suspended screen, the final prints)."""

    def __init__(self, code: str, screen: Path, *, rows: int = 24, columns: int = 80,
                 extra_env: dict[str, str] | None = None):
        # Like PTYProcess, plus the terminal is the child's controlling
        # terminal, so Ctrl-C reaches a suspended screen's program as SIGINT.
        self.master, self.slave = pty.openpty()
        _set_size(self.slave, rows, columns)
        self.before = termios.tcgetattr(self.slave)
        env = {name: value for name, value in os.environ.items() if name not in SESSION_MARKERS}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "TERM": "xterm-256color",
                    "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(TESTS_ROOT)])})
        env.update(extra_env or {})
        slave = self.slave

        def attach() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                                        preexec_fn=attach, close_fds=True, env=env)
        self.output = bytearray()
        self.screen = screen
        self.screen_mark = 0
        self.mark = 0

    def drain(self, wait: float) -> None:
        ready, _, _ = select.select([self.master], [], [], wait)
        if not ready:
            return
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return
        self.output.extend(chunk)

    def drawn(self) -> str:
        try:
            return self.screen.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def expect(self, needle: str, timeout: float = JOURNEY_TIMEOUT) -> None:
        """Wait until the UI draws ``needle`` after the last screen expectation."""

        deadline = time.monotonic() + timeout
        while True:
            self.drain(0.05)
            text = self.drawn()
            found = text.find(needle, self.screen_mark)
            if found >= 0:
                self.screen_mark = found + len(needle)
                return
            if time.monotonic() > deadline or self.process.poll() is not None:
                debug = os.environ.get("CM_JOURNEY_DEBUG")
                if debug:
                    Path(debug).write_text(text + "\n=== OUTPUT ===\n" + bytes(self.output).decode("utf-8", "replace"))
                raise AssertionError(f"the screen never showed {needle!r}; last drawn:\n{text[-3000:]}\n"
                                     f"output tail: {bytes(self.output)[-1500:]!r}")

    def expect_out(self, needle: str, timeout: float = JOURNEY_TIMEOUT) -> None:
        """Wait for ``needle`` in the raw terminal output after the last one."""

        data = needle.encode()
        _read_after(self, self.mark, data, timeout)
        self.mark = bytes(self.output).index(data, self.mark) + len(data)
        self.screen_mark = len(self.drawn())

    def keys(self, *items: bytes | str, pause: float = 0.1) -> None:
        for item in items:
            self.send(item.encode() if isinstance(item, str) else item)
            deadline = time.monotonic() + pause
            while time.monotonic() < deadline:
                self.drain(0.02)


class JourneyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-journeys-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- the parent-side runtime of the same shape -----------------------------

    @contextlib.contextmanager
    def shape(self, options: dict[str, Any] | None = None) -> Iterator[Any]:
        root = Path(tempfile.mkdtemp(prefix="cm-journey-shape-", dir=self.tmp))
        runtime = child_runtime(root, lambda _prepared: 0, dict(options or {}))
        with child_patches(runtime, dict(options or {})):
            yield runtime

    def picker_index(self, entry_id: str, options: dict[str, Any] | None = None) -> int:
        from claude_multi.setup import providers as layer

        with self.shape(options) as runtime:
            ids = [entry.id for entry in layer.picker_entries(runtime)]
        return ids.index(entry_id)

    def step_index(self, step_id: str) -> int:
        from claude_multi.setup import status

        return list(status.STEP_ORDER).index(step_id)

    def login_launcher(self, **env: str) -> str:
        target = self.tmp / "fake-gateway-login"
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        target.write_text(f"#!/bin/sh\n{exports} exec {shlex.quote(sys.executable)} "
                          f"{shlex.quote(str(FAKE_LOGIN))} \"$@\"\n")
        target.chmod(0o755)
        return str(target)

    # -- the child ---------------------------------------------------------------

    def child(self, *, argv: list[str] | None = None, options: dict[str, Any] | None = None,
              setup: str = "", after: str = "", **kwargs: Any) -> JourneyPTY:
        root = self.tmp / "child"
        screen = self.tmp / "screen.log"
        parts = setup if isinstance(setup, (tuple, list)) else (setup,)
        joined = "\n".join(textwrap.dedent(part).strip("\n") for part in parts)
        body = textwrap.indent(joined, "    ") or "    pass"
        code = CHILD.format(options=json.dumps(options or {}), temp=str(root), argv=list(argv or []),
                            setup=body, after=textwrap.dedent(after).strip("\n"), screen=str(screen))
        child = JourneyPTY(code, screen, **kwargs)
        self.addCleanup(child.close)
        return child

    def finish(self, child: JourneyPTY) -> str:
        try:
            code, output, after = child.finish(FINISH_TIMEOUT)
        except AssertionError:
            debug = os.environ.get("CM_JOURNEY_DEBUG")
            if debug:
                Path(debug).write_text(child.drawn())
            raise
        text = output.decode("utf-8", "replace")
        self.assertEqual(code, 0, text[-4000:])
        _restored(self, child, after)
        return text

    def fact(self, child: JourneyPTY, name: str) -> str:
        """A ``NAME=value`` line the child's setup printed."""

        child.expect_out(f"{name}=")
        rest = bytes(child.output)[child.mark:]
        while b"\n" not in rest:
            child.drain(0.05)
            rest = bytes(child.output)[child.mark:]
        return rest.split(b"\n", 1)[0].decode().strip()

    def to_picker_entry(self, child: JourneyPTY, entry_id: str, options: dict[str, Any] | None = None) -> None:
        """From the step list: A, then down to ``entry_id`` and Enter."""

        index = self.picker_index(entry_id, options)
        child.keys("a")
        child.expect("connect a provider — more can follow")
        child.keys(HOME, *([DOWN] * index), ENTER)

    def open_step(self, child: JourneyPTY, step_id: str) -> None:
        child.keys(HOME, *([DOWN] * self.step_index(step_id)), ENTER)


# ================================================================ first run, per credential kind


class FirstRunJourneys(JourneyCase):
    def test_j1_openrouter_key(self) -> None:
        from claude_multi.setup import texts

        child = self.child()
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "openrouter:api-key")
        child.expect("OpenRouter API key")
        child.keys("sk-or-journey-key", ENTER, ENTER)
        child.expect("OpenRouter API key saved (")
        child.expect(texts.RELOAD_TEXT["reloaded"])
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        self.open_step(child, "profile")
        child.expect("openrouter (chosen automatically")
        child.expect("bill per token to OpenRouter")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("profile: openrouter")
        child.expect("Status  Ready")
        child.keys(ENTER)
        child.expect_out("FAKE_LAUNCH=openrouter")
        self.finish(child)

    def starter_provider(self) -> str:
        """A provider with an API key, models and no shipped profile of its own."""

        from claude_multi import catalog
        from claude_multi.setup import providers as layer, texts

        with self.shape() as runtime:
            entries = [entry for entry in layer.picker_entries(runtime)
                       if entry.kind == "api-key" and entry.available and entry.note != texts.NO_MODELS_NOTE
                       and entry.provider_id not in catalog.AGGREGATOR_PROVIDERS
                       and entry.id != "anthropic:api-key"]
        self.assertTrue(entries)
        return entries[0].provider_id

    def test_j2_a_key_no_shipped_profile_fits_builds_the_starter(self) -> None:
        from claude_multi.setup import texts

        provider = self.starter_provider()
        child = self.child()
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, f"{provider}:api-key")
        child.expect("API key")
        child.keys("journey-key-123456", ENTER, ENTER)
        child.expect(texts.RELOAD_TEXT["reloaded"])
        child.keys(ESC)
        child.expect("no profile fits yet — Enter builds one")
        self.open_step(child, "profile")
        child.expect("Nothing shipped fits")
        child.expect("starter preview")
        child.keys(ENTER)
        child.expect("Saved starter and made it your default profile.")
        child.keys(ESC)
        child.expect("profile: starter (default)")
        child.keys(ESC)
        output = self.finish(child)
        self.assertIn("Nothing was launched.", output)
        self.assertIn("DEFAULT=starter", output)

    def test_j3_anthropic_api_key(self) -> None:
        from claude_multi.cli import text as cli_text

        child = self.child()
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "anthropic:api-key")
        child.expect("Use an Anthropic API key for Claude models?")
        child.keys(RIGHT, ENTER)  # Switch
        child.expect("Anthropic API key")
        child.keys("sk-ant-journey-123456", ENTER, ENTER)
        child.expect("the gateway reloaded")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("profile: claude")
        child.expect("Status  Ready")
        child.keys(ENTER)
        child.expect_out("FAKE_LAUNCH=claude")
        self.finish(child)

    def test_j4_claude_account_sign_in(self) -> None:
        from claude_multi.cli import text as cli_text
        from claude_multi.setup import texts

        argv_log = self.tmp / "login-argv.jsonl"
        child = self.child(options={"login": self.login_launcher(FAKE_LOGIN_ARGV=str(argv_log))})
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "anthropic:account")
        child.expect(cli_text.ACK_TITLE.format(kind="Claude account"))
        child.keys("nope", ENTER, ENTER)
        child.expect(texts.ACK_WRONG[:40])
        child.keys(ENTER)  # the same entry again
        child.expect(cli_text.ACK_TITLE.format(kind="Claude account"))
        child.keys("personal", ENTER, ENTER)
        child.expect(cli_text.SIGNIN_METHOD_TITLE.format(kind="Claude account"))
        child.keys(END, ENTER)  # an address to open on any device
        child.expect_out("Paste the Claude callback URL")
        child.keys("http://localhost:54545/callback?code=journey&state=x\n")
        child.expect_out(cli_text.SIGNIN_RETURN)
        child.keys(ENTER)
        child.expect("(Claude account). Its models are")
        child.keys(ENTER)  # Close
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("profile: claude")
        child.keys(ENTER)
        child.expect_out("FAKE_LAUNCH=claude")
        self.finish(child)
        argv = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertTrue(any("--claude-login" in run and "--no-browser" in run for run in argv), argv)

    def test_j5_chatgpt_account_device_sign_in(self) -> None:
        from claude_multi.cli import text as cli_text

        child = self.child(options={"login": self.login_launcher()})
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "openai:account")
        child.expect(cli_text.ACK_TITLE.format(kind="ChatGPT account"))
        child.keys("personal", ENTER, ENTER)
        child.expect_out("enter the code FAKE-CODE")
        child.expect_out(cli_text.SIGNIN_RETURN)
        child.keys(ENTER)
        child.expect("(ChatGPT account). Its models are")
        child.keys(ENTER)
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("profile: openai")
        child.keys(ENTER)
        child.expect_out("FAKE_LAUNCH=openai")
        self.finish(child)


    # -- providers you add ----------------------------------------------------

    def declare_by_hand(self, child: JourneyPTY, wire: str, key: str) -> None:
        """"Enter a model by hand", then the declaration form: lead only, the
        prefilled efforts and family kept."""

        child.keys(HOME, DOWN, ENTER)
        child.expect("declare model")
        child.keys(f"{wire}\n{key}\n200000\n\nfixture docs, date\n")
        child.keys(ENTER, ENTER, ENTER, ENTER, ENTER, ENTER)  # family, efforts, default, output, lead only, roles
        child.expect("Declaration preview")
        child.keys(ENTER)
        child.expect(f"declared {key}")
        child.keys(ESC)

    def admit_on_models(self, child: JourneyPTY) -> None:
        """M on the card, Enter on the declared model, the consent and the
        smoke (answered by the fixture)."""

        child.keys("m")
        child.expect("Enter admit")
        child.keys(ENTER)
        child.expect("Confirm explicit action")
        child.keys("y")
        child.expect("admission smoke")
        child.keys("y")
        child.expect("Usable as a Direct lead")
        child.keys(ESC)
        child.expect("op · admitted")
        child.keys(ESC)
        child.expect("Status  ")

    def starter_from_step_six(self, child: JourneyPTY) -> str:
        child.keys("w")
        child.expect("claude-multi — Get started")
        self.open_step(child, "profile")
        child.expect("Nothing shipped fits")
        child.expect("starter preview")
        child.keys(ENTER)
        child.expect("Saved starter and made it your default profile.")
        child.keys(ESC)
        child.expect("profile: starter (default)")
        child.keys(ESC)
        return self.finish(child)

    def test_j6_your_anthropic_compatible_endpoint(self) -> None:
        from claude_multi.cli import text as cli_text

        key = "custom-vendorx-chat"
        child = self.child(setup=MODELS_CURSOR.format(key=key), options={"serve_render": True})
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "other:anthropic-compatible")
        child.expect(cli_text.ENDPOINT_FORM_TITLE["anthropic-compatible"])
        child.keys("vendorx", ENTER, "https://api.vendorx.example/anthropic", ENTER, ENTER)
        child.keys(*([BACKSPACE] * len("unknown")), "vendorx", ENTER, ENTER)
        child.expect(cli_text.ENDPOINT_PREVIEW_TITLE)
        child.keys(ENTER)  # declare
        child.expect(cli_text.APPROVE_TITLE.format(id="vendorx"))
        child.keys(RIGHT, ENTER)  # Approve
        child.expect(cli_text.KEY_MODAL_TITLE.format(display="vendorx"))
        child.keys("vendorx-journey-key", ENTER, ENTER)
        child.expect(cli_text.ADD_MODELS_TITLE.format(id="vendorx"))
        self.declare_by_hand(child, "vendorx-chat-1", key)
        child.expect(cli_text.ADMIT_NOW_TITLE.format(key=key))
        child.keys(RIGHT, ENTER)  # Later: Models admits it below
        child.expect("Added vendorx")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("Status  ")
        self.admit_on_models(child)
        output = self.starter_from_step_six(child)
        self.assertIn("DEFAULT=starter", output)

    def test_j7_a_server_on_your_network(self) -> None:
        from claude_multi.cli import text as cli_text

        key = "custom-homelab-chat"
        options = {"presets": True, "serve_render": True, "lan_reachable": True}
        child = self.child(setup=MODELS_CURSOR.format(key=key), options=options)
        child.expect("claude-multi — Get started")
        preset = next(entry for entry in self.entry_ids(options) if entry.startswith("other:lan:"))
        self.to_picker_entry(child, preset, options)
        child.expect(cli_text.LAN_FORM_TITLE)
        child.keys(*([BACKSPACE] * len("lan")), "homelab", ENTER, "http://192.168.1.20:8000/v1", ENTER)
        child.expect(cli_text.LAN_PREVIEW_TITLE)
        child.keys(ENTER)  # declare
        child.expect(cli_text.ADD_MODELS_TITLE.format(id="homelab"))
        self.declare_by_hand(child, "homelab-chat-1", key)
        child.expect(cli_text.ADMIT_NOW_TITLE.format(key=key))
        child.keys(RIGHT, ENTER)  # Later: Models admits it below
        child.expect("Added homelab")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("Status  ")
        self.admit_on_models(child)
        output = self.starter_from_step_six(child)
        self.assertIn("DEFAULT=starter", output)

    def entry_ids(self, options: dict[str, Any]) -> list[str]:
        from claude_multi.setup import providers as layer

        with self.shape(options) as runtime:
            return [entry.id for entry in layer.picker_entries(runtime)]


# The Models screen opens on the model the journey declared (its row depends
# on the fixture catalog's other models).
MODELS_CURSOR = """
import claude_multi.cli.screens.models as models_screen
hinted = models_screen._ModelsScreen._hinted_key
stack.enter_context(mock.patch.object(models_screen._ModelsScreen, "_hinted_key", lambda self: (
    {key!r} if {key!r} in self.keys else hinted(self))))
"""


# ================================================================ failure journeys


MINE = """
from claude_multi import catalog
document = runtime.profiles.load(catalog.DEFAULT_SEED)
document.pop("seed", None)
document["name"] = "mine"
runtime.profiles.save(document)
"""


class FailureJourneys(JourneyCase):
    def test_f2a_a_refused_render_keeps_the_previous_key(self) -> None:
        from claude_multi.setup import texts

        setup = """
            from claude_multi import errors
            import hashlib
            import claude_multi.cli.screens.providers as providers
            from claude_multi import tui
            journeys.put_key(runtime, "openrouter", "old-journey-key")
            key_file = Path(runtime.environ["CLAUDE_MULTI_SECRET_ENV"])
            journeys.say("BEFORE", hashlib.sha256(key_file.read_bytes()).hexdigest())
            rows = [row.id for row in providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE).rows]
            journeys.say("ROW", rows.index("openrouter"))
            stack.enter_context(mock.patch.object(runtime, "render_gateway",
                                                  side_effect=errors.ClaudeMultiError("fixture render refusal")))
        """
        after = """
        import hashlib
        print("AFTER=" + hashlib.sha256(Path(runtime.environ["CLAUDE_MULTI_SECRET_ENV"]).read_bytes()).hexdigest())
        """
        child = self.child(setup=setup, after=after)
        before = self.fact(child, "BEFORE")
        row = int(self.fact(child, "ROW"))
        child.expect("Status  ")
        child.keys("g")
        child.expect("providers")
        child.keys(HOME, *([DOWN] * row), "k")
        child.expect("OpenRouter API key")
        child.expect("Replace replaces it; Cancel keeps it.")
        child.keys("new-journey-key-1", ENTER, RIGHT, ENTER)  # Replace
        child.expect(texts.RENDER_REFUSED.split("{")[0])
        child.keys(ESC, ESC)
        output = self.finish(child)
        self.assertIn(f"AFTER={before}", output)

    def test_f2b_a_reload_that_needs_a_restart_says_so(self) -> None:
        setup = """
            import claude_multi.cli.screens.providers as providers
            from claude_multi import tui
            journeys.put_key(runtime, "openrouter", "old-journey-key")
            rows = [row.id for row in providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE).rows]
            journeys.say("ROW", rows.index("openrouter"))
        """
        child = self.child(setup=setup, options={"reload": "restart_required"})
        row = int(self.fact(child, "ROW"))
        child.expect("Status  ")
        child.keys("g")
        child.expect("providers")
        child.keys(HOME, *([DOWN] * row), "K")
        child.expect("Replace replaces it; Cancel keeps it.")
        child.keys("new-journey-key-1", ENTER, RIGHT, ENTER)
        child.expect("the gateway needs a restart to use it")
        child.keys(ESC, ESC)
        self.finish(child)

    def test_f1_a_changed_route_is_approved_again(self) -> None:
        from claude_multi.cli import text as cli_text

        setup = """
            import json
            from claude_multi import operator as operator_mod, state, tui
            import claude_multi.cli.screens.providers as providers
            import test_cli
            journeys.put_key(runtime, "openrouter", "journey-key")
            journeys.put_key_name(runtime, "ACME_API_KEY", "acme-journey-key")
            for argv, text in ((test_cli.ACME_ADD, "y\\n"), (test_cli.SMALL_ADD, "")):
                entry.main(argv, runtime=runtime, input_stream=io.StringIO(text), output_stream=io.StringIO(),
                           interactive=True)
            path = operator_mod.providers_dir({"HOME": str(runtime.home)}) / "acme.json"
            document = json.loads(path.read_bytes())
            document["provider"]["base_url"] = "https://changed.example.test/anthropic"
            state.atomic_write(path, json.dumps(document).encode())
            rows = [row.id for row in providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE).rows]
            journeys.say("ROW", rows.index("acme"))
        """
        child = self.child(setup=setup)
        row = int(self.fact(child, "ROW"))
        child.expect("Status  ")
        child.keys("g")
        child.expect("providers")
        child.keys(HOME, *([DOWN] * row), ENTER)
        child.expect(cli_text.APPROVE_TITLE_CHANGED.format(id="acme"))
        child.keys(RIGHT, ENTER)  # Approve
        child.expect("approved")
        child.keys(ESC, ESC)
        self.finish(child)

    def test_f3_a_corrupt_profile_opens_a_blocked_card(self) -> None:
        from claude_multi.cli import text as cli_text

        setup = MINE, """
            import _tui_fixture as fx
            from claude_multi import state
            fx.v4_record(runtime, "mine", managed_id=fx.FIXED_ID)
            state.atomic_write(runtime.profiles.root / "mine.json", b'{\\n  "name": "mine",\\n  oops\\n}\\n')
        """
        after = """
        print("BACKUPS=" + str(len(list(runtime.profiles.root.glob(".mine.removed-*.json")))))
        """
        child = self.child(setup=setup, after=after)
        child.expect("Status  BLOCKED")
        child.expect("profile mine cannot be loaded:")
        child.expect("line 3:")
        child.keys("p")
        child.expect(cli_text.PROFILES_TITLE)
        child.expect("cannot load")
        child.keys("x")
        child.expect(cli_text.DELETE_TITLE.format(name="mine"))
        child.expect("1 session(s) follow mine")
        child.keys(RIGHT, ENTER)
        child.expect("propagation — mine")  # the following session keeps its lineup
        child.expect("is now pinned")
        child.keys(ESC)
        child.expect("Deleted mine; a copy is kept as")
        child.keys(ESC)
        child.expect("— profile: ")
        child.keys(ESC)
        output = self.finish(child)
        self.assertIn("BACKUPS=1", output)

    def test_f4_an_interrupted_sign_in_changes_nothing(self) -> None:
        from claude_multi.cli import text as cli_text
        from claude_multi.setup import texts

        after = """
        auth = Path(runtime.environ["HOME"]) / runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        print("RECORDS=" + str(len(list(auth.iterdir()))))
        """
        child = self.child(options={"login": self.login_launcher()}, after=after)
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "anthropic:account")
        child.expect(cli_text.ACK_TITLE.format(kind="Claude account"))
        child.keys("personal", ENTER, ENTER)
        child.expect(cli_text.SIGNIN_METHOD_TITLE.format(kind="Claude account"))
        child.keys(END, ENTER)
        child.expect_out("Paste the Claude callback URL")
        child.keys(CTRL_C)
        child.expect_out(cli_text.SIGNIN_RETURN)
        child.keys(ENTER)
        child.expect(texts.SIGNIN_CANCELLED[:30])
        child.keys(ESC)
        child.expect("connect a provider — more can follow")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        # Still nothing connected: the card can launch but asks for a look.
        child.expect("Status  Attention")
        child.keys(ESC)
        output = self.finish(child)
        self.assertIn("RECORDS=0", output)

    def test_f5_a_concurrent_edit_offers_reload(self) -> None:
        from claude_multi import tui

        setup = MINE, """
            journeys.put_key(runtime, "openrouter", "journey-key")
            from claude_multi.setup import profiles as setup_profiles
            journeys.say("ROW", [row.name for row in setup_profiles.rows(runtime)].index("mine"))
            journeys.say("CONFIG", runtime.environ["XDG_CONFIG_HOME"])
        """
        child = self.child(setup=setup)
        row = int(self.fact(child, "ROW"))
        config = self.fact(child, "CONFIG")
        child.expect("Status  ")
        child.keys("p")
        child.expect("profiles")
        child.keys(HOME, *([DOWN] * row), "e")
        child.expect("edit profile — mine")
        child.keys(ENTER)  # General
        child.expect(tui.PROFILE_FORM_GENERAL_TITLE)
        child.keys(DOWN, "x", CTRL_S)  # an edit to the description
        child.expect("unsaved changes")
        self.write_elsewhere(config, "mine", "written elsewhere")
        child.keys(CTRL_S)
        child.expect(tui.PROFILE_FORM_CHANGED_TITLE.format(name="mine"))
        child.keys(RIGHT, ENTER)  # Reload
        child.expect('description "written elsewhere"')
        child.expect(tui.PROFILE_FORM_RELOADED.format(name="mine"))
        child.keys(ESC, ESC, ESC)
        self.finish(child)

    def write_elsewhere(self, config: str, name: str, description: str) -> None:
        from claude_multi import profile

        store = profile.ProfileStore(seeds={}, environ={"XDG_CONFIG_HOME": config, "HOME": str(self.tmp)})
        store.update(name, lambda doc: doc.update(description=description))

    def test_f6_inside_a_claude_session_nothing_opens(self) -> None:
        from claude_multi.cli import text as cli_text

        child = self.child(options={"env": {"CLAUDECODE": "1"}})
        child.expect("— profile: ")
        child.expect("nothing is connected yet")
        child.keys("w")
        child.expect(cli_text.GS_IN_SESSION[:50])
        child.keys(ESC)
        output = self.finish(child)
        self.assertNotIn(cli_text.GS_HEADER, self.drawn_before(child, cli_text.GS_IN_SESSION[:50]))
        self.assertIn("Nothing was launched.", output)

    def drawn_before(self, child: JourneyPTY, needle: str) -> str:
        text = child.drawn()
        return text[: text.find(needle)]

    def test_f7_over_ssh_the_address_method_is_preselected(self) -> None:
        from claude_multi.cli import text as cli_text

        argv_log = self.tmp / "login-argv.jsonl"
        child = self.child(options={"login": self.login_launcher(FAKE_LOGIN_ARGV=str(argv_log)),
                                    "env": {"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.2 22"}})
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, "anthropic:account")
        child.expect(cli_text.ACK_TITLE.format(kind="Claude account"))
        child.keys("personal", ENTER, ENTER)
        child.expect(cli_text.SIGNIN_METHOD_TITLE.format(kind="Claude account"))
        child.keys(ENTER)  # the preselected method
        child.expect_out("Paste the Claude callback URL")
        child.keys("http://localhost:54545/callback?code=journey&state=x\n")
        child.expect_out(cli_text.SIGNIN_RETURN)
        child.keys(ENTER)
        child.expect("Signed in:")
        child.keys(ENTER, ESC, ESC, ESC)
        self.finish(child)
        argv = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertEqual(len(argv), 1)
        self.assertIn("--no-browser", argv[0])

    def test_f8_line_mode_offers_setup_and_n_continues(self) -> None:
        from claude_multi.setup import texts

        child = self.child(argv=["--line"])
        child.expect_out(texts.LINE_NOT_CONNECTED)
        child.expect_out(texts.LINE_SETUP_NOW)
        child.keys("n\n")
        child.expect_out("Enter launch · q quit: ")
        child.keys("q\n")
        output = self.finish(child)
        self.assertIn("Nothing was launched.", output)
        self.assertNotIn(texts.SETUP_HEADER.splitlines()[0] + "\n", output.replace("\r", ""))


if __name__ == "__main__":
    unittest.main()
