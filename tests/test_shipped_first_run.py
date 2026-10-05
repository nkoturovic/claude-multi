"""The shipped assets on a first run: every way to connect the picker
offers either reaches a model or says how. A shipped keyed provider
without models is named as such, its saved key continues into adding a
model (the command line names the next steps, in order), and the profile
step names them again instead of stopping at "no lead"; every unavailable
route carries its reason, and the OpenAI API key reaches a reviewed model.

Every shipped keyed provider ships models now, so the provider without
models is synthetic: a copy of the shipped assets with one keyed
provider's lines removed, taken through the key, the next steps, adding a
model and admitting it, then the profile step saving that starter as the
default, read back ready with the admitted model as its lead. A setup with
only one keyed provider (Kimi alone, Qwen alone, and the synthetic one
without models, through its listing or by hand) reaches a usable profile
through the guided TUI journey. Read from the packaged catalog (model ids
are derived, never pinned); temp home, no gateway, no provider request:
the listing and the admission's test request are mocked seams."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tui_fixture as fx
import test_cli
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from test_tui import DOWN, ENTER, ESC, FakeWindow
from claude_multi import catalog, cli, operator as operator_mod, profile, proxy, state, strict_json, tui
from claude_multi.setup import defaults, providers as setup_providers, status as setup_status, texts
import claude_multi.cli.commands.discover as discover_cmd
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.providers as providers_screen
import claude_multi.cli.text as cli_text


class _ShippedCase(unittest.TestCase):
    def assets(self, tmp: Path) -> Path:
        return SHIPPED_ROOT

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cm-shipped-first-run-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.runtime = fx.fixture_runtime(tmp, asset_root=self.assets(tmp), secrets=())
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(self.runtime))
        stack.enter_context(mock.patch.object(consent, "stdio_ttys", return_value=True))
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        self.runtime.profiles.install_seeds()


@uses_shipped_catalog
class ShippedFirstRunTests(_ShippedCase):

    def test_every_route_reaches_a_model_or_says_why_not(self) -> None:
        entries = setup_providers.picker_entries(self.runtime)
        self.assertTrue(entries)
        lines = self.runtime.lineup_catalog().lines
        for entry in entries:
            with self.subTest(entry=entry.id):
                if not entry.available:
                    self.assertTrue(entry.note or entry.state_text, "an unavailable route says why")
                    continue
                if entry.kind == "api-key" and entry.provider_id != "anthropic":
                    has_models = any(line.get("provider") == entry.provider_id for line in lines.values())
                    self.assertEqual(setup_status.without_models(self.runtime, (entry.provider_id,)),
                                     () if has_models else (entry.provider_id,))
        # The OpenAI API key is offered, and the shipped catalog reviews at
        # least one line for it, so a key alone reaches a model.
        offered = operator_mod.transport_alternative("openai", operator_mod.TRANSPORT_API_KEY)
        self.assertTrue(offered.available)
        self.assertIsNone(offered.closed_reason)
        self.assertTrue(operator_mod.key_route_lines(self.runtime.catalog.docs, "openai"))


def modelless_copy(target: Path) -> tuple[Path, str]:
    """A copy of the shipped assets with one keyed, non-aggregator
    provider's lines removed: the first (by id) whose lines no shipped seed
    binds and no retired entry names as its successor."""

    root = target / "assets"
    shutil.copytree(SHIPPED_ROOT, root)
    raw = catalog.load_catalog(root).docs
    models = raw["models-v2"]["models"]
    seeds = {str(spec.get("model")) for document in catalog.load_catalog(root).seed_profiles.values()
             for spec in [document.get("lead"), *(document.get("agents") or {}).values()]
             if isinstance(spec, dict)}
    successors = {entry.get("successor") for entry in raw["retired"]["retired"].values()}
    for pid, provider in sorted(raw["providers"]["providers"].items()):
        ref = (provider["transport"].get("auth") or {}).get("secret_ref")
        keys = {key for key, entry in models.items() if entry["provider"] == pid}
        if (provider["transport"]["kind"] == "direct" and isinstance(ref, str) and keys
                and pid not in catalog.AGGREGATOR_PROVIDERS and not keys & (seeds | successors)):
            break
    else:
        raise AssertionError("no shipped keyed provider can lose its lines")
    path = root / "catalog" / "models.json"
    document = strict_json.load(path)
    document["models"] = {key: entry for key, entry in document["models"].items() if entry["provider"] != pid}
    path.write_bytes(strict_json.pretty_file_bytes(document))
    return root, pid


@uses_shipped_catalog
class SyntheticModellessProviderTests(test_cli.OperatorCommandCase):
    """A keyed provider with no model on the shipped assets, alone: its key,
    the steps the command line and the profile step name, a model added by
    hand and admitted, then a usable starter profile."""

    def setUp(self) -> None:
        super().setUp()
        temporary = Path(tempfile.mkdtemp(prefix="cm-modelless-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        self.runtime.asset_root, self.pid = modelless_copy(temporary)
        self.runtime.reload_catalog()
        state.atomic_write(self.secret_file, b"")
        for pid in sorted(self.runtime.catalog.docs["providers"]["providers"]):
            if pid != self.pid:
                code, _out, err = self.op(["providers", "disable", pid])
                self.assertEqual(code, 0, err)

    def test_the_command_line_names_the_manual_path_to_a_profile_in_order(self) -> None:
        pid = self.pid
        key_file = self.root / "provider.key"
        state.atomic_write(key_file, b"cli-dummy-key\n")
        steps = (f"claude-multi discover {pid})", f"claude-multi discover {pid} --add WIRE",
                 f"claude-multi models add {pid} WIRE", "claude-multi models admit KEY",
                 "claude-multi setup --step profile")
        for argv, answers in ((["providers", "set-key", pid, "--secret-file", str(key_file)], ""),
                              (["setup", "--step", "profile"], "\n")):
            with self.subTest(argv=argv[:2]):
                _code, out, err = self.op(argv, answers)
                line = next(line for line in out.splitlines() if line.startswith(f"next: {pid} "))
                positions = [line.find(step) for step in steps]
                self.assertNotIn(-1, positions, line)
                self.assertEqual(positions, sorted(positions), line)
                self.assertNotIn("cli-dummy-key", out + err)

    def test_the_key_continues_through_adding_and_admitting_to_a_profile(self) -> None:
        pid = self.pid
        entry = next(item for item in setup_providers.picker_entries(self.runtime) if item.id == f"{pid}:api-key")
        self.assertEqual((entry.note, entry.available), (texts.NO_MODELS_NOTE, True))
        key_file = self.root / "modelless.key"
        state.atomic_write(key_file, b"modelless-dummy-key\n")
        code, out, err = self.op(["providers", "set-key", pid, "--secret-file", str(key_file)])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"next: {pid} has no models yet", out)
        self.assertIn(f"claude-multi discover {pid} --add WIRE", out)
        self.assertNotIn("modelless-dummy-key", out + err)
        self.assertEqual(setup_status.without_models(self.runtime, (pid,)), (pid,))
        code, out, err = self.op(["setup", "--step", "profile"], "\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn(texts.SETUP_STARTER_NO_LEAD, out + err)
        self.assertIn(texts.NO_MODELS_NEXT.format(id=pid), out + err)
        key = f"custom-{pid}-first"
        code, _out, err = self.op(["models", "add", pid, f"{pid}-first-1", "--as", key, "--context", "200000",
                                   "--source", "operator", "--effort", "high"])
        self.assertEqual(code, 0, err)
        self.serve_current()
        code, out, err = self.op(["models", "admit", key], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.calls, [key])
        self.assertEqual(setup_status.without_models(self.runtime, (pid,)), ())
        plan, _warnings = self.runtime.starter_plan("starter")
        self.assertIsNotNone(plan.document, plan.refusal)
        self.assertEqual(plan.document["lead"]["model"], key)
        # The public profile step saves that starter and makes it the default.
        self.assertNotIn("starter", self.runtime.profiles.names())
        code, out, err = self.op(["setup", "--step", "profile"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.SETUP_STARTER_SAVED.format(name="starter"), out + err)
        # Read back from disk (the catalog reloaded, a new store): saved,
        # selected, its lead the admitted model, and ready.
        self.runtime.reload_catalog()
        reloaded = self.runtime
        self.assertTrue(reloaded.profiles.has_user("starter"))
        saved = reloaded.profiles.load("starter")
        self.assertEqual(saved["lead"]["model"], key)
        for slot in (saved.get("agents") or {}).values():
            self.assertEqual(reloaded.lineup_catalog().lines[slot["model"]]["provider"], pid)
        self.assertEqual(defaults.chosen_default(reloaded), "starter")
        choice = defaults.resolve_default(reloaded)
        self.assertEqual((choice.name, choice.reason, choice.ready), ("starter", "default", True))
        evaluation = profile.evaluate(saved, reloaded.lineup_catalog(), bindings={},
                                      effective=reloaded.current_effective())
        self.assertEqual(evaluation.errors, ())
        self.assertEqual((evaluation.lineup.lead.binding.key, evaluation.lineup.lead.binding.provider), (key, pid))
        code, out, err = self.op(["setup", "--step", "profile"], "\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.SETUP_PROFILE_FIT.format(name="starter", why=choice.reason_text), out + err)


class _JourneyCase(_ShippedCase):
    """One keyed provider alone, connected on the Providers screen and taken
    to a starter profile that launches. The provider calls are mocked: the
    listing's one request and the admission's test request."""

    KEY = "journey-dummy-key"

    def setUp(self) -> None:
        super().setUp()
        # The fixture gateway serves what is published (reload proof included).
        def served(_gateway, _token):
            config = proxy.config_dir(self.runtime.home) / "config.yaml"
            return (set(re.findall(r'alias: "([^"]+)"', config.read_text())) if config.exists() else set()), 200

        self.runtime.served_models_callback = served
        self.shown: list[tuple[str, list[str]]] = []
        self.listings: list[str] = []
        for patcher in (
            mock.patch.object(screens_common.OnboardingActions, "confirm", lambda _self, text: True),
            mock.patch.object(screens_common.OnboardingActions, "preview", lambda _self, text: True),
            mock.patch.object(screens_common.OnboardingActions, "show",
                              lambda _self, title, lines: self.shown.append((title, list(lines)))),
            mock.patch.object(tui.OnboardingForm, "run", self._accept),
            mock.patch.object(discover_cmd, "_fetch", self._listing),
            mock.patch.object(operator_mod, "smoke_current", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _listing(self, _runtime, url, _headers) -> bytes:
        self.listings.append(url)
        return json.dumps({"data": [{"id": "journey-model-1", "display_name": "Journey 1",
                                     "context_length": 262144}]}).encode()

    @staticmethod
    def _accept(form, _win) -> dict[str, str]:
        """The declaration form as prefilled; by hand, the person's answers."""

        values = {name: field.value for name, field in form.inputs.items()}
        if not values["wire"]:
            values.update(wire="journey-model-1", key="custom-journey-model-1", context="131072",
                          source="docs", ref="https://provider.example/models 2026-10-04")
        return values

    def journey(self, pid: str, *, by_hand: bool = False) -> None:
        self.assertEqual(setup_status.connected(self.runtime), (), "nothing is connected at the start")
        modelless = setup_status.without_models(self.runtime, (pid,)) == (pid,)
        screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        screen.selected = [row.id for row in screen.rows].index(pid)
        keys = ["K", *self.KEY, ENTER, ENTER]  # the key, Save
        if modelless:
            # List its models (one request) and declare the one listed, or
            # enter it by hand; then Admit.
            keys += [DOWN, ENTER] if by_hand else [ENTER, " ", ENTER]
            keys += [ENTER]
        win = FakeWindow([*keys, ESC, ESC], height=30, width=100)
        screen.run(win)
        if modelless:
            self.assertIn(cli_text.NO_MODELS_ADMITTED.format(id=pid, keys="custom-journey-model-1"), screen.message)
            self.assertEqual(len(self.listings), 0 if by_hand else 1)
        # A usable profile: the starter step saves it as the default, and it launches.
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["setup", "--step", "profile"], runtime=self.runtime, output_stream=out,
                            input_stream=io.StringIO("y\n"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(texts.SETUP_STARTER_SAVED.format(name="starter"), out.getvalue())
        lead = self.runtime.profiles.load("starter")["lead"]["model"]
        self.assertEqual(self.runtime.lineup_catalog().lines[lead]["provider"], pid)
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--print-launch"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("record: profile starter", out.getvalue())
        self.assertIn("would launch", out.getvalue())
        everything = "\n".join([*win.frames, screen.message, repr(self.shown), out.getvalue()])
        self.assertNotIn(self.KEY, everything)


@uses_shipped_catalog
class SingletonJourneyTests(_JourneyCase):
    """Kimi alone, and Qwen alone: the key reaches the provider's shipped
    models and a starter profile that launches."""

    def test_kimi_alone_reaches_a_usable_profile(self) -> None:
        self.journey("kimi")

    def test_qwen_alone_reaches_a_usable_profile(self) -> None:
        self.journey("qwen")


@uses_shipped_catalog
class ModellessJourneyTests(_JourneyCase):
    """The synthetic keyed provider without models, alone: its key
    continues into its listing or a model by hand, the admission, and a
    starter profile that launches."""

    def assets(self, tmp: Path) -> Path:
        root, self.pid = modelless_copy(tmp)
        return root

    def test_a_provider_without_models_reaches_a_usable_profile_through_its_listing(self) -> None:
        self.assertEqual(setup_status.without_models(self.runtime, (self.pid,)), (self.pid,))
        self.journey(self.pid, by_hand=False)

    def test_a_provider_without_models_reaches_a_usable_profile_by_hand(self) -> None:
        self.assertEqual(setup_status.without_models(self.runtime, (self.pid,)), (self.pid,))
        self.journey(self.pid, by_hand=True)


if __name__ == "__main__":
    unittest.main()
