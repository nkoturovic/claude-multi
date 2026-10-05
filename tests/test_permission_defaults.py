"""The managed permission default mode.

Claude Code 2.1.284+ starts a session whose settings configure no permission
mode in ``auto`` (measured: 2.1.281 ``default`` vs 2.1.286 ``auto``). A
managed launch therefore compiles ``permissions.defaultMode: "default"`` into
its flag settings, but ONLY when no settings layer the client reads
configures a mode (``managed.permission_mode_configured``): flag settings
outrank user, project and local settings, so an unconditional value would
silently override an operator's configured mode. The user layer is
``$HOME/.claude/settings.json``: the launcher unsets ``CLAUDE_CONFIG_DIR``.

The real-binary proof (offline, fake provider, trusted bwrap, private
fixture HOME; the observed mode is ``UserPromptSubmit.permission_mode``,
never argv) runs on the pinned client and, when retained, on the previous
pin ``PREVIOUS_CLIENT`` (the comparison baseline):

- (a) nothing configured -> compiled ``default`` -> ``default``, for the
  profile, ad-hoc direct and no-subagent scopes, fresh and resume; the
  omitted control (nothing compiled) shows why;
- (b) user ``bypassPermissions`` (+ ``skipDangerousModePermissionPrompt``)
  -> nothing compiled -> ``bypassPermissions``;
- (c) a project or local layer -> nothing compiled -> that mode;
- (d) negative control: user ``bypassPermissions`` plus a forced compiled
  ``default`` -> ``default`` (the override the decision avoids);
- (e) an explicit ``--permission-mode`` for every mode the client accepts,
  and ``--dangerously-skip-permissions``, wins over the compiled value and
  the configured layers;
- (f) ``CLAUDE_CONFIG_DIR`` set in the launching environment with another
  mode than ``$HOME/.claude``: the decision follows ``$HOME/.claude``;
- (g) a user layer with ``bypassPermissions`` and an unrelated duplicate key
  (the client's parse keeps the last value) -> nothing compiled ->
  ``bypassPermissions``.

Real-binary fixtures use ``claude_config_dir = <fixture HOME>/.claude``
(``run_native`` hands the child ``CLAUDE_CONFIG_DIR=<claude_config_dir>``),
so the configured user layer is the very file the client reads.
Evidence is metadata only: hook events keep the event name, session id and
permission mode; request rows keep model, max_tokens, stream, tool count and
a system-prompt digest class.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import test_compiler_v2  # module import: no test classes re-exported
from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source
from claude_multi import compiler, managed, probe, profile, scope, state, strict_json

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LIVE_DOMAIN_TOUCHED = "live daemon domain touched"
# The comparison baseline: the pin 2.1.286 replaced (both were measured).
# Located next to the pinned binary and hash-verified before every exec; an
# absent baseline drops only the comparison runs (never the pinned proof).
PREVIOUS_CLIENT = ("2.1.281", "56fe3da88458465fb27d7e9299dddb3fead55750fb9c2de795f233b5eea6dce1")
LEAD_MODEL = "pd-lead"
TURN = "PD-TURN"
# Every --permission-mode value the pinned client accepts (S14's matrix).
ACCEPTED_MODES = ("default", "manual", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan")

_HOOK_BODY = """\
import json, sys
event = json.load(sys.stdin)
kept = {key: event.get(key) for key in ("hook_event_name", "session_id", "permission_mode", "source")}
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(kept, sort_keys=True) + "\\n")
print("{}")
"""


# ------------------------------------------------------------------ pure


class PermissionLayerTests(unittest.TestCase):
    """The decision seam itself (no binary)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-pd-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.cwd = self.home / "project"
        self.policy = self.root / "policy"
        self.cwd.mkdir(parents=True)

    def write(self, path: Path, document: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def configured(self, **environ: str) -> bool:
        return managed.permission_mode_configured({"HOME": str(self.home), **environ}, self.cwd, self.policy)

    def test_user_layer_is_home_never_claude_config_dir(self) -> None:
        other = self.root / "config-dir"
        self.write(other / "settings.json", {"permissions": {"defaultMode": "plan"}})
        self.assertFalse(self.configured(CLAUDE_CONFIG_DIR=str(other)))
        self.write(self.home / ".claude/settings.json", {"permissions": {"defaultMode": "acceptEdits"}})
        self.assertTrue(self.configured(CLAUDE_CONFIG_DIR=str(other)))
        layers = managed.permission_mode_layers({"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(other)},
                                                self.cwd, self.policy)
        self.assertEqual(layers[0], self.home / ".claude/settings.json")
        self.assertNotIn(other / "settings.json", layers)
        # The managed client never sees CLAUDE_CONFIG_DIR (the decision's premise).
        self.assertIn("CLAUDE_CONFIG_DIR", compiler.V2_ENV_UNSET)

    def test_every_layer_by_presence_only(self) -> None:
        paths = (self.home / ".claude/settings.json", self.cwd / ".claude/settings.json",
                 self.cwd / ".claude/settings.local.json", self.policy / "managed-settings.json",
                 self.policy / "managed-settings.d/10-fixture.json")
        for path in paths:
            for value in ("default", "bypassPermissions", None, 7):
                with self.subTest(layer=str(path.relative_to(self.root)), value=value):
                    self.write(path, {"permissions": {"defaultMode": value}})
                    self.assertTrue(self.configured())
                    path.unlink()
                    self.assertFalse(self.configured())
        for document in ({"permissions": {"allow": ["Bash"]}}, {"defaultMode": "plan"}, {"permissions": "plan"}):
            with self.subTest(document=document):
                self.write(paths[0], document)
                self.assertFalse(self.configured())

    def test_unreadable_or_malformed_layers_contribute_nothing(self) -> None:
        path = self.home / ".claude/settings.json"
        path.parent.mkdir(parents=True)
        for raw in (b"{", b"[]", b"", b'{"permissions": {"defaultMode": NaN}}', b"\xff{}",
                    b'{"permissions": {"defaultMode": "a"}, "pad": "' + b"x" * (1 << 20) + b'"}'):
            with self.subTest(raw=raw[:60]):
                path.write_bytes(raw)
                self.assertFalse(self.configured())
        path.unlink()
        path.mkdir()
        self.assertFalse(self.configured())

    def test_duplicate_keys_parse_like_the_client(self) -> None:
        """The client's JSON parse keeps a duplicate key's last
        value and still applies the layer, so the presence decision does too
        (an unrelated duplicate never makes a configured layer count as
        unconfigured); strict reads elsewhere are unchanged."""

        path = self.home / ".claude/settings.json"
        path.parent.mkdir(parents=True)
        for raw, configured in (
                (b'{"permissions": {"defaultMode": "a", "defaultMode": "b"}}', True),
                (b'{"includeCoAuthoredBy": true, "includeCoAuthoredBy": false,'
                 b' "permissions": {"defaultMode": "bypassPermissions"}}', True),
                (b'{"permissions": {"defaultMode": "plan"}, "permissions": {"allow": ["Bash"]}}', False)):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.assertEqual(self.configured(), configured)
                self.assertEqual(managed.read_settings(path), {})

    def test_compile_carries_the_decision_and_nothing_else(self) -> None:
        for default_mode in (None, scope.PERMISSION_DEFAULT_MODE):
            with self.subTest(default_mode=default_mode):
                _lineup, result = test_compiler_v2._seed_launch("balanced", default_mode=default_mode)
                permissions = result.scope_plan.settings["permissions"]
                self.assertEqual(set(permissions), {"deny"} | ({"defaultMode"} if default_mode else set()))
                self.assertEqual(permissions.get("defaultMode"), default_mode)
        with self.assertRaises(scope.ScopeError):
            test_compiler_v2._seed_launch("balanced", default_mode="bypassPermissions")


# ------------------------------------------------------------- real binary


@dataclasses.dataclass(frozen=True)
class _Client:
    label: str
    trusted: probe.TrustedExecutable


class _Responder:
    """PROBE-OK for every request (SSE when streamed); metadata rows only."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe"}}
        system = json.dumps(document.get("system"), sort_keys=True).encode("utf-8")
        with self.lock:
            self.rows.append({"model": document.get("model"), "max_tokens": document.get("max_tokens"),
                              "stream": bool(document.get("stream")),
                              "tools": len(document.get("tools") or ()),
                              "tool_choice": (document.get("tool_choice") or {}).get("type"),
                              "system_class": hashlib.sha256(system).hexdigest()[:12]})
        payload = probe._message_payload([{"type": "text", "text": "PROBE-OK"}], model=document.get("model"),
                                         stop_reason="end_turn", message_id="msg_pd")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


def comparison_clients(pinned: probe.TrustedExecutable) -> tuple[_Client, ...]:
    """The pinned client, then the previous pin when it is retained (hash-verified)."""

    clients = [_Client(pinned.resolved_path.name, pinned)]
    version, digest = PREVIOUS_CLIENT
    path = pinned.resolved_path.parent / version
    if path != pinned.resolved_path and path.is_file() and probe._sha256_file(path) == digest:
        clients.append(_Client(version, probe.TrustedExecutable(path, digest)))
    return tuple(clients)


class _PermissionCase(unittest.TestCase):
    pinned: probe.TrustedExecutable
    clients: tuple[_Client, ...] = ()
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the permission-default probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.pinned = gate.trusted
            cls.clients = comparison_clients(gate.trusted)

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-pd-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.policy = state.ensure_private_dir(self.root / "policy")  # empty: the host has none
        self.count = 0

    def fixture(self) -> probe.ProbeFixture:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count:03d}", environ=self.environ)
        # The client's config dir IS $HOME/.claude, the user layer
        # the decision reads (the launcher unsets CLAUDE_CONFIG_DIR).
        fixture = dataclasses.replace(
            fixture, claude_config_dir=state.ensure_private_dir(fixture.home / ".claude"))
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    @staticmethod
    def layer(path: Path, mode: str | None, **extra: Any) -> None:
        state.ensure_private_dir(path.parent)
        document: dict[str, Any] = {**extra}
        if mode is not None:
            document["permissions"] = {"defaultMode": mode}
        state.atomic_write(path, strict_json.canonical_file_bytes(document))

    def decide(self, fixture: probe.ProbeFixture, launch_environ: dict[str, str] | None = None) -> str | None:
        """The production decision (``Runtime.permission_default_mode``)."""

        environ = {"HOME": str(fixture.home), **(launch_environ or {})}
        if managed.permission_mode_configured(environ, fixture.project_dir, self.policy):
            return None
        return scope.PERMISSION_DEFAULT_MODE

    @staticmethod
    def compiled_permissions(shape: str, default_mode: str | None) -> dict[str, Any]:
        """The production compile's ``permissions`` for one scope shape."""

        if shape == "direct":
            bundle, lcat, eff, _lineup = test_compiler_v2._seed("balanced")
            lead = next(iter(sorted(lcat.lines)))
            lineup = profile.resolve(profile.ad_hoc_direct(lead), lcat, effective=eff)
            result = test_compiler_v2._launch(lineup, eff, bundle=bundle, default_mode=default_mode)
        else:
            _lineup, result = test_compiler_v2._seed_launch(
                "balanced", no_subagents=shape == "no-subagents", default_mode=default_mode)
        return json.loads(json.dumps(result.scope_plan.settings["permissions"]))

    def run_turn(self, client: _Client, fixture: probe.ProbeFixture, permissions: dict[str, Any],
                 extra: tuple[str, ...] = (), *, session: str, resume: bool = False,
                 pty: bool = False) -> tuple[str | None, _Responder]:
        """One turn; returns the observed ``UserPromptSubmit.permission_mode``."""

        events = fixture.root / "pd-events.jsonl"
        script = fixture.root / "pd-hook.py"
        if not script.exists():
            script.write_text(f"#!{sys.executable}\n" + _HOOK_BODY, encoding="utf-8")
            script.chmod(0o700)
        command = f"{shlex.quote(str(script))} {shlex.quote(str(events))}"
        settings = {"permissions": permissions,
                    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command,
                                                               "timeout": 5}]}]}}
        path = fixture.root / f"flag-settings-{uuid.uuid4().hex[:8]}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(settings))
        before = self.events(events)
        argv = ("--resume" if resume else "--session-id", session, "--settings", str(path),
                "--model", LEAD_MODEL, *extra)
        responder = _Responder()
        try:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                common = dict(trusted=client.trusted, fixture=fixture, provider=provider, timeout=150,
                              allow_real=True, environ=self.environ)
                if pty:
                    glyph = "❯".encode("utf-8")
                    onboarding = () if resume else (probe.PTYInteraction(b"Choose", b"2\r"),
                                                    probe.PTYInteraction(b"Press", b"\r"))
                    steps = (*onboarding, probe.PTYInteraction(glyph, TURN.encode() + b"\r"),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                             probe.PTYInteraction(glyph, b"/exit\r"))
                    result = probe.run_native_pty(argv, steps, **common)
                else:
                    result = probe.run_native(("-p", TURN, *argv), **common)
        except probe.ProbeError as exc:
            if _LIVE_DOMAIN_TOUCHED in str(exc):
                self.skipTest(f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; inconclusive, never a pass")
            raise
        self.assertFalse(result.timed_out, f"{client.label}: timed out {extra}")
        self.assertEqual(result.returncode, 0, f"{client.label}: exit {result.returncode} {extra}")
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        new = [row for row in self.events(events)[len(before):] if row["hook_event_name"] == "UserPromptSubmit"]
        self.assertEqual(len(new), 1, f"{client.label}: UserPromptSubmit events {new}")
        self.assertEqual(new[0]["session_id"], session)
        return new[0]["permission_mode"], responder

    @staticmethod
    def events(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def record(self, line: str) -> None:
        print(f"permission-default: {line} pinned={self.pinned.resolved_path.name}/{self.pinned.sha256[:12]}")


class PermissionDefaultsTests(_PermissionCase):
    """(a)-(f) on the exact clients."""

    def test_omitted_mode_profile_direct_noagents_fresh_resume(self) -> None:
        """(a) plus the omitted control that motivates it."""

        omitted: dict[str, str | None] = {}
        for client in self.clients:
            fixture = self.fixture()
            session = str(uuid.uuid4())
            omitted[client.label], _ = self.run_turn(
                client, fixture, self.compiled_permissions("profile", None), session=session)
            for shape in ("profile", "direct", "no-subagents"):
                fixture = self.fixture()
                decision = self.decide(fixture)
                self.assertEqual(decision, scope.PERMISSION_DEFAULT_MODE)
                permissions = self.compiled_permissions(shape, decision)
                self.assertEqual(permissions["defaultMode"], "default")
                session = str(uuid.uuid4())
                for resume in (False, True):
                    with self.subTest(client=client.label, shape=shape, resume=resume):
                        mode, _ = self.run_turn(client, fixture, permissions, session=session, resume=resume)
                        self.assertEqual(mode, "default")
        # The compiled key is load-bearing on the pinned client: without it
        # an unconfigured session starts in another mode (2.1.284+: auto).
        self.assertNotEqual(omitted[self.clients[0].label], "default", omitted)
        self.record("(a) compiled default -> default on profile/direct/no-subagents fresh+resume; omitted control "
                    + " ".join(f"{label}->{mode}" for label, mode in omitted.items()))

    def test_configured_layers_compile_nothing(self) -> None:
        """(b) user bypass, (c) project/local layer, (d) forced-default negative control."""

        observed: dict[str, str | None] = {}
        for client in self.clients:
            cases = (
                ("b-user-bypass", ".claude/settings.json", "bypassPermissions", True, False),
                ("c-project", "project/.claude/settings.json", "acceptEdits", False, False),
                ("c-local", "project/.claude/settings.local.json", "plan", False, False),
                ("d-forced-default", ".claude/settings.json", "bypassPermissions", True, True),
            )
            for label, relative, mode, skip_prompt, forced in cases:
                fixture = self.fixture()
                extra = {"skipDangerousModePermissionPrompt": True} if skip_prompt else {}
                self.layer(fixture.home / relative, mode, **extra)
                decision = self.decide(fixture)
                self.assertIsNone(decision, label)
                permissions = self.compiled_permissions("profile", scope.PERMISSION_DEFAULT_MODE if forced
                                                        else decision)
                session = str(uuid.uuid4())
                lifecycles = (False, True) if label == "b-user-bypass" else (False,)
                for resume in lifecycles:
                    with self.subTest(client=client.label, case=label, resume=resume):
                        got, _ = self.run_turn(client, fixture, permissions, session=session, resume=resume)
                        self.assertEqual(got, "default" if forced else mode)
                        observed[f"{client.label}/{label}{'/resume' if resume else ''}"] = got
        self.record("(b)(c)(d) " + " ".join(f"{key}->{value}" for key, value in observed.items()))

    def test_duplicate_key_user_layer_compiles_nothing(self) -> None:
        """(g) A user layer with ``bypassPermissions`` plus an
        unrelated duplicate key. The client applies it (last key wins), so
        the decision compiles nothing and the session runs in that mode."""

        observed: dict[str, str | None] = {}
        for client in self.clients:
            fixture = self.fixture()
            path = fixture.home / ".claude/settings.json"
            state.atomic_write(path, b'{"includeCoAuthoredBy": true, "includeCoAuthoredBy": false, '
                                     b'"skipDangerousModePermissionPrompt": true, '
                                     b'"permissions": {"defaultMode": "bypassPermissions"}}')
            self.assertEqual(managed.read_settings(path), {}, "the strict reader refuses the duplicate")
            decision = self.decide(fixture)
            self.assertIsNone(decision)
            with self.subTest(client=client.label):
                got, _ = self.run_turn(client, fixture, self.compiled_permissions("profile", decision),
                                       session=str(uuid.uuid4()))
                self.assertEqual(got, "bypassPermissions")
                observed[client.label] = got
        self.record("(g) duplicate-key user bypass -> nothing compiled -> "
                    + " ".join(f"{label}->{mode}" for label, mode in observed.items()))

    def test_claude_config_dir_never_decides(self) -> None:
        """(f): CLAUDE_CONFIG_DIR in the launching environment holds another
        mode; the decision and the client follow $HOME/.claude."""

        observed: dict[str, str | None] = {}
        for client in self.clients:
            for label, home_mode, other_mode in (("f-home-configured", "acceptEdits", "plan"),
                                                 ("f-home-unconfigured", None, "plan")):
                fixture = self.fixture()
                other = state.ensure_private_dir(fixture.root / "launch-config-dir")
                self.layer(other / "settings.json", other_mode)
                if home_mode is not None:
                    self.layer(fixture.home / ".claude/settings.json", home_mode)
                decision = self.decide(fixture, {"CLAUDE_CONFIG_DIR": str(other)})
                self.assertEqual(decision, None if home_mode else scope.PERMISSION_DEFAULT_MODE)
                permissions = self.compiled_permissions("profile", decision)
                session = str(uuid.uuid4())
                for resume in (False, True):
                    with self.subTest(client=client.label, case=label, resume=resume):
                        got, _ = self.run_turn(client, fixture, permissions, session=session, resume=resume)
                        self.assertEqual(got, home_mode or "default")
                        self.assertNotEqual(got, other_mode)
                        observed[f"{client.label}/{label}{'/resume' if resume else ''}"] = got
        self.record("(f) " + " ".join(f"{key}->{value}" for key, value in observed.items()))

    def test_explicit_mode_overrides_flag_settings(self) -> None:
        """(e): every accepted --permission-mode and --dangerously-skip-permissions
        win over the compiled default and a configured user layer."""

        observed: dict[str, str | None] = {}
        surfaces = [*((mode, ("--permission-mode", mode)) for mode in ACCEPTED_MODES),
                    ("dangerously-skip-permissions", ("--dangerously-skip-permissions",))]
        for client in self.clients:
            for layer_mode in (None, "acceptEdits"):
                for label, argv in surfaces:
                    fixture = self.fixture()
                    if layer_mode is not None:
                        self.layer(fixture.home / ".claude/settings.json", layer_mode)
                    permissions = self.compiled_permissions("profile", scope.PERMISSION_DEFAULT_MODE)
                    with self.subTest(client=client.label, surface=label, layer=layer_mode):
                        got, _ = self.run_turn(client, fixture, permissions, argv, session=str(uuid.uuid4()))
                        expected = {"dangerously-skip-permissions": "bypassPermissions",
                                    "manual": "default"}.get(label, label)
                        self.assertEqual(got, expected)
                        observed[f"{client.label}/{label}/{layer_mode or 'none'}"] = got
        self.record("(e) " + " ".join(f"{key}->{value}" for key, value in observed.items()))

    def test_pty_headless_workflow_effective_mode(self) -> None:
        """(a) on the interactive entrypoint, fresh and resume."""

        observed: dict[str, str | None] = {}
        for client in self.clients:
            fixture = self.fixture()
            permissions = self.compiled_permissions("profile", self.decide(fixture))
            session = str(uuid.uuid4())
            for resume in (False, True):
                with self.subTest(client=client.label, resume=resume):
                    got, _ = self.run_turn(client, fixture, permissions, session=session, resume=resume, pty=True)
                    self.assertEqual(got, "default")
                    observed[f"{client.label}/pty{'/resume' if resume else ''}"] = got
        self.record("(a-pty) " + " ".join(f"{key}->{value}" for key, value in observed.items()))


class _ToolResponder:
    """Forces one tool_use (``Agent``/``Bash``) on the first lead request that
    offers it; classifies the tool_result in memory (refused by auto mode,
    awaiting review, needs approval, launched/ran). Metadata rows only."""

    CLASSES = (("auto-unavailable", "auto mode cannot determine the safety"),
               ("review", "Review dynamic workflow"), ("needs-approval", "needs approval"),
               ("launched", "launched successfully"), ("ran", "completed with no output"))

    def __init__(self, tool: str, tool_input: dict[str, Any]) -> None:
        self.tool, self.tool_input = tool, tool_input
        self.lock = threading.Lock()
        self.forced = False
        self.rows: list[dict[str, Any]] = []
        self.result_class: str | None = None

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        names = [tool.get("name") for tool in document.get("tools") or () if isinstance(tool, dict)]
        system = json.dumps(document.get("system"), sort_keys=True).encode("utf-8")
        with self.lock:
            self.rows.append({"model": document.get("model"), "max_tokens": document.get("max_tokens"),
                              "stream": bool(document.get("stream")), "tools": len(names),
                              "system_class": hashlib.sha256(system).hexdigest()[:12]})
            for message in document.get("messages") or ():
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else ():
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("tool_use_id") == "toolu_pd_forced":
                        text = json.dumps(block.get("content"))
                        self.result_class = next((label for label, needle in self.CLASSES if needle in text),
                                                 "other")
            if self.tool in names and not self.forced:
                self.forced = True
                content = [{"type": "tool_use", "id": "toolu_pd_forced", "name": self.tool, "input": self.tool_input}]
                stop = "tool_use"
            else:
                content, stop = [{"type": "text", "text": "PROBE-OK"}], "end_turn"
        payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop, message_id="msg_pd")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class AutoModeAndWorkflowTests(_PermissionCase):
    """(a) a default-mode Workflow child dispatch is positively
    observed with its compiled selectors; (b) ``auto`` (reached only by the
    operator's explicit choice) is discriminated against an
    explicit ``default`` with two fixture designs, by request metadata."""

    LEAD = "claude-opus-5-5[1m]"

    def test_workflow_child_dispatch_in_default_mode(self) -> None:
        import test_client_skill_workflow as wf

        observed = {}
        for client in self.clients:
            fixture = self.fixture()
            scope_dir = state.ensure_private_dir(fixture.root / "scope")
            agents = state.ensure_private_dir(scope_dir / ".claude" / "agents")
            state.atomic_write(agents / "cm-spike-w.md", wf._AGENT_W.encode("utf-8"))
            probe.seed_fixture_project_trust(fixture, scope_dir)
            settings = {"availableModels": sorted([wf._LEAD, wf._AGENT_MODEL, wf._SUB_MODEL]), "model": wf._LEAD,
                        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": wf._SUB_MODEL},
                        "permissions": self.compiled_permissions("profile", self.decide(fixture))}
            self.assertEqual(settings["permissions"]["defaultMode"], "default")
            path = fixture.root / "workflow-settings.json"
            state.atomic_write(path, strict_json.canonical_file_bytes(settings))
            responder = wf._WorkflowResponder(wf._workflow_script(("default", "typed")), fixture.project_dir)

            def both_children() -> None:
                deadline = time.monotonic() + 80
                while len(responder.agents) < 2 and time.monotonic() < deadline:
                    time.sleep(0.2)

            glyph = "❯".encode("utf-8")
            steps = (probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                     probe.PTYInteraction(glyph, b"client check S10\r"),
                     # The default-mode review dialog ("Run a dynamic workflow?"):
                     # the operator approves with 1 (Yes, run it). The dialog
                     # attaches its key handler after its footer paints, so a
                     # key sent at once can be lost (as for S4's Esc).
                     probe.PTYInteraction(b"amend", b"1", before_send=lambda: time.sleep(0.8)),
                     probe.PTYInteraction(glyph, b"/exit\r", before_send=both_children,
                                          callback_timeout=90))
            try:
                with probe.FakeAnthropicProvider(responder=responder) as provider:
                    result = probe.run_native_pty(
                        ("--settings", str(path), "--add-dir", str(scope_dir), "--model", wf._LEAD), steps,
                        trusted=client.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                        environ=self.environ, child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"})
            except probe.ProbeError as exc:
                if _LIVE_DOMAIN_TOUCHED in str(exc):
                    self.skipTest(f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; inconclusive, never a pass")
                raise
            with self.subTest(client=client.label):
                self.assertEqual(result.returncode, 0)
                self.assertTrue(responder.workflow_offered)
                self.assertEqual(responder.agents["default"]["model"], wf._SUB_MODEL)
                self.assertEqual(responder.agents["default"]["agent_type"], "workflow-subagent")
                self.assertEqual(responder.agents["typed"]["model"], wf._AGENT_MODEL)
                self.assertEqual(responder.agents["typed"]["request_class"], "workflow")
                observed[client.label] = {name: row["model"] for name, row in sorted(responder.agents.items())}
        self.record(f"(h) default-mode workflow children dispatched with compiled selectors {observed}")

    def test_auto_classifier_shape_and_negative_control(self) -> None:
        designs = (
            ("agent-spawn", "Agent", {"description": "pd", "subagent_type": "general-purpose",
                                   "prompt": "Reply PROBE-OK"}),
            ("tool-bash", "Bash", {"command": "touch pd-marker-file", "description": "pd"}),
        )
        client = self.clients[0]  # the pinned client
        summary = {}
        for design, tool, tool_input in designs:
            shapes = {}
            for mode in ("default", "auto"):
                fixture = self.fixture()
                # The compiled default mode alone (the profile's native-agent
                # denies would refuse general-purpose before any mode decides).
                settings = {"availableModels": [self.LEAD], "model": self.LEAD,
                            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}}
                path = fixture.root / "auto-settings.json"
                state.atomic_write(path, strict_json.canonical_file_bytes(settings))
                responder = _ToolResponder(tool, tool_input)
                try:
                    with probe.FakeAnthropicProvider(responder=responder) as provider:
                        result = probe.run_native(
                            ("-p", TURN, "--settings", str(path), "--model", self.LEAD, "--permission-mode", mode),
                            trusted=client.trusted, fixture=fixture, provider=provider, timeout=150,
                            allow_real=True, environ=self.environ)
                except probe.ProbeError as exc:
                    if _LIVE_DOMAIN_TOUCHED in str(exc):
                        self.skipTest(f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; inconclusive, never a pass")
                    raise
                self.assertEqual(result.returncode, 0, f"{design}/{mode}")
                # Every request reached the fake provider on the lead's wire:
                # nothing the client sent is attributable to a classifier.
                wires = {row["model"] for row in responder.rows}
                self.assertEqual(wires, {self.LEAD.removesuffix("[1m]")}, f"{design}/{mode}")
                shapes[mode] = {"requests": len(responder.rows), "result": responder.result_class,
                                "marker": (fixture.project_dir / "pd-marker-file").exists(),
                                "max_tokens": sorted({row["max_tokens"] for row in responder.rows}),
                                "system_classes": len({row["system_class"] for row in responder.rows})}
            summary[design] = shapes
            # No extra (classifier) request under auto: the same request
            # count as the explicit-default negative control at most.
            self.assertLessEqual(shapes["auto"]["requests"], shapes["default"]["requests"], design)
            self.assertEqual(shapes["auto"]["max_tokens"], shapes["default"]["max_tokens"], design)
        # Agent under auto: refused visibly (no classifier through the
        # gateway); under default it launches. Bash ``touch`` in the project
        # runs under auto without any classifier request; default needs approval.
        self.assertEqual(summary["agent-spawn"]["auto"]["result"], "auto-unavailable")
        self.assertEqual(summary["agent-spawn"]["default"]["result"], "launched")
        self.assertEqual(summary["tool-bash"]["auto"]["result"], "ran")
        self.assertEqual(summary["tool-bash"]["default"]["result"], "needs-approval")
        self.assertTrue(summary["tool-bash"]["auto"]["marker"])
        self.assertFalse(summary["tool-bash"]["default"]["marker"])
        self.record(f"(i) auto vs default (no classifier request reaches the gateway; Agent refused "
                    f"visibly under auto) {summary}")


if __name__ == "__main__":
    unittest.main()
