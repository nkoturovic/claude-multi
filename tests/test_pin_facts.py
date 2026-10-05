"""Pin facts: the owned copy's state, the user's own claude, staleness, the card row."""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import pin, views
from claude_multi.cli.screens import launch_sessions
from _v4 import V4Case


def _contract(version="2.1.300", *, size=3, verified_at="2026-09-01", keys=None):
    entry = {
        "version": version,
        "platforms": {"linux-x64": {"sha256": "a" * 64, "size": size}},
        "manifest_sha256": "1" * 64, "signature_sha256": None,
        "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
        "verified_at": verified_at,
        "evidence": {"linux-x64": "battery", "others": "identity+smoke", "receipt_sha256": "2" * 64},
    }
    if keys is not None:
        entry["settings_keys"] = keys
    return {"version": 2, "verified": [entry]}


class PinFactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-pin-facts-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = {"HOME": str(self.root / "home")}

    def owned(self, data=b"abc", mode=0o755):
        path = pin.owned_path(self.env, "2.1.300", "linux-x64")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_copy_state_is_metadata_only(self) -> None:
        contract = _contract()
        state, text = pin.copy_state(contract, self.env, platform="linux-x64")
        self.assertEqual(state, "not set up")
        self.assertIn("claude-multi setup --step claude", text)
        retained = self.root / "retained"
        retained.mkdir()
        (retained / "2.1.300").write_bytes(b"xyz")
        self.assertEqual(pin.copy_state(contract, self.env, platform="linux-x64", retained_root=retained),
                         ("pending", None))
        self.owned()  # the size matches; the hash is the launch's job
        self.assertEqual(pin.copy_state(contract, self.env, platform="linux-x64"), ("ready", None))
        self.owned(b"abcd")
        self.assertEqual(pin.copy_state(contract, self.env, platform="linux-x64")[0], "damaged")
        self.owned(b"abc", 0o644)
        self.assertEqual(pin.copy_state(contract, self.env, platform="linux-x64")[0], "damaged")
        self.assertEqual(pin.copy_state(contract, self.env, platform="darwin-arm64")[0], "no build")

    def test_version_from_path_never_runs_the_file(self) -> None:
        self.assertEqual(pin.version_from_path("/x/claude/versions/2.1.290"), "2.1.290")
        self.assertEqual(pin.version_from_path("/x/claude-multi/claude/2.1.286/claude"), "2.1.286")
        self.assertEqual(pin.version_from_path("/x/claude-multi/claude/2.1.286/claude.exe"), "2.1.286")
        self.assertIsNone(pin.version_from_path("/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"))
        self.assertIsNone(pin.version_from_path("/usr/local/bin/claude"))

    def test_installed_client_resolves_the_real_path(self) -> None:
        versions = self.root / "versions"
        versions.mkdir()
        build = versions / "2.1.290"
        build.write_bytes(b"never run")
        build.chmod(0o755)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").symlink_to(build)
        client = pin.installed_client({"PATH": str(bin_dir)})
        self.assertEqual((client.path, client.real, client.version), (bin_dir / "claude", build, "2.1.290"))
        self.assertIsNone(pin.installed_client({"PATH": str(self.root / "empty")}))
        self.assertIsNone(pin.installed_client({}))

    def test_staleness(self) -> None:
        contract = _contract(verified_at="2026-09-01")
        day = datetime.date(2026, 9, 20)
        self.assertIsNone(pin.staleness(contract, day, "2.1.300"))
        self.assertIsNone(pin.staleness(contract, day, "2.1.250"))
        self.assertIsNone(pin.staleness(contract, day, None))
        self.assertIn("your Claude Code is 2.1.301", pin.staleness(contract, day, "2.1.301"))
        late = pin.staleness(contract, datetime.date(2026, 10, 2), None)
        self.assertIn("it was verified 31 days ago", late)
        self.assertIn("a claude-multi release that verifies a newer Claude Code brings it", late)
        self.assertIsNone(pin.staleness(contract, datetime.date(2026, 10, 1), None))

    def test_settings_keys(self) -> None:
        self.assertIsNone(pin.settings_keys(_contract()))
        self.assertEqual(pin.settings_keys(_contract(keys=["env", "model"])), frozenset({"env", "model"}))
        self.assertIsNone(pin.settings_keys({"verified": "broken"}))


