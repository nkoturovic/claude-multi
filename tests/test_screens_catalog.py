"""The Models, Providers and Settings screens.

Every test runs on a hermetic ``_tui_fixture.fixture_runtime`` inside
``hermetic(runtime)`` with doctor's clock at ``FIXED_NOW``; nothing reads the
host journal, ``/proc`` or the daemon socket directory.  Expected rows, ids and
counts derive from the loaded fixture catalog: no fixture id is a
literal here.  Screens are driven on ``test_tui.FakeWindow`` in
``MONO_PALETTE``; every script ends with Esc ("exit and restore").
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import os
import shutil
import stat
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, custom, profile, proxy, settings, state, strict_json, tui, views

import _tui_fixture as fx
import claude_multi.cli.text as cli_text
import _tui_render
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from test_tui import DOWN, END, HOME, LEFT, RIGHT, UP, FakeWindow
import claude_multi.management
import claude_multi.service
import subprocess

SPEC = "the catalog screens contract"
ESC = "\x1b"
ENTER = "\n"
REGISTRY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "registry"


def _fixture_copy(case: unittest.TestCase, *, new: tuple[str, ...] = ()) -> Path:
    """A writable fixture asset copy with ``new`` lines set to ``status: "new"``."""

    temporary = Path(tempfile.mkdtemp(prefix="cm-screens-catalog-assets-"))
    case.addCleanup(shutil.rmtree, temporary, True)
    root = temporary / "claude-multi"
    shutil.copytree(FIXTURE_ROOT, root)
    for root_dir, _dirs, files in os.walk(root):
        os.chmod(root_dir, 0o755)
        for name in files:
            os.chmod(Path(root_dir) / name, 0o644)
    path = root / "catalog" / "models.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for key in new:
        document["models"][key]["status"] = "new"
    path.write_bytes(strict_json.pretty_file_bytes(document))
    return root


def new_line_key(cat: catalog.Catalog) -> str:
    """An agents-capable line no seed names (the catalog has no legacy
    default composition)."""

    bound: set[str] = set()
    for document in cat.seed_profiles.values():
        slots = [document.get("lead"), *document.get("agents", {}).values()]
        bound |= {slot["model"] for slot in slots if isinstance(slot, dict) and "model" in slot}
    key = next(
        (k for k, e in cat.lines.items() if "agents" in e["capabilities"] and k not in bound),
        None,
    )
    if key is None:
        raise AssertionError(f"{SPEC} §7.1: the fixture has a line to make New")
    return key


def _write_settings(runtime: cli.Runtime, data: bytes) -> Path:
    path = settings.settings_path(runtime.environ)
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, data)
    return path


class _ScreenCase(unittest.TestCase):
    runtime_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-catalog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.make_runtime(self.tmp / "root", **self.runtime_kwargs)
        guard = mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    def make_runtime(self, root: Path, **kwargs) -> fx.ScreenRuntime:
        runtime = fx.fixture_runtime(root, **kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(runtime))
        stack.enter_context(mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW))
        return runtime

    def settings_doc(self, runtime: cli.Runtime | None = None) -> dict:
        path = settings.settings_path((runtime or self.runtime).environ)
        return json.loads(path.read_text()) if path.exists() else {}

    @staticmethod
    def run_screen(screen, keys, *, height=24, width=80) -> FakeWindow:
        win = FakeWindow(list(keys), height=height, width=width)
        screen.run(win)
        return win

    def assertFloor(self, factory) -> None:
        """The floor from ``min_size``; one row or column less is too small."""

        screen = factory()
        rows, cols = screen.min_size(80)
        too_small = tui.TOO_SMALL.format(what=screen.what)

        def frame(height: int, width: int) -> str:
            win = FakeWindow([], height=height, width=width)
            factory()._draw(win)
            return win.text()

        self.assertIn(too_small, frame(rows - 1, 80))
        self.assertIn(tui.TOO_SMALL_HINT.format(cols=cols, rows=rows), frame(rows - 1, 80))
        fits = frame(rows, 80)
        self.assertNotIn(too_small, fits)
        self.assertIn("Esc back", fits.splitlines()[-1])
        self.assertTrue(fits.splitlines()[1].strip(), "the title is visible at the floor")
        narrow_rows, _cols = screen.min_size(cols)
        self.assertNotIn(too_small, frame(narrow_rows, cols))
        self.assertIn(too_small, frame(narrow_rows, cols - 1))
        self.assertIn(too_small, frame(narrow_rows - 1, cols))
        # Below the floor only Esc acts.
        win = FakeWindow(["j", ENTER, "?", ESC], height=rows - 1, width=80)
        self.assertIsNone(factory().run(win))


# ================================================================ Models


class ModelsScreenTests(_ScreenCase):
    def screen(self, runtime=None, **kwargs) -> cli._ModelsScreen:
        return cli._ModelsScreen(runtime or self.runtime, palette=tui.MONO_PALETTE, **kwargs)

    def rows(self, runtime=None) -> tuple[views.LineRow, ...]:
        runtime = runtime or self.runtime
        registry = custom.load_registry(runtime.environ)
        return views.line_rows(
            runtime.lineup_catalog(), runtime.current_effective(),
            custom_ids=frozenset(registry["models"]),
        )

    def row_line(self, text: str, key: str) -> str:
        return next(line for line in text.splitlines() if line[4:].startswith(key + " "))

    def test_every_row_kind_in_catalog_order(self) -> None:
        win = self.run_screen(self.screen(), [ESC], height=40, width=120)
        lines = win.text().splitlines()
        lcat = self.runtime.lineup_catalog()
        self.assertIn(f"models — catalog {lcat.catalog_version}", lines[1])
        body = [line[4:] for line in lines[4:] if line[4:5].strip()]
        catalog_rows = [r for r in self.rows() if r.source == "catalog" and r.status == "active"]
        self.assertTrue(lines[4].startswith("  › "), "the cursor starts on the first row")
        used = views.used_by(
            {n: self.runtime.profiles.load(n) for n in self.runtime.profiles.names()},
            self.runtime.bindings.bindings(), lcat,
        )
        for index, row in enumerate(catalog_rows):
            line = body[index]
            with self.subTest(key=row.key):
                self.assertTrue(line.startswith(row.key + " "), (line, row.key))
                self.assertIn(row.display, line)
                self.assertIn(row.provider, line)
                self.assertIn(row.class_label, line)
                efforts = views.efforts_text(row.efforts)
                self.assertIn(efforts if row.agents_capable else f"{efforts} (lead recommended)", line)
                count = used[row.key]
                expected = f"{count} profile{'s' if count != 1 else ''}" if count else "—"
                self.assertTrue(line.rstrip().endswith(expected), line)
        self.assertTrue(
            lines[5 + len(catalog_rows)].strip().startswith(
                "new (not admitted — Enter adds an optional badge): —"
            )
        )

    def test_the_selected_row_detail(self) -> None:
        first = next(r for r in self.rows() if r.source == "catalog")
        win = self.run_screen(self.screen(), [ESC])
        detail = (
            f"{first.display} · {first.provider_display} · {first.mode}-effort · "
            f"default {first.default_effort}"
        )
        self.assertIn(views.clip(detail, 77), win.text())

    def test_custom_rows_are_listed_once_after_the_catalog_rows(self) -> None:
        provider = next(
            pid for pid, p in sorted(self.runtime.catalog.providers.items())
            if p["transport"]["kind"] == "direct"
        )
        custom.add_model(
            self.runtime.environ, "screens-custom-line", wire_model="screens-custom-wire",
            provider=provider, context_tokens=262144, created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
        )
        win = self.run_screen(self.screen(), [ESC], height=40, width=120)
        text = win.text()
        rows = [line for line in text.splitlines() if line[4:].startswith("screens-custom-line ")]
        self.assertEqual(len(rows), 1, "v2-checks code 20: a custom line is listed once")
        line = self.row_line(text, "screens-custom-line")
        self.assertIn("custom", line)
        self.assertNotIn("direct only", line)
        self.assertTrue(line.rstrip().endswith("—"))
        last_catalog = [r.key for r in self.rows() if r.source == "catalog"][-1]
        self.assertGreater(text.index("screens-custom-line "), text.index(f" {last_catalog} "))
        self.assertLess(text.index("screens-custom-line "), text.index("new (not admitted"))

    def test_new_row_admit_and_revoke(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        screen = self.screen(runtime)
        # Both optional badge actions open on Cancel. Neither changes availability.
        before = next(row for row in screen.line_rows if row.key == key)
        win = self.run_screen(screen, [END, ENTER, LEFT, ENTER, ENTER, RIGHT, ENTER, ESC])
        heading = next(f for f in win.frames if f"› {key} " in f)
        self.assertIn("new (not admitted — Enter adds an optional badge):", heading)
        self.assertNotIn("New · Off", heading)
        self.assertIn("admission: not admitted (optional)", heading)
        self.assertIn("Enter admit badge · Q diagnostics", heading)
        self.assertTrue(any(f"Admit {key}?" in f for f in win.frames))
        self.assertTrue(any(f"admitted {key} — badge only" in f for f in win.frames))
        self.assertTrue(any("Enter revoke badge · Q diagnostics" in f for f in win.frames))
        self.assertTrue(any(f"Revoke {key}?" in f for f in win.frames))
        self.assertEqual(screen.message, f"revoked {key} — badge only; use and qualification unchanged")
        self.assertEqual(self.settings_doc(runtime).get("admitted_lines"), [])
        after = next(row for row in screen.line_rows if row.key == key)
        self.assertTrue(before.offered and after.offered)
        self.assertEqual(before.qualification, after.qualification)

    def test_admit_writes_admitted_lines(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        screen = self.screen(runtime)
        self.run_screen(screen, [END, ENTER, LEFT, ENTER, ESC])
        self.assertEqual(self.settings_doc(runtime)["admitted_lines"], [key])
        self.assertEqual(screen.selected_key, key, "the selection follows the admitted line")
        self.assertIn(key, screen.model(80).admitted)

    def test_cancel_writes_nothing(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        self.run_screen(self.screen(runtime), [END, ENTER, ENTER, ESC])
        self.assertEqual(self.settings_doc(runtime), {})

    def test_unreadable_settings_banner_and_inert_enter(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        path = _write_settings(runtime, b"{")
        # Enter inspects the line instead (no admission while the banner shows).
        win = self.run_screen(self.screen(runtime), [END, ENTER, ESC, ESC])
        text = win.text()
        self.assertIn("settings.json is unreadable:", text)
        # The banner wraps at a TMPDIR-dependent point; compare it re-joined.
        self.assertIn("— admission is unavailable", " ".join(text.split()))
        self.assertFalse(any(f"Admit {key}?" in f for f in win.frames), "no admission")
        self.assertTrue(any("model — full details" in f for f in win.frames))
        self.assertEqual(path.read_bytes(), b"{")

    def test_state_writes_refused(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        runtime.allow_state_writes = False
        screen = self.screen(runtime)
        self.run_screen(screen, [END, ENTER, ESC])
        self.assertEqual(screen.message, cli.SETTINGS_READ_ONLY)
        self.assertEqual(self.settings_doc(runtime), {})

    def test_settings_error_is_the_message(self) -> None:
        key = new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        screen = self.screen(runtime)
        with mock.patch.object(
            settings.SettingsStore, "admit_line", side_effect=settings.SettingsError("store says no")
        ):
            self.run_screen(screen, [END, ENTER, LEFT, ENTER, ESC])
        self.assertEqual(screen.message, "store says no")

    def test_retired_keys_group_by_successor(self) -> None:
        lcat = self.runtime.lineup_catalog()
        null_key = fx.needs_choice_key(self.runtime)
        live = [k for k, e in lcat.lines.items() if e.get("status", "active") == "active"]
        template = lcat.retired[null_key]
        retired = dict(lcat.retired)
        retired["screens-old-a"] = {**copy.deepcopy(template), "successor": live[0]}
        retired["screens-old-b"] = {**copy.deepcopy(template), "successor": live[0]}
        retired["screens-old-c"] = {**copy.deepcopy(template), "successor": live[1]}
        patched = dataclasses.replace(lcat, retired=retired)
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: patched):
            win = self.run_screen(self.screen(), [ESC], height=30, width=120)
        text = win.text()
        self.assertIn(f"screens-old-a screens-old-b → {live[0]}", text)
        self.assertIn(f"screens-old-c → {live[1]}", text)
        self.assertIn(f"{null_key} → (none)", text)

    def test_continuity_clause(self) -> None:
        count = len(cli._doctor_continuity(self.runtime)[0])
        self.assertGreater(count, 0, "the fixture seeds continuity aliases")
        text = self.run_screen(self.screen(), [ESC]).text()
        self.assertIn(f"{count} continuity aliases retained", text)
        with mock.patch('claude_multi.cli.gateway_facts._doctor_continuity', lambda _rt: ({}, "absent", None)):
            text = self.run_screen(self.screen(), [ESC]).text()
        self.assertNotIn("continuity aliases", text)

    def test_radar_line_follows_the_detail(self) -> None:
        runtime = self.make_runtime(
            self.tmp / "radar", environ_extra={catalog.REGISTRY_DIR_ENV: str(REGISTRY_FIXTURE)}
        )
        registry = catalog.load_pinned_registry(REGISTRY_FIXTURE)
        lcat = runtime.lineup_catalog()
        key, when = next(
            (k, registry.codex[e["wire_model"]]["retirement_at"])
            for k, e in lcat.lines.items()
            if (registry.codex.get(e.get("wire_model")) or {}).get("retirement_at")
        )
        moment = datetime.fromisoformat(when.replace("Z", "+00:00")) - timedelta(days=10)
        with mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: moment):
            radar = next(line for line in cli._doctor_retirement_radar(runtime) if line.startswith(f"line {key} ("))
            screen = self.screen(runtime)
            steps = screen.keys.index(key) - screen.index
            win = self.run_screen(screen, ["j"] * steps + [ESC])
        self.assertEqual(screen.selected_key, key)
        self.assertIn(views.clip(radar, 77), win.text())
        # The full radar text sits in help.
        with mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: moment):
            screen = self.screen(runtime)
            # The help scrolls: End shows its last lines, the radar text.
            win = self.run_screen(screen, ["j"] * steps + ["?", END, ESC, ESC], width=160)
        modal = [f for f in win.frames if "models — help" in f][-1]
        self.assertIn(" ".join(radar.split()[:8]), " ".join(modal.split()))

    def test_width_tiers(self) -> None:
        for width, used_by, provider in ((80, True, True), (69, False, True), (63, False, False), (60, False, False)):
            with self.subTest(width=width):
                header = next(l for l in self.run_screen(self.screen(), [ESC], width=width).text().splitlines()
                              if l.lstrip().startswith("line "))
                self.assertEqual("used by" in header, used_by)
                self.assertEqual("provider" in header, provider)
                self.assertIn("efforts", header)

    def test_floor(self) -> None:
        self.assertFloor(self.screen)

    def test_help_modal(self) -> None:
        win = self.run_screen(self.screen(), ["?", END, ESC, ESC])
        first, last = [f for f in win.frames if "models — help" in f][0], \
            [f for f in win.frames if "models — help" in f][-1]
        self.assertIn(" ".join(cli.MODELS_HELP.split()[:6]), " ".join(first.split()))
        flat = " ".join(" ".join(line.strip(" |") for line in last.splitlines()).split())
        self.assertIn("in G (providers).", flat)
        self.assertIn(
            "Retained aliases keep older routes in the gateway configuration; "
            "Providers shows whether routes are served.", flat)

    def test_profile_hint_preselects_its_lead_line(self) -> None:
        lead = self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"]["model"]
        key = self.runtime.lineup_catalog().resolve_key(lead).key
        self.assertEqual(self.screen(profile_hint=catalog.DEFAULT_SEED).selected_key, key)

    def test_runner(self) -> None:
        win = FakeWindow([ESC])
        self.assertIsNone(cli.run_models_screen(self.runtime, win, tui.MONO_PALETTE))
        self.assertTrue(any("models — catalog" in f for f in win.frames))


# ================================================================ Providers


class ProvidersScreenTests(_ScreenCase):
    def screen(self, runtime=None, journal=None) -> cli._ProvidersScreen:
        return cli._ProvidersScreen(
            runtime or self.runtime, palette=tui.MONO_PALETTE,
            journal=journal if journal is not None else (lambda: None),
        )

    def providers(self, runtime=None) -> dict:
        return (runtime or self.runtime).lineup_catalog().providers

    def first(self, kind: str, runtime=None) -> str:
        providers = self.providers(runtime)
        return next(pid for pid in sorted(providers) if providers[pid]["transport"]["kind"] == kind)

    def line_of(self, text: str, pid: str) -> str:
        return next(line for line in text.splitlines() if line[4:].startswith(pid + " "))

    def steps_to(self, screen, pid: str) -> list[str]:
        return [DOWN] * [row.id for row in screen.rows].index(pid)

    def test_rows_and_credential_kinds(self) -> None:
        text = self.run_screen(self.screen(), [ESC], height=30).text()
        self.assertIn(cli.PROVIDERS_TITLE, text)
        self.assertIn("(not a test of the provider)", " ".join(text.split()))
        providers = self.providers()
        for pid in sorted(providers):
            kind = providers[pid]["transport"]["kind"]
            line = self.line_of(text, pid) if pid != sorted(providers)[0] else next(
                l for l in text.splitlines() if l.startswith("  › " + pid)
            )
            with self.subTest(pid=pid):
                self.assertIn(" on ", line)
                expected = {"oauth-pool": "not signed in", "direct": "key set", "direct-openai": "keyless"}[kind]
                self.assertIn(expected, line)
        self.assertNotIn("connected", text.lower())

    def test_oauth_records_refresh_failing_and_missing_keys(self) -> None:
        runtime = self.make_runtime(self.tmp / "keys", secrets=[])
        pools = sorted({p["transport"]["pool"] for p in self.providers(runtime).values()
                        if p["transport"]["kind"] == "oauth-pool"})
        auth = runtime.home / runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        auth.mkdir(parents=True, exist_ok=True)
        (auth / f"{pools[-1]}-user.json").write_text("{}")
        (auth / f"{pools[-1]}-other.json").write_text("{}")
        journal = fx.journal_text(dead_pool=pools[0])
        text = self.run_screen(self.screen(runtime, lambda: journal), [ESC], height=30).text()
        providers = self.providers(runtime)
        for pid in sorted(providers):
            transport = providers[pid]["transport"]
            line = next(l for l in text.splitlines() if l[4:].startswith(pid + " "))
            with self.subTest(pid=pid):
                if transport["kind"] == "oauth-pool":
                    self.assertIn("sign-in failing" if transport["pool"] == pools[0] else "signed in ×2", line)
                elif transport["kind"] == "direct":
                    self.assertIn("key missing", line)

    def test_quota_fact_on_the_selected_row(self) -> None:
        facts = cli._provider_facts(self.runtime).facts
        fact = next(f for f in facts if f["selectors"])
        alias = fact["selectors"][0]
        journal = fx.journal_text(quota=[(alias, "402", 2)])
        screen = self.screen(journal=lambda: journal)
        win = self.run_screen(screen, self.steps_to(screen, fact["id"]) + [ESC])
        self.assertIn(views.clip(f"{alias}: 2× 402 (payment required) in the last 24 h", 77), win.text())

    def test_space_toggles_enabled_in_settings(self) -> None:
        lcat = self.runtime.lineup_catalog()
        pid = next(pid for pid in sorted(lcat.providers) if any(e["provider"] == pid for e in lcat.lines.values()))
        screen = self.screen()
        steps = self.steps_to(screen, pid)
        self.run_screen(screen, steps + [" ", ESC])
        self.assertEqual(self.settings_doc()["providers"][pid], {"enabled": False})
        self.assertEqual(
            screen.message,
            f"{pid} disabled — applies at the next launch or resume; running sessions keep their routes",
        )
        row = next(r for r in screen.rows if r.id == pid)
        self.assertEqual(row.cells[1], "off")
        self.assertIn(
            "off in Settings: new sessions do not fence its models; running sessions keep their routes",
            row.details,
        )
        screen = self.screen()
        self.run_screen(screen, steps + [" ", ESC])
        self.assertEqual(self.settings_doc()["providers"][pid], {"enabled": True})
        self.assertTrue(screen.message.startswith(f"{pid} enabled — "))

    def test_space_refused_without_state_writes(self) -> None:
        self.runtime.allow_state_writes = False
        screen = self.screen()
        self.run_screen(screen, [" ", ESC])
        self.assertEqual(screen.message, cli.SETTINGS_READ_ONLY)
        self.assertEqual(self.settings_doc(), {})

    def test_zero_model_rows(self) -> None:
        zero = views.zero_model_providers(self.runtime.lineup_catalog())
        self.assertTrue(zero, "the fixture has a provider with no line")
        text = self.run_screen(self.screen(), [ESC], height=30, width=120).text()
        for pid in zero:
            line = next(l for l in text.splitlines() if l[4:].startswith(pid + " "))
            self.assertIn(views.PROVIDERS_NO_MODELS, line)
            self.assertNotIn("0/0", line)

    def test_custom_row_detail(self) -> None:
        custom.add_provider(
            self.runtime.environ, "screens-lab", base_url="https://lab.example.com/apps/anthropic",
            auth_kind="bearer", secret_env="SCREENS_LAB_API_KEY",
            catalog_providers=self.runtime.catalog.providers,
        )
        screen = self.screen()
        row = next(r for r in screen.rows if r.id == "screens-lab")
        self.assertTrue(row.custom)
        win = self.run_screen(screen, self.steps_to(screen, "screens-lab") + [ESC], height=30)
        self.assertIn("custom provider — edit or remove it with claude-multi custom …", win.text())
        fact = next(f for f in screen.facts if f["id"] == "screens-lab")
        self.assertEqual((fact["source"], fact["models"]), ("custom", 0))

    def test_a_set_key_is_described_as_set(self) -> None:
        fact = {"id": "keyed", "kind": "direct-anthropic", "credential": "KEYED_API_KEY present",
                "guidance": "set KEYED_API_KEY (masked) with K", "served_note": "1/1 served", "enabled": True}
        missing = dict(fact, credential="KEYED_API_KEY missing")
        eff = self.runtime.current_effective()
        present_row, missing_row = views.provider_rows([fact, missing], eff=eff, journal=None)
        self.assertEqual(present_row.details[0], "API key KEYED_API_KEY is set — K replaces it, X removes it")
        self.assertEqual(missing_row.details[0], "set KEYED_API_KEY (masked) with K")

    def test_w_joins_the_bar_only_while_the_gateway_is_down(self) -> None:
        self.assertNotIn(cli_text.PROVIDERS_GATEWAY_BINDING, self.screen()._keybar_bindings())
        down = self.screen(self.make_runtime(self.tmp / "down-w", gateway_down=True))
        bindings = down._keybar_bindings()
        self.assertIn(cli_text.PROVIDERS_GATEWAY_BINDING, bindings)
        self.assertEqual([key for key, _label in bindings][-2:], ["?", "Esc"])

    def test_a_provider_with_no_model_line_never_asks_for_an_apply(self) -> None:
        from types import SimpleNamespace

        screen = self.screen()
        screen.config_drift = screen.admit_failed = False
        screen.facts = [{"id": "retained", "models_note": "configured · no models"}]
        screen.connections = {"retained": SimpleNamespace(state="connected", served="0/2")}
        self.assertFalse(screen._apply_needed())
        screen.facts = [{"id": "retained", "models_note": None}]
        self.assertTrue(screen._apply_needed())

    def test_w_opens_the_gateway_dialog_and_refreshes(self) -> None:
        screen = self.screen()
        with mock.patch("claude_multi.cli.screens.gateway_actions.dialog",
                        return_value="gateway ready: fixture") as dialog:
            self.run_screen(screen, ["W", ESC])
        dialog.assert_called_once()
        self.assertIs(dialog.call_args.args[0], self.runtime)
        self.assertEqual(screen.message, "gateway ready: fixture")
        self.assertIn("W opens the local gateway", cli.PROVIDERS_HELP)

    def test_gateway_down_banner_names_the_service_hint(self) -> None:
        runtime = self.make_runtime(self.tmp / "down", gateway_down=True)
        text = self.run_screen(self.screen(runtime), [ESC], width=260).text()
        self.assertIn(
            "gateway is DOWN — served counts unknown; start it with " + cli.gateway_service_hint("start")
            + " (W: start it or read its log)",
            text,
        )
        self.assertNotIn("systemctl", text)
        pid = sorted(self.providers(runtime))[0]
        self.assertTrue(next(l for l in text.splitlines() if pid in l).find("unknown") >= 0)

    def test_config_drift_banner(self) -> None:
        runtime = self.make_runtime(self.tmp / "drift", served=())
        config_dir = proxy.config_dir(runtime.home)
        state.ensure_private_dir(config_dir)
        state.atomic_write(config_dir / "config.yaml", b"stale: true\n")
        screen = self.screen(runtime)
        text = self.run_screen(screen, [ESC]).text()
        self.assertTrue(screen.apply_offer)
        self.assertIn(cli_text.PROVIDERS_APPLY_BANNER, " ".join(text.split()))
        self.assertIn("P apply", text)

    def test_help_and_refresh(self) -> None:
        calls = []
        screen = self.screen(journal=lambda: calls.append(1) or None)
        win = self.run_screen(screen, ["?", ESC, "r", ESC])
        modal = next(f for f in win.frames if "providers — help" in f)
        self.assertIn(" ".join(cli.PROVIDERS_HELP.split()[:6]), modal)
        self.assertIn("Enter does the main thing for the selected provider",
                      " ".join(line.strip(" |") for line in modal.splitlines()))
        self.assertEqual(screen.message, "refreshed.")
        self.assertEqual(len(calls), 2, "the journal is read on open and once per R")

    def test_the_legacy_add_flows_are_gone(self) -> None:
        # N opens the picker of your own providers and A the models chooser;
        # Esc leaves both with nothing written to the legacy registry.
        screen = self.screen()
        before = copy.deepcopy(custom.load_registry(self.runtime.environ))
        win = self.run_screen(screen, ["N", ESC, "A", ESC, ESC])
        self.assertEqual(custom.load_registry(self.runtime.environ), before)
        self.assertTrue(any(cli_text.PICKER_TITLE_OTHER in f for f in win.frames))
        for name in ("_add_provider", "_add_models", "_prompt", "_cancelled"):
            self.assertFalse(hasattr(cli._ProvidersScreen, name), name)
        self.assertFalse(hasattr(cli, "_registry_id_for_wire"))
        self.assertIn("N adds your own endpoint", " ".join(cli.PROVIDERS_HELP.split()))

    def test_floor(self) -> None:
        self.assertFloor(self.screen)

    def test_served_header_fits_at_minimum_width(self) -> None:
        for management in (None, fx.quota_body()):
            with self.subTest(management=management is not None):
                runtime = self.make_runtime(self.tmp / ("quota" if management else "no-quota"),
                                            management=management)
                screen = self.screen(runtime)
                _, cols = screen.min_size(80)
                rows, _ = screen.min_size(cols)
                text = self.run_screen(screen, [ESC], height=rows, width=cols).text()
                header = next(line for line in text.splitlines() if "provider" in line and "served" in line)
                self.assertTrue(header.rstrip().endswith("route"), header)
                self.assertLess(header.index("served") + len("served"), cols)

    def test_runner_default_journal_seam(self) -> None:
        win = FakeWindow([ESC])
        with mock.patch('claude_multi.cli.gateway_facts._read_gateway_journal', return_value=None) as reader:
            cli.run_providers_screen(self.runtime, win, tui.MONO_PALETTE)
        reader.assert_called_once_with(self.runtime)


class ProvidersJournalSeamTests(_ScreenCase):
    """Under injected loopback seams nothing spawns ``journalctl``."""

    def test_facts_and_screen_never_run_a_subprocess(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("journalctl ran")) as run:
            facts = cli._provider_facts(self.runtime).facts
            screen = cli._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE)
            screen.run(FakeWindow(["r", ESC]))
        run.assert_not_called()
        self.assertTrue(facts)
        for fact in facts:
            self.assertTrue({"enabled", "models", "source", "selectors"} <= set(fact), fact)

    def test_journal_facts_render_the_doctor_lines_byte_identically(self) -> None:
        pool = _tui_render.first_oauth_pool(self.runtime)
        alias = "screens-alias"
        text = fx.journal_text(dead_pool=pool, quota=[(alias, "403", 1), ("quiet", "429", 3)])
        facts = cli._journal_facts(self.runtime, text)
        self.assertEqual(set(facts.dead_pools), {pool})
        self.assertEqual(facts.quota, {(alias, "403"): 1}, "429 below the hourly threshold is not a fact")
        report = cli._doctor_journal_report(self.runtime, text)
        self.assertEqual(len(report), 2)
        self.assertEqual(cli._journal_facts(self.runtime, None), cli.JournalFacts({}, None, {}))


# ================================================================ Settings


class SettingsScreenTests(_ScreenCase):
    def screen(self, runtime=None, *, lineup=False, tty_in=None, tty_out=None) -> cli._SettingsScreen:
        runtime = runtime or self.runtime
        return cli._SettingsScreen(
            runtime, tui.MONO_PALETTE,
            card_lineup=_tui_render.default_lineup(runtime) if lineup else None,
            tty_in=tty_in or io.StringIO(), tty_out=tty_out or io.StringIO(),
        )

    def to(self, screen, key: str) -> list[str]:
        """Down-steps from the first selectable row to the row ``key``."""

        selectable = [e[1].key for e in screen.entries if screen._selectable(e)]
        return ["j"] * selectable.index(key)

    def test_write_failures_keep_the_filesystem_remedy_visible(self) -> None:
        from claude_multi import choices
        from claude_multi.setup import defaults

        for number, (_what, remedy) in state.FILESYSTEM_FAILURES.items():
            for action in ("settings", "preference", "ceiling", "default", "environment"):
                with self.subTest(errno=number, action=action):
                    screen = self.screen()
                    exc = OSError(number, "fixture write failure", str(self.tmp / "root" / "settings.json"))
                    expected = state.failure_text(exc, self.runtime.environ)
                    with contextlib.ExitStack() as stack:
                        if action == "settings":
                            stack.enter_context(mock.patch.object(settings.SettingsStore, "update", side_effect=exc))
                            screen._set(screen._row(settings.EXPLORE_INHERIT_CAP_DISABLED_KEY), False, "off")
                        elif action == "preference":
                            stack.enter_context(mock.patch.object(settings.PreferencesStore, "update", side_effect=exc))
                            screen._cycle_preference(screen._row(settings.FEEDBACK_DRAFTS_KEY))
                        elif action == "ceiling":
                            stack.enter_context(mock.patch.object(choices, "update", side_effect=exc))
                            screen._say(screen._update_ceiling(400000), "warn")
                        elif action == "default":
                            stack.enter_context(mock.patch.object(defaults, "apply_default", side_effect=exc))
                            screen._set_default(None)
                        else:
                            stack.enter_context(mock.patch.object(choices, "set_session_env_keep", side_effect=exc))
                            screen._say(screen._save_env_keep(["MCP_TOOL_API_KEY"]), "warn")
                    self.assertEqual(screen.message, expected)
                    win = self.run_screen(screen, [ESC])
                    rendered = " ".join(win.text().split())
                    self.assertIn("fix: " + remedy, rendered)
                    self.assertNotIn("^J", rendered)
                    self.assertEqual(self.settings_doc(), {})
        # Failure leaves the screen usable for a subsequent successful save.
        screen._set(screen._row(settings.EXPLORE_INHERIT_CAP_DISABLED_KEY), False, "off")
        self.assertIn(settings.EXPLORE_INHERIT_CAP_DISABLED_KEY, self.settings_doc())

    def test_integer_write_failure_stays_in_the_modal_with_its_remedy(self) -> None:
        import errno

        screen = self.screen()
        exc = OSError(errno.ENOSPC, "fixture write failure")
        with mock.patch.object(settings.SettingsStore, "update", side_effect=exc):
            win = self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY)
                                  + [ENTER, *"80", ENTER, ENTER, ESC, ESC])
        lines = textwrap.wrap(state.failure_text(exc, self.runtime.environ), 60)
        self.assertTrue(any(all(line in frame for line in lines) for frame in win.frames))
        self.assertEqual(self.settings_doc(), {})

    def test_wrapped_and_committed_write_failures_keep_the_shared_text(self) -> None:
        import errno

        screen = self.screen()
        cause = OSError(errno.EROFS, "fixture write failure")
        wrapped = settings.SettingsError("fixture settings write failed")
        wrapped.__cause__ = cause
        committed = state.CommittedStateError(errno.ENOSPC, "the change was written but may not be durable yet")
        for exc, expected in ((wrapped, state.failure_text(cause, self.runtime.environ)),
                              (committed, state.failure_text(committed, self.runtime.environ)),
                              (settings.SettingsError("fixture validation failure"), "fixture validation failure")):
            with self.subTest(error=type(exc).__name__), \
                    mock.patch.object(settings.SettingsStore, "update", side_effect=exc):
                self.assertEqual(screen._update(lambda doc: None), expected)

    def test_every_row_and_detail(self) -> None:
        screen = self.screen(lineup=True)
        selectable = [e[1] for e in screen.entries if screen._selectable(e)]
        win = self.run_screen(screen, ["j"] * (len(selectable) - 1) + [ESC])
        self.assertIn(cli.SETTINGS_TITLE, win.frames[0])
        for section in views.SETTINGS_SECTIONS:
            self.assertIn("  " + section, win.frames[0])
        for index, row in enumerate(selectable):
            with self.subTest(row=row.key):
                self.assertIn(row.label, win.frames[index])
                self.assertIn(textwrap.wrap(row.detail, 76)[0], win.frames[index])
        lineup = _tui_render.default_lineup(self.runtime)
        self.assertIn(f"→ effective for {lineup.name} ({views.short_label(lineup.lead.binding.display)}): window",
                      win.frames[0])

    def test_kept_environment_row_edits_refuses_and_resets_names_only(self) -> None:
        from claude_multi import choices

        screen = self.screen()
        row = screen._row(views.SETTINGS_ENV_KEEP_KEY)
        self.assertEqual((row.section, row.label, row.value), (views.SETTINGS_SECTIONS[2], "kept environment", "none"))
        steps = [HOME] + self.to(screen, views.SETTINGS_ENV_KEEP_KEY)
        refused = self.run_screen(screen, steps + [ENTER, *"ANTHROPIC_API_KEY", ENTER, ENTER, ESC, ESC])
        self.assertEqual(list(choices.read(self.runtime.environ).get("session_env_keep")), [])
        self.assertTrue(any("reserved prefix" in frame for frame in refused.frames))
        self.run_screen(screen, steps + [ENTER, *"MCP_TOOL_API_KEY, OTHER_API_KEY", ENTER, ENTER, ESC])
        self.assertEqual(list(choices.read(self.runtime.environ).get("session_env_keep")),
                         ["MCP_TOOL_API_KEY", "OTHER_API_KEY"])
        row = screen._row(views.SETTINGS_ENV_KEEP_KEY)
        self.assertEqual((row.value, row.note), ("2 names", "MCP_TOOL_API_KEY, OTHER_API_KEY"))
        # R focuses Cancel first: Enter there keeps the names.
        self.run_screen(screen, steps + ["R", ENTER, ESC])
        self.assertEqual(len(choices.read(self.runtime.environ).get("session_env_keep")), 2)
        self.run_screen(screen, steps + ["R", LEFT, ENTER, ESC])
        self.assertEqual(list(choices.read(self.runtime.environ).get("session_env_keep")), [])
        self.assertEqual(screen._row(views.SETTINGS_ENV_KEEP_KEY).value, "none")

    def test_kept_environment_row_is_read_only_without_write_access(self) -> None:
        screen = self.screen()
        steps = self.to(screen, views.SETTINGS_ENV_KEEP_KEY)
        self.runtime.allow_state_writes = False
        win = self.run_screen(screen, steps + [ENTER, ESC])
        self.assertTrue(any(cli_text.SETTINGS_READ_ONLY_COMMAND in frame for frame in win.frames))
        self.assertTrue(any(cli_text.SETTINGS_READ_ONLY in frame for frame in win.frames))

    def test_without_a_card_lineup(self) -> None:
        text = self.run_screen(self.screen(), [ESC]).text()
        self.assertIn("→ effective: open from the card to see a profile's numbers", text)

    def test_compaction_edit_recomputes_the_effective_row(self) -> None:
        screen = self.screen(lineup=True)
        value = settings.COMPACTION_PERCENT_DEFAULT - 5
        before = screen._row("effective").label
        self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY) + [ENTER, *str(value), ENTER, ENTER, ESC])
        self.assertEqual(self.settings_doc()[settings.COMPACTION_PERCENT_KEY], value)
        self.assertEqual(
            screen.message,
            f'saved auto-compact at = {value} — applies at the next launch or resume; running sessions '
            'show "settings changed"',
        )
        lineup = _tui_render.default_lineup(self.runtime)
        ctx = views.effective_context(self.runtime, lineup, self.runtime.current_effective()).as_document()
        after = screen._row("effective").label
        self.assertNotEqual(after, before, "sc[6]: recomputed after the write")
        self.assertTrue(after.endswith(f"compacts at ~{profile.format_tokens(ctx['trigger'])}"), after)
        self.assertEqual(screen._row(settings.COMPACTION_PERCENT_KEY).value, f"{value} %")

    def test_int_modal_starts_empty_and_names_current_default_range(self) -> None:
        screen = self.screen()
        win = self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY) + [ENTER, ESC, ESC])
        modal = next(f for f in win.frames if "current " in f)
        self.assertIn(
            f"current {settings.COMPACTION_PERCENT_DEFAULT} · default {settings.COMPACTION_PERCENT_DEFAULT} · "
            f"range {settings.COMPACTION_PERCENT_MIN}–{settings.COMPACTION_PERCENT_MAX}",
            modal,
        )
        self.assertIn("[" + " " * 10, modal, "the input starts empty (cf[9])")
        self.assertEqual(self.settings_doc(), {})

    def test_out_of_bounds_keeps_the_modal_open_with_the_error(self) -> None:
        cases = (
            (settings.COMPACTION_PERCENT_KEY, settings.COMPACTION_PERCENT_MIN - 1, "below minimum"),
            (settings.COMPACTION_PERCENT_KEY, settings.COMPACTION_PERCENT_MAX + 1, "above maximum"),
            (settings.REVIEW_ROUND_CAP_KEY, settings.REVIEW_ROUND_CAP_MIN - 1, "below minimum"),
            (settings.REVIEW_ROUND_CAP_KEY, settings.REVIEW_ROUND_CAP_MAX + 1, "above maximum"),
        )
        for key, value, words in cases:
            screen = self.screen()
            with self.subTest(key=key, value=value):
                win = self.run_screen(screen, self.to(screen, key) + [ENTER, *str(value), ENTER, ENTER, ESC, ESC])
                error = f"operator settings invalid: $.{key}: value {value} {words}"
                lines = textwrap.wrap(error, 60)
                self.assertTrue(any(all(line in f for line in lines) for f in win.frames), error)
                self.assertEqual(self.settings_doc(), {})

    def test_toggle_explore(self) -> None:
        screen = self.screen()
        key = settings.EXPLORE_INHERIT_CAP_DISABLED_KEY
        self.run_screen(screen, self.to(screen, key) + [ENTER, ESC])
        value = not settings.EXPLORE_INHERIT_CAP_DISABLED_DEFAULT
        self.assertEqual(self.settings_doc()[key], value)
        self.assertTrue(screen.message.startswith(
            f"saved Explore follows lead model = {'on' if value else 'off'} — applies at the next launch"
        ))

    def test_review_rounds_arrows_and_reset(self) -> None:
        screen = self.screen()
        key = settings.REVIEW_ROUND_CAP_KEY
        steps = self.to(screen, key)
        self.run_screen(screen, steps + [RIGHT, ESC])
        expected = min(settings.REVIEW_ROUND_CAP_MAX, settings.REVIEW_ROUND_CAP_DEFAULT + 1)
        self.assertEqual(self.settings_doc().get(key, settings.REVIEW_ROUND_CAP_DEFAULT), expected)
        screen = self.screen()
        win = self.run_screen(screen, steps + ["R", LEFT, ENTER, ESC])
        self.assertTrue(any("Reset review rounds (max) to its default?" in f for f in win.frames))
        self.assertNotIn(key, self.settings_doc())
        self.assertTrue(screen.message.startswith("reset review rounds (max) to its default"))

    def test_feedback_drafts_preference_cycles_resets_and_never_touches_settings(self) -> None:
        # A host preference in preferences.json, not settings.json.
        screen = self.screen()
        key = settings.FEEDBACK_DRAFTS_KEY
        store = self.runtime.preferences_store
        self.assertEqual(screen._row(key).value, "off")
        self.run_screen(screen, self.to(screen, key) + [ENTER, ESC])
        self.assertEqual(store.load(), {key: "notify"})
        self.assertTrue(screen.message.startswith("saved feedback drafts = notify — applies at the next launch"))
        self.assertEqual(self.settings_doc(), {})
        screen = self.screen()
        self.assertEqual(screen._row(key).value, "notify")
        win = self.run_screen(screen, self.to(screen, key) + ["R", LEFT, ENTER, ESC])
        self.assertTrue(any("Reset feedback drafts to its default?" in f for f in win.frames))
        self.assertEqual(store.load(), {})
        self.assertEqual(self.settings_doc(), {})

    def test_footer_follows_the_selected_row_with_the_handler_predicates(self) -> None:
        # The footer advertises exactly what the handlers accept.
        edit = tuple(cli_text.SETTINGS_KEYBAR)
        cases = {
            settings.COMPACTION_PERCENT_KEY: edit, settings.FEEDBACK_DRAFTS_KEY: edit,
            settings.REVIEW_ROUND_CAP_KEY: edit, "providers": cli_text.SETTINGS_KEYBAR_OPEN,
            "admitted": cli_text.SETTINGS_KEYBAR_OPEN, "token": cli_text.SETTINGS_KEYBAR_ROTATE,
            "ceiling": edit,
            "reserve": (("read-only:", cli_text.SETTINGS_READ_ONLY_EVIDENCE), ("?", "help"), ("Esc", "back")),
        }
        for writes in (True, False):
            self.runtime.allow_state_writes = writes
            screen = self.screen()
            for key, expected in cases.items():
                with self.subTest(key=key, writes=writes):
                    screen.index = next(i for i, (kind, value) in enumerate(screen.entries)
                                        if kind == "row" and value.key == key)
                    bindings = screen.keybar_bindings()
                    row = screen.selected_row
                    advertised = ("R", "reset to default") in bindings
                    self.assertEqual(advertised, screen._can_edit(row))
                    if writes or expected in (cli_text.SETTINGS_KEYBAR_OPEN, cases["reserve"]):
                        self.assertEqual(bindings, expected)
                    else:
                        self.assertEqual(bindings[0], ("read-only:", cli_text.SETTINGS_READ_ONLY_COMMAND))
        self.runtime.allow_state_writes = True

    def test_reset_on_a_read_only_row(self) -> None:
        screen = self.screen()
        self.run_screen(screen, self.to(screen, "reserve") + ["R", ESC])
        self.assertEqual(screen.message, "output reserve is not editable here")

    def test_workflow_picker(self) -> None:
        screen = self.screen()
        key = settings.WORKFLOW_DEFAULT_BINDING_KEY
        steps = self.to(screen, key)
        lcat = self.runtime.lineup_catalog()
        rows = views.line_rows(lcat, self.runtime.current_effective(), custom_ids=frozenset())
        picker = views.picker_rows(rows, slot="workflow", bindings={}, lcat=lcat,
                                   eff=self.runtime.current_effective(), current=None)
        win = self.run_screen(screen, steps + [ENTER, ESC, ESC])
        frame = next(f for f in win.frames if picker.title in f)
        self.assertEqual(picker.items[0].kind, "off")
        self.assertIn("  › off", frame, "the BindingPicker")
        self.assertNotIn(profile.ULTRACODE, frame)
        line_items = [i for i in picker.items if i.kind == "line"]
        self.assertEqual({i.key for i in line_items}, {r.key for r in rows}, "all valid declarations stay visible")
        for item in line_items:
            row = next(r for r in rows if r.key == item.key)
            if row.mode == "client":
                self.assertEqual(item.efforts, (), "client-effort rows run at default_effort only")
                self.assertIn(f"efforts {row.default_effort}", item.text)
        # Choose a gateway-effort line and its last effort.
        target = next(i for i in line_items if len(i.efforts) > 1 and i.selectable)
        index = picker.items.index(target)
        selectable_before = sum(1 for i in picker.items[1:index] if i.selectable)
        effort = target.efforts[-1]
        effort_steps = [RIGHT] * (target.efforts.index(effort) - target.efforts.index(target.initial_effort))
        screen = self.screen()
        self.run_screen(screen, steps + [ENTER] + ["j"] * (selectable_before + 1) + effort_steps + [ENTER, ESC])
        self.assertEqual(self.settings_doc()[key], {"model": target.key, "effort": effort})
        self.assertTrue(screen.message.startswith("saved workflow default binding = "), screen.message)
        # "off" clears it.
        screen = self.screen()
        self.run_screen(screen, steps + [ENTER, HOME, ENTER, ESC])
        self.assertNotIn(key, self.settings_doc())
        self.assertTrue(screen.message.startswith("saved workflow default binding = off"))

    def test_token_rotation_runs_on_the_screen_streams(self) -> None:
        tty_in, tty_out = io.StringIO("\n"), io.StringIO()
        screen = self.screen(tty_in=tty_in, tty_out=tty_out)
        calls = []

        def rotate(runtime, **kwargs):
            calls.append((runtime, kwargs))
            return 0

        with mock.patch('claude_multi.cli.doctor_actions._doctor_rotate_token', side_effect=rotate):
            win = self.run_screen(screen, self.to(screen, "token") + [ENTER, LEFT, ENTER, ESC])
        self.assertTrue(any(cli.SETTINGS_TOKEN_TITLE in f for f in win.frames))
        self.assertEqual(
            calls, [(self.runtime, {"input_stream": tty_in, "output_stream": tty_out, "interactive": True})]
        )
        self.assertIn("Press Enter to return", tty_out.getvalue())
        self.assertEqual(screen.message, "token rotation finished")

    def test_token_rotation_cancel_runs_nothing(self) -> None:
        screen = self.screen()
        with mock.patch('claude_multi.cli.doctor_actions._doctor_rotate_token') as rotate:
            self.run_screen(screen, self.to(screen, "token") + [ENTER, ESC, ESC])
        rotate.assert_not_called()

    def test_corrupt_settings_are_read_only(self) -> None:
        path = _write_settings(self.runtime, b"{")
        screen = self.screen()
        win = self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY) + [ENTER, ESC])
        with self.assertRaises(settings.SettingsError) as caught:
            self.runtime.settings_store.load()
        self.assertEqual(screen.banner, f"settings.json is invalid: {caught.exception} — fix or remove {path}")
        self.assertIn("  settings.json is invalid: ", win.text())
        self.assertEqual(screen.message, "auto-compact at is not editable here")
        self.assertEqual(path.read_bytes(), b"{")

    def test_state_writes_refused(self) -> None:
        self.runtime.allow_state_writes = False
        screen = self.screen()
        self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY) + [ENTER, ESC])
        self.assertEqual(screen.message, cli.SETTINGS_READ_ONLY)
        self.assertEqual(self.settings_doc(), {})

    def test_diamond_lists_the_overriding_profiles(self) -> None:
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "screens-override"
        value = settings.COMPACTION_PERCENT_DEFAULT - 10
        document["settings_overrides"] = {settings.COMPACTION_PERCENT_KEY: value}
        self.runtime.profiles.new(document)
        screen = self.screen()
        win = self.run_screen(screen, self.to(screen, settings.COMPACTION_PERCENT_KEY) + [ESC], width=120)
        row = screen._row(settings.COMPACTION_PERCENT_KEY)
        self.assertEqual(row.value, f"{settings.COMPACTION_PERCENT_DEFAULT} % ◆")
        self.assertIn(f"◆ overridden by: screens-override ({value} %)", win.text().replace("\n  ", " "))

    def test_g_and_m_open_the_screens(self) -> None:
        opened = []
        with mock.patch('claude_multi.cli.screens.providers._ProvidersScreen.run', lambda _s, _w: opened.append("providers")), \
                mock.patch('claude_multi.cli.screens.models._ModelsScreen.run', lambda _s, _w: opened.append("models")):
            screen = self.screen()
            self.run_screen(screen, ["G", "m", *self.to(screen, "providers"), ENTER, "j", ENTER, ESC])
        self.assertEqual(opened, ["providers", "models", "providers", "models"])

    def test_help(self) -> None:
        win = self.run_screen(self.screen(), ["?", ESC, ESC])
        modal = next(f for f in win.frames if "settings — help" in f)
        self.assertIn(" ".join(cli.SETTINGS_HELP.split()[:6]), modal)
        self.assertIn("Provider keys are never", modal)

    def test_floor(self) -> None:
        self.assertFloor(self.screen)

    def test_runner_passes_the_streams(self) -> None:
        tty_in, tty_out = io.StringIO(), io.StringIO()
        with mock.patch('claude_multi.cli.screens.settings._SettingsScreen') as screen:
            cli.run_settings_screen(self.runtime, "win", tui.MONO_PALETTE, tty_in=tty_in, tty_out=tty_out)
        screen.assert_called_once_with(
            self.runtime, tui.MONO_PALETTE, card_lineup=None, tty_in=tty_in, tty_out=tty_out
        )
        screen.return_value.run.assert_called_once_with("win")





class ProviderQuotaScreenTests(_ScreenCase):
    runtime_kwargs = {"management": fx.quota_body(count=2, percent=18, short_percent=42, age=timedelta(minutes=12))}

    def screen(self, **kwargs):
        return cli._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE,
                                    now=fx.FIXED_NOW, tz=timezone.utc, **kwargs)

    def test_visible_age_and_refresh_failure_remedy_at_80_columns(self):
        pool_name = _tui_render.first_oauth_pool(self.runtime)
        screen = self.screen(journal=lambda: fx.journal_text(dead_pool=pool_name))
        text = self.run_screen(screen, [ESC]).text()
        row = next(r for r in screen.rows if r.kind == "oauth-pool")
        self.assertEqual(row.cells[2], "sign-in failing")
        drawn = next(line for line in text.splitlines() if line.startswith("  › "))
        self.assertIn("12m", drawn)
        self.assertIn(views.SIGNIN_REMEDY, text)
        self.assertIn("+1 more (claude-multi quota)", text)
        self.assertIn("quota", text)
        self.assertEqual(len(self.runtime.management_calls), 1)

    def test_cache_ttl_and_nonrefresh_actions(self):
        screen = self.screen()
        self.run_screen(screen, ["R", "R", ESC])
        self.assertEqual(len(self.runtime.management_calls), 1)
        self.runtime.pool_clock = lambda: 61.0
        screen._toggle()
        self.assertEqual(len(self.runtime.management_calls), 1, "Settings toggle is not a quota read event")
        screen._refresh()
        self.assertEqual(len(self.runtime.management_calls), 2)

    def test_down_and_foreign_owner_never_request_management(self):
        for label, kwargs in (("down", {"gateway_down": True}), ("foreign", {})):
            runtime = self.make_runtime(self.tmp / label, management=fx.quota_body(), **kwargs)
            if label == "foreign":
                runtime.listener_owner = lambda _base: claude_multi.service.OwnerVerdict("foreign", "fixture foreign")
            screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE)
            text = self.run_screen(screen, [ESC]).text()
            self.assertEqual(runtime.management_calls, [])
            self.assertEqual(screen.pool.state, "down")
            if label == "foreign":
                self.assertIn("Attention:", text)

    def test_quota_is_unavailable_off_the_management_channel_without_a_remedy(self):
        # A bundle or source install: even a selected key is never read or
        # sent, and the screen offers no command that could not help.
        runtime = self.make_runtime(self.tmp / "bundle", management=fx.quota_body(),
                                    environ_extra={claude_multi.management.CHANNEL_ENV: "bundle"})
        screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE)
        self.assertEqual(screen.pool.state, "unavailable")
        self.assertEqual(runtime.management_calls, [])
        pool_rows = [index for index, row in enumerate(screen.rows) if row.kind == "oauth-pool"]
        self.assertTrue(pool_rows)
        for index in pool_rows:
            screen.selected = index
            text = self.run_screen(screen, [ESC]).text()
            self.assertIn("quota unavailable in this build", text)
            self.assertNotIn("claude-multi-proxy init", text)
            self.assertNotIn("claude-multi quota —", text)

    def test_no_management_key_keeps_signin_and_full_settings_guidance(self):
        # Inject the management transport without prepare_start: unlike the
        # default fixture's seam state, production without a key is no-key.
        runtime = self.make_runtime(self.tmp / "no-key")
        runtime.environ[claude_multi.management.CHANNEL_ENV] = claude_multi.management.MANAGEMENT_CHANNEL
        runtime.management_callback = mock.Mock(side_effect=AssertionError("no key must not make a request"))
        screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE)
        self.assertEqual(screen.pool.state, "no-key")
        self.assertFalse((runtime.home / ".config" / "claude-multi" / "management-key").exists())
        for index, row in enumerate(screen.rows):
            if row.kind != "oauth-pool":
                continue
            with self.subTest(provider=row.id):
                screen.selected = index
                text = self.run_screen(screen, [ESC]).text()
                self.assertEqual(row.cells[2], "not signed in")
                self.assertIn(views.SIGNIN_GUIDANCE, text)
                self.assertIn("quota off — no management key", text)
                self.assertNotIn("quota no-key", text)
                screen._toggle()
                text = self.run_screen(screen, [ESC]).text()
                self.assertIn("off in Settings: new sessions exclude it; running sessions keep their routes", text)
                screen._toggle()
        runtime.management_callback.assert_not_called()

    def test_an_unproven_owner_gets_no_token_and_its_attention_appears_once(self):
        # Another process of yours on the gateway port: the served read and
        # the quota read never send it the gateway's token.
        self.runtime.listener_owner = lambda _base: claude_multi.service.OwnerVerdict("unknown", "fixture unverified owner")
        screen = self.screen()
        self.assertEqual(self.runtime.management_calls, [])
        self.assertTrue(self.runtime.gateway_attention)
        self.assertIn("not proven to be the claude-multi gateway", self.runtime.gateway_attention[0])
        text = self.run_screen(screen, [ESC]).text()
        self.assertEqual(text.count("fixture unverified owner"), 1)

    def test_long_names_multiple_windows_do_not_clip_age(self):
        screen = self.screen()
        facts = [dict(f, id="fixture-provider-with-a-long-name" if f["kind"] == "oauth-pool" else f["id"])
                 for f in screen.facts]
        # Use a long safe provider label without modifying the catalog. Its
        # pool still comes from the same cached provider fact.
        screen.facts = facts
        screen.rows = screen._rows()
        text = self.run_screen(screen, [ESC]).text()
        self.assertIn("12m", next(line for line in text.splitlines() if line.startswith("  › ")))
        self.assertIn("+1 more (claude-multi quota)", text)

    def test_management_failure_detail_keeps_readonly_remedy_visible(self):
        for code in (401, 403, 404, 500):
            runtime = self.make_runtime(self.tmp / str(code), management=code)
            screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE)
            text = self.run_screen(screen, [ESC]).text()
            self.assertIn("claude-multi quota", text)
            self.assertEqual(len(runtime.management_calls), 1)
            self.run_screen(screen, ["R", ESC])
            self.assertEqual(len(runtime.management_calls), 1)



class OnboardingHelpTextTests(unittest.TestCase):
    """Each 3.1 help states its keys (case-sensitive), the CLI
    equivalents and the relaunch class of T2 agents."""

    def test_models_help_describes_the_31_screen(self) -> None:
        text = " ".join(cli_text.MODELS_HELP.split())
        for needle in ("Q runs optional diagnostics", "E edits its declaration", "V shows details",
                       "candidates counts registry", "optional local admission badge", "default-No consent",
                       "X removes a model you added; where profiles use it you choose a replacement first.",
                       "Esc → G (Providers) shows its remedy.",
                       "CLI: models admit|revoke|edit|rm KEY; models qualify KEY --agents"):
            self.assertIn(needle, text)
        self.assertNotIn("custom models (direct only) follow", text)

    def test_providers_help_states_case_and_the_guarded_key_import(self) -> None:
        text = " ".join(cli_text.PROVIDERS_HELP.split())
        self.assertNotIn("case-sensitive", text)
        self.assertNotIn("(claude-multi providers approve ID)", " ".join(cli_text.MODELS_HELP.split()))
        self.assertIn("K sets or replaces an API key (typed masked; saving reloads the gateway; nothing is sent "
                      "to the provider)", text)
        self.assertIn("Changing credentials needs a terminal outside Claude Code.", text)

    def test_lineup_and_picker_help(self) -> None:
        self.assertIn("A model you added (◇) can change LIVE when its selector is already in the proven launch fence",
                      " ".join(cli_text.LINEUP_DIALOG_HELP.split()))
        picker = " ".join(tui.BINDING_PICKER_HELP.split())
        self.assertIn("All valid lines appear, including New, legacy custom and operator models (◇)", picker)
        self.assertIn("Admission badges, qualification and role recommendations do not block a binding", picker)
        self.assertIn("unusable routes are dimmed with remedies", picker)
        self.assertNotIn("Only catalog models", picker)


if __name__ == "__main__":
    unittest.main()
