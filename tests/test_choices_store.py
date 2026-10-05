"""The choices document as the default profile and the sign-in
acknowledgements use it: strict loading, refused documents, its leaf lock,
acknowledgement records, round trips that keep every other choice, and the
documents older releases read staying untouched."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from claude_multi import choices, settings, state

from _catalog import FIXTURE_ROOT

ACK = {"text_id": "claude-personal-1", "text_sha256": "0" * 63 + "1",
       "acknowledged_at": "2026-10-02T09:30:15Z", "typed": "personal"}


class ChoicesStoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-choices-store-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = {"HOME": str(self.root / "home")}
        (self.root / "home").mkdir(mode=0o700)

    def write(self, data: bytes) -> None:
        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, data)


class StrictLoadTests(ChoicesStoreCase):
    def test_duplicate_keys_and_invalid_documents_are_refused(self) -> None:
        cases = (
            b'{"version": 1, "default_profile": "a", "default_profile": "b"}',
            b'{"version": 1, "acknowledgements": []}',
            b'{"version": 1, "acknowledgements": {"gemini": {}}}',
            json.dumps({"version": 1, "acknowledgements": {"claude": dict(ACK, typed="yes")}}).encode(),
            json.dumps({"version": 1, "acknowledgements": {"claude": dict(ACK, text_sha256="ABC")}}).encode(),
            json.dumps({"version": 1, "acknowledgements": {"claude": dict(ACK, acknowledged_at="today")}}).encode(),
            json.dumps({"version": 1, "acknowledgements": {"claude": dict(ACK, text_id="")}}).encode(),
            json.dumps({"version": 1, "acknowledgements": {"claude": dict(ACK, extra=1)}}).encode(),
            json.dumps({"version": 1, "acknowledgements": {"claude": {k: v for k, v in ACK.items()
                                                                       if k != "typed"}}}).encode(),
        )
        for data in cases:
            self.write(data)
            with self.subTest(data=data), self.assertRaises(choices.ChoicesError) as caught:
                choices.read(self.env)
            self.assertIn("choices.json", caught.exception.remedy)

    def test_a_valid_document_reads_back(self) -> None:
        self.write(json.dumps({"version": 1, "default_profile": "mine",
                               "acknowledgements": {"codex": ACK}}).encode())
        current = choices.read(self.env)
        self.assertEqual(current.get("default_profile"), "mine")
        self.assertEqual(current.get("acknowledgements"), {"codex": ACK})
        self.assertEqual(choices.Choices().get("acknowledgements"), {})


class AcknowledgementTests(ChoicesStoreCase):
    def test_records_per_pool_keep_every_other_choice(self) -> None:
        choices.update(self.env, default_profile="mine", session_env_keep=["EXAMPLE_TOOL_API_KEY"])
        choices.set_acknowledgement(self.env, "claude", ACK)
        choices.set_acknowledgement(self.env, "codex", dict(ACK, text_id="codex-personal-1"))
        document = json.loads(choices.path(self.env).read_text())
        self.assertEqual(document, {
            "version": 1, "default_profile": "mine", "session_env_keep": ["EXAMPLE_TOOL_API_KEY"],
            "acknowledgements": {"claude": ACK, "codex": dict(ACK, text_id="codex-personal-1")}})
        choices.set_acknowledgement(self.env, "claude", None)
        self.assertEqual(choices.read(self.env).get("acknowledgements"),
                         {"codex": dict(ACK, text_id="codex-personal-1")})
        choices.set_acknowledgement(self.env, "codex", None)
        self.assertNotIn("acknowledgements", json.loads(choices.path(self.env).read_text()))
        # The default profile round trip keeps the acknowledgements and the rest.
        choices.set_acknowledgement(self.env, "claude", ACK)
        choices.update(self.env, default_profile=None)
        self.assertEqual(json.loads(choices.path(self.env).read_text()),
                         {"version": 1, "session_env_keep": ["EXAMPLE_TOOL_API_KEY"], "acknowledgements": {"claude": ACK}})

    def test_refused_records_write_nothing(self) -> None:
        choices.set_acknowledgement(self.env, "claude", ACK)
        before = choices.path(self.env).read_bytes()
        for pool, record in (("gemini", ACK), ("codex", dict(ACK, typed="sure"))):
            with self.subTest(pool=pool), self.assertRaises(choices.ChoicesError):
                choices.set_acknowledgement(self.env, pool, record)
        self.assertEqual(choices.path(self.env).read_bytes(), before)
        self.assertEqual(choices.ACK_POOLS, ("claude", "codex"))
        self.assertEqual(choices.ACK_WORD, "personal")


class LockTests(ChoicesStoreCase):
    def test_a_leaf_lock_of_its_own(self) -> None:
        """Writes take only ``choices.json.lock``: they wait for it and for
        nothing else (not the profiles or Settings store locks, not the
        served-change barrier)."""

        target = choices.path(self.env)
        state.ensure_private_dir(target.parent)
        for other in ("profiles", "settings.json", "preferences.json", "served-change"):
            with self.subTest(held=other), state.FileLock(target.parent / other):
                choices.update(self.env, default_profile="mine")
        lock = state.FileLock(target)
        lock.acquire()
        done = threading.Event()
        worker = threading.Thread(target=lambda: (choices.update(self.env, default_profile="other"), done.set()))
        worker.start()
        self.assertFalse(done.wait(0.3))  # waits for the holder of choices.json.lock
        lock.release()
        worker.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(choices.read(self.env).get("default_profile"), "other")
        self.assertEqual(lock.lock_path.name, "choices.json.lock")

    def test_conditional_writes_compare_under_the_lock(self) -> None:
        self.assertEqual(choices.digest(self.env), "absent")
        absent = choices.digest(self.env)
        choices.update(self.env, expected=absent, default_profile="mine")
        read = choices.digest(self.env)
        self.assertTrue(read.startswith("sha256:"))
        choices.update(self.env, default_profile="other")  # another writer
        before = choices.path(self.env).read_bytes()
        with self.assertRaises(choices.ChoicesChanged):
            choices.update(self.env, expected=read, default_profile=None)
        self.assertEqual(choices.path(self.env).read_bytes(), before)
        # A value moves only while it is still the one named.
        self.assertFalse(choices.replace(self.env, "default_profile", "mine", "ours"))
        self.assertEqual(choices.read(self.env).get("default_profile"), "other")
        self.assertTrue(choices.replace(self.env, "default_profile", "other", None))
        self.assertIsNone(choices.read(self.env).get("default_profile"))
        self.assertNotIn("default_profile", json.loads(choices.path(self.env).read_text()))


class OlderReleaseDocumentTests(ChoicesStoreCase):
    def test_settings_and_preferences_are_untouched_and_still_load(self) -> None:
        env = dict(self.env)
        settings_path = settings.settings_path(env)
        preferences_path = settings.preferences_path(env)
        state.ensure_private_dir(settings_path.parent)
        state.atomic_write(settings_path, b'{\n  "admitted_lines": [],\n  "version": 1\n}\n')
        state.atomic_write(preferences_path, b'{\n  "claude_feedback_drafts": "notify"\n}\n')
        before = (settings_path.read_bytes(), preferences_path.read_bytes())
        choices.update(env, default_profile="mine")
        choices.set_acknowledgement(env, "claude", ACK)
        self.assertEqual((settings_path.read_bytes(), preferences_path.read_bytes()), before)
        self.assertEqual(settings.feedback_drafts_preference(env), ("notify", None))
        self.assertEqual(settings.SettingsStore(env).load(), {"admitted_lines": [], "version": 1})
        for name in ("settings.schema.json", "preferences.schema.json"):
            schema = json.loads((FIXTURE_ROOT / "schemas" / name).read_text())
            with self.subTest(schema=name):
                self.assertIs(schema.get("additionalProperties"), False)
                self.assertFalse(set(choices.FIELDS) & set(schema.get("properties", {})))


if __name__ == "__main__":
    unittest.main()
