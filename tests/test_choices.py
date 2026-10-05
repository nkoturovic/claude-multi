"""``choices.json``: the closed choices document next to ``settings.json``."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path

from _layout import RESOURCES_ROOT
from claude_multi import choices, settings, state


class ChoicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-choices-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home)}

    def test_defaults_without_a_file(self) -> None:
        current = choices.read(self.env)
        self.assertIsNone(current.get("default_profile"))
        self.assertEqual(current.get("session_env_keep"), [])
        with self.assertRaises(KeyError):
            current.get("unknown")
        self.assertFalse(choices.path(self.env).exists())

    def test_update_writes_a_closed_private_document(self) -> None:
        result = choices.update(self.env, default_profile="balanced", session_env_keep=["EXAMPLE_TOOL_API_KEY"])
        path = choices.path(self.env)
        self.assertEqual(path, self.home / ".config/claude-multi/choices.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "default_profile": "balanced",
                                                        "session_env_keep": ["EXAMPLE_TOOL_API_KEY"]})
        self.assertEqual(choices.read(self.env), result)
        # The default value removes the key; the other key stays.
        choices.update(self.env, default_profile=None)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "session_env_keep": ["EXAMPLE_TOOL_API_KEY"]})

    def test_the_config_root_follows_xdg_config_home(self) -> None:
        env = {**self.env, "XDG_CONFIG_HOME": str(self.root / "xdg")}
        choices.update(env, default_profile="quality")
        self.assertTrue((self.root / "xdg/claude-multi/choices.json").is_file())
        self.assertEqual(choices.path(env).parent, settings.settings_path(env).parent)

    def test_refused_values_and_documents(self) -> None:
        for key, value in (("default_profile", "Bad Name"), ("default_profile", 3),
                           ("session_env_keep", "PATH"), ("session_env_keep", ["lower"]),
                           ("session_env_keep", ["A", "A"]), ("session_env_keep", ["A"] * 65)):
            with self.subTest(key=key, value=value), self.assertRaises(choices.ChoicesError):
                choices.update(self.env, **{key: value})
        with self.assertRaises(choices.ChoicesError):
            choices.update(self.env, theme="dark")
        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        for document in ({"version": 2}, {"version": 1, "theme": "dark"}, [], {"version": True}):
            state.atomic_write(path, json.dumps(document).encode())
            with self.subTest(document=document), self.assertRaises(choices.ChoicesError) as caught:
                choices.read(self.env)
            self.assertIn("choices.json", caught.exception.remedy)
        state.atomic_write(path, b"{not json")
        with self.assertRaises(choices.ChoicesError):
            choices.read(self.env)

    def test_documents_older_releases_read_never_carry_choices(self) -> None:
        choices.update(self.env, default_profile="balanced")
        self.assertFalse(settings.settings_path(self.env).exists())
        self.assertFalse(settings.preferences_path(self.env).exists())
        # settings.json and preferences.json stay closed under their own
        # schemas, which know none of these keys.
        for name in ("settings.schema.json", "preferences.schema.json"):
            schema = json.loads((RESOURCES_ROOT / "schemas" / name).read_text())
            self.assertIs(schema.get("additionalProperties"), False, name)
            self.assertFalse(set(choices.FIELDS) & set(schema.get("properties", {})), name)
        # An existing preferences.json is read as before, untouched.
        settings.PreferencesStore(self.env).update(lambda doc: doc.update(claude_feedback_drafts="notify"))
        choices.update(self.env, session_env_keep=["EXAMPLE_TOOL_API_KEY"])
        self.assertEqual(settings.PreferencesStore(self.env).load(), {"claude_feedback_drafts": "notify"})


class SessionEnvKeepChoiceTests(unittest.TestCase):
    """``session_env_keep``: API-key variable names a managed session keeps."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-choices-keep-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home)}

    def test_a_write_refuses_names_the_keep_policy_forbids(self) -> None:
        for name in ("PATH", "ANTHROPIC_EXTRA_API_KEY", "CLAUDE_TOOL_API_KEY", "MANAGEMENT_PASSWORD",
                     "sk-" + "ant-" + secrets.token_hex(8)):
            with self.subTest(name=name):
                with self.assertRaises(choices.ChoicesError) as caught:
                    choices.update(self.env, session_env_keep=[name])
                if name.startswith("sk-"):
                    self.assertNotIn(name, str(caught.exception))
        self.assertFalse(choices.path(self.env).exists())

    def test_the_validated_save_refuses_a_current_provider_key_name(self) -> None:
        with self.assertRaises(choices.ChoicesError) as caught:
            choices.set_session_env_keep(self.env, ["DOCS_TOOL_API_KEY", "OPENROUTER_API_KEY"],
                                         credential_names={"OPENROUTER_API_KEY"})
        self.assertIn("OPENROUTER_API_KEY is a provider's API-key name", str(caught.exception))
        self.assertFalse(choices.path(self.env).exists())
        result = choices.set_session_env_keep(self.env, ["DOCS_TOOL_API_KEY"], credential_names={"OPENROUTER_API_KEY"})
        self.assertEqual(result.get("session_env_keep"), ["DOCS_TOOL_API_KEY"])
        self.assertEqual(choices.env_keep_problems(["DOCS_TOOL_API_KEY"], credential_names={"DOCS_TOOL_API_KEY"})[0][0],
                         "DOCS_TOOL_API_KEY")

    def test_round_trip_is_private_and_atomic_and_older_documents_still_load(self) -> None:
        state.ensure_private_dir(settings.settings_path(self.env).parent)
        state.atomic_write(settings.settings_path(self.env), b'{"version": 1}\n')
        settings.PreferencesStore(self.env).update(lambda doc: doc.update(claude_feedback_drafts="notify"))
        choices.set_session_env_keep(self.env, ["DOCS_TOOL_API_KEY"], credential_names=())
        path = choices.path(self.env)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "session_env_keep": ["DOCS_TOOL_API_KEY"]})
        self.assertEqual(choices.read(self.env).get("session_env_keep"), ["DOCS_TOOL_API_KEY"])
        # The documents an older release reads are untouched and still load.
        self.assertEqual(settings.PreferencesStore(self.env).load(), {"claude_feedback_drafts": "notify"})
        self.assertEqual(settings.SettingsStore(self.env).load(), {"version": 1})
        self.assertEqual(settings.settings_path(self.env).read_bytes(), b'{"version": 1}\n')
        for name in ("settings.schema.json", "preferences.schema.json"):
            schema = json.loads((RESOURCES_ROOT / "schemas" / name).read_text())
            self.assertNotIn("session_env_keep", json.dumps(schema), name)
        # Clearing the list removes the key (the default).
        choices.set_session_env_keep(self.env, [], credential_names=())
        self.assertEqual(json.loads(path.read_text()), {"version": 1})

    def test_a_stored_name_a_later_rule_refuses_still_reads(self) -> None:
        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b'{"version": 1, "default_profile": "mine", "session_env_keep": ["KIMI_CLAUDE_API_KEY"]}')
        current = choices.read(self.env)
        self.assertEqual(current.get("default_profile"), "mine")
        self.assertEqual(current.get("session_env_keep"), ["KIMI_CLAUDE_API_KEY"])
        # ... and the launch-time check names it.
        self.assertEqual([name for name, _why in choices.env_keep_problems(
            current.get("session_env_keep"), credential_names={"KIMI_CLAUDE_API_KEY"})], ["KIMI_CLAUDE_API_KEY"])


if __name__ == "__main__":
    unittest.main()
