"""The TUI action table against the screens' bars, in both directions."""

from __future__ import annotations

import re
import unittest

from claude_multi import tui, views
import claude_multi.cli.screens.actions as actions
import claude_multi.cli.screens.profiles as screens_profiles
import claude_multi.cli.text as cli_text


def _keys(*bars) -> set[str]:
    return {key for bar in bars for key, _label in (bar if isinstance(bar[0], tuple) else (bar,))}


# Every bar a screen can draw (the row-dependent bindings included).
BARS = {
    "card": _keys(views.CARD_KEYS_FRESH, views.CARD_KEYS_NOT_CONNECTED, ("U", "update")),
    "resume-card": _keys((("Enter", "resume"),), views.CARD_KEYS_RESUME, ("U", "update")),
    "sessions": _keys(cli_text.SESSIONS_KEYBAR, cli_text.SESSIONS_KEYBAR_ALL, cli_text.SESSIONS_STOP_BINDING,
                      cli_text.SESSIONS_MARK_BINDING, cli_text.SESSIONS_REPAIR_BINDING),
    "lineup-dialog": _keys(cli_text.LINEUP_DIALOG_KEYBAR),
    "direct": _keys(cli_text.DIRECT_KEYBAR, cli_text.DIRECT_EFFORT_BINDING, cli_text.DIRECT_GATEWAY_BINDING),
    "providers": _keys(cli_text.PROVIDERS_KEYBAR, cli_text.PROVIDERS_KEYBAR_KEY_MISSING,
                       cli_text.PROVIDERS_KEYBAR_KEY_SET, cli_text.PROVIDERS_KEYBAR_ACCOUNT,
                       cli_text.PROVIDERS_KEYBAR_ACCOUNT_KEY,
                       cli_text.PROVIDERS_KEYBAR_OWN, cli_text.PROVIDERS_KEYBAR_OWN_KEYLESS,
                       cli_text.PROVIDERS_KEYBAR_KEYLESS, cli_text.PROVIDERS_APPLY_BINDING,
                       cli_text.PROVIDERS_GATEWAY_BINDING),
    "models": _keys(cli_text.MODELS_KEYBAR, cli_text.MODELS_KEYBAR_NEW, cli_text.MODELS_KEYBAR_ADMITTED),
    "settings": _keys(cli_text.SETTINGS_KEYBAR, cli_text.SETTINGS_KEYBAR_OPEN, cli_text.SETTINGS_KEYBAR_ROTATE),
    "profiles": _keys(cli_text.PROFILES_KEYBAR_YOURS, cli_text.PROFILES_KEYBAR_SEED,
                      cli_text.PROFILES_KEYBAR_UNLOADABLE, cli_text.PROFILES_RESTORE_BINDING,
                      screens_profiles.keybar(None, fallback_only=False)),
    "get-started": _keys(cli_text.GS_KEYBAR),
    "editor": _keys(tui.PROFILE_EDITOR_KEYBAR),
    "binding-picker": _keys(tui.BINDING_PICKER_KEYBAR),
    "named-bindings": _keys(tui.NAMED_BINDINGS_KEYBAR),
}


class ActionTableTests(unittest.TestCase):
    def test_every_bar_key_has_an_action_and_every_action_is_on_a_bar(self) -> None:
        self.assertEqual(set(actions.ACTIONS), set(BARS))
        for screen, rows in actions.ACTIONS.items():
            with self.subTest(screen=screen):
                self.assertEqual({row.key for row in rows}, BARS[screen])

    def test_ids_are_stable_names(self) -> None:
        seen: set[str] = set()
        for screen, rows in actions.ACTIONS.items():
            keys = [row.key for row in rows]
            self.assertEqual(len(keys), len(set(keys)), screen)
            for row in rows:
                self.assertRegex(row.id, rf"^{re.escape(screen)}\.[a-z][a-z-]*$")
                self.assertNotIn(row.id, seen)
                seen.add(row.id)
                self.assertTrue(row.does)
                if row.command is not None:
                    self.assertTrue(row.command.startswith("claude-multi"), row.id)
        self.assertEqual(actions.action("card.doctor").key, "H")
        for missing in ("card.nothing", "nowhere.quit"):
            with self.assertRaises(KeyError):
                actions.action(missing)

    def test_arrow_keys_are_named_one_way(self) -> None:
        for bar_keys in BARS.values():
            self.assertNotIn("Left/Right", bar_keys)


if __name__ == "__main__":
    unittest.main()
