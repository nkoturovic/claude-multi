"""A managed resume while this shell exports ``CLAUDE_CONFIG_DIR``.

Managed launches unset ``CLAUDE_CONFIG_DIR``, so a managed session's
transcript lives under ``~/.claude/projects``. The resume gate looks for it
under the current native root, which honours the exported variable. When the
transcript is under ``~/.claude`` instead, the refusal names that mismatch and
a retry that unsets the variable for one command; when it is nowhere the
refusal stays the unknown-root one. Metadata only: no transcript is opened.
"""

from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_multi import paths
from claude_multi.cli import resume_checks, session_facts

RUNTIME_ID = "11111111-1111-4111-8111-111111111111"


class ConfigDirResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-resume-config-dir-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self.record = {"session_id": RUNTIME_ID, "runtime_session_id": RUNTIME_ID, "cwd": str(self.project)}
        self.exported = self.root / "other-claude"
        self.runtime = SimpleNamespace(environ={"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.exported)})

    def _default_transcript(self) -> Path:
        path = (paths.native_projects(self.home, {}) / session_facts._native_project_slug(str(self.project))
                / f"{RUNTIME_ID}.jsonl")
        path.parent.mkdir(parents=True)
        path.touch()
        return path

    def test_transcript_under_the_default_root_names_the_mismatch_and_a_quoted_retry(self) -> None:
        transcript = self._default_transcript()
        with mock.patch("builtins.open", side_effect=AssertionError("metadata only")):
            gate = resume_checks._evaluate_resume_gate(self.runtime, self.record, live_prefixes=frozenset())
        self.assertEqual(gate.kind, "transcript-unknown")
        self.assertEqual(gate.actions, ())
        text = resume_checks._resume_gate_refusal(gate)
        self.assertIn("CLAUDE_CONFIG_DIR", gate.title)
        self.assertIn("managed sessions do not use it", text)
        self.assertIn(paths.display(transcript, self.runtime.environ), text)
        retry = f"env -u CLAUDE_CONFIG_DIR claude-multi -r {RUNTIME_ID}"
        self.assertIn(retry, text)
        self.assertEqual(shlex.split(retry)[:3], ["env", "-u", "CLAUDE_CONFIG_DIR"])
        self.assertNotIn("forget", text)
        self.assertFalse(self.exported.exists())

    def test_the_retry_quotes_an_unusual_id(self) -> None:
        self.assertEqual(resume_checks.config_dir_retry("a b"), "env -u CLAUDE_CONFIG_DIR claude-multi -r 'a b'")

    def test_a_transcript_in_neither_root_keeps_the_unknown_root_refusal(self) -> None:
        gate = resume_checks._evaluate_resume_gate(self.runtime, self.record, live_prefixes=frozenset())
        self.assertEqual(gate.kind, "transcript-unknown")
        self.assertEqual(gate.title, "Historical native root unknown")
        self.assertNotIn("env -u", resume_checks._resume_gate_refusal(gate))

    def test_without_the_variable_the_default_root_transcript_resumes(self) -> None:
        self._default_transcript()
        runtime = SimpleNamespace(environ={"HOME": str(self.home)})
        gate = resume_checks._evaluate_resume_gate(runtime, self.record, live_prefixes=frozenset())
        self.assertEqual(gate.kind, "ok")

    def test_a_transcript_under_the_exported_root_is_present(self) -> None:
        path = (paths.native_projects(self.home, self.runtime.environ)
                / session_facts._native_project_slug(str(self.project)) / f"{RUNTIME_ID}.jsonl")
        path.parent.mkdir(parents=True)
        path.touch()
        self._default_transcript()
        status = resume_checks._resume_transcript_status(self.runtime, self.record)
        self.assertEqual(status, ("present", str(path), True))


if __name__ == "__main__":
    unittest.main()