class ClaudeCardRowTests(V4Case):
    def test_the_card_row_names_the_pin_its_evidence_and_the_users_claude(self) -> None:
        runtime = self.make_runtime(pin_report=True)
        text, problem = launch_sessions._claude_card_row(runtime)
        self.assertIsNone(problem)
        self.assertEqual(text, f"Claude Code 2.1.281 · battery evidence on {self.platform}")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        build = self.root / "versions" / "2.1.290"
        build.parent.mkdir()
        build.write_bytes(b"x")
        build.chmod(0o755)
        (bin_dir / "claude").symlink_to(build)
        runtime.environ["PATH"] = str(bin_dir)
        self.assertTrue(launch_sessions._claude_card_row(runtime)[0].endswith(" · your claude 2.1.290"))

    def test_a_missing_copy_blocks_the_card(self) -> None:
        self.fake_binary.unlink()
        runtime = self.make_runtime(pin_report=True)
        _text, problem = launch_sessions._claude_card_row(runtime)
        self.assertEqual(problem, pin.not_set_up_text("2.1.281"))
        card = views.card_model(target=self.profile_target(), lineup=None, errors=[problem],
                                claude_row=_text)
        self.assertFalse(card.ready)
        self.assertIn(("claude", _text), [(row.kind, row.text) for row in card.rows])

    def test_test_seams_turn_the_row_off(self) -> None:
        self.assertEqual(launch_sessions._claude_card_row(self.runtime), (None, None))
        self.assertIsNone(launch_sessions._claude_card_staleness(self.runtime))

    def card_texts(self, runtime, today: datetime.date) -> list[str]:
        from claude_multi import cli, tui
        import io

        now = datetime.datetime.combine(today, datetime.time(12), tzinfo=datetime.timezone.utc)
        with mock.patch("claude_multi.cli.gateway_facts._doctor_now", return_value=now):
            screen = cli._LaunchCardScreen(
                runtime, self.profile_target(), passthrough=[], palette=tui.MONO_PALETTE,
                gateway_checked=True, gateway_check=lambda: None, hint_detector=lambda _contract: None,
                tty_in=io.StringIO(), tty_out=io.StringIO())
            _prepared, card = screen._plan()
        return [row.text for row in card.rows if row.role == "warn"]

    def test_the_card_names_a_stale_pin_without_a_command(self) -> None:
        runtime = self.make_runtime(pin_report=True)
        fresh = self.card_texts(runtime, datetime.date(2026, 9, 26))
        self.assertFalse([text for text in fresh if "this claude-multi runs Claude Code" in text])
        stale = [text for text in self.card_texts(runtime, datetime.date(2026, 11, 26))
                 if "this claude-multi runs Claude Code" in text]
        self.assertEqual(len(stale), 1)
        self.assertIn("it was verified 62 days ago", stale[0])
        self.assertNotIn("claude-multi update", stale[0])
        # A newer installed claude is an Attention on the card too.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        build = self.root / "versions" / "2.1.290"
        build.parent.mkdir()
        build.write_bytes(b"x")
        build.chmod(0o755)
        (bin_dir / "claude").symlink_to(build)
        runtime.environ["PATH"] = str(bin_dir)
        newer = [text for text in self.card_texts(runtime, datetime.date(2026, 9, 26))
                 if "your Claude Code is 2.1.290" in text]
        self.assertEqual(len(newer), 1)


if __name__ == "__main__":
    unittest.main()
