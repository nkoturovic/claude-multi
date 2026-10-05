"""Shared fixture: a hermetic Runtime that really launches through the v4 path.

``V4Case`` builds a temp HOME/XDG root, a fake pinned Claude binary (the
owned copy under the temp data root; the native contract's ``verified``
entry records its size and sha256 for this platform, so ``resolve_claude``
verifies a real file) and a ``cli.Runtime`` whose ``execve`` seam captures
argv/env instead of replacing the process. ``launch_fresh``/``resume`` go
through ``Runtime.prepare`` -> ``Runtime.perform`` -> ``launch.perform_launch``:
the marker, the shared migration hold, the scope swap, the record save and
the pointer all happen for real. No test reaches a gateway or a provider.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claude_multi import cli, pin, profile, scope, sessions, state, strict_json
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
import claude_multi.service


class _Runtime(cli.Runtime):
    """No machine liveness scan: tests set ``live_prefixes`` explicitly."""

    live_prefixes: frozenset[str] = frozenset()

    def _live_prefixes(self) -> frozenset[str]:
        return self.live_prefixes


class V4Case(unittest.TestCase):
    """Temp roots, a fake verified binary and a real-launching Runtime."""

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-v4-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = self.root / "project"
        self.project.mkdir()
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "TERM": "dumb",
            "CLAUDE_MULTI_SECRET_ENV": str(self.root / "secrets" / "claude.env"),
        }
        state.ensure_private_dir(self.root / "secrets")
        state.atomic_write(
            self.root / "secrets" / "claude.env",
            b"KIMI_CLAUDE_API_KEY=v4-test-dummy\nQWEN_CLAUDE_API_KEY=v4-test-dummy\n",
        )
        token_dir = state.ensure_private_dir(Path(self.env["HOME"]) / ".config" / "claude-multi")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode())
        self.platform = pin.host_platform()
        install = pin.owned_path(self.env, "2.1.281", self.platform)
        state.ensure_private_dir(install.parent)
        install.write_bytes(b"#!/bin/fake-claude\n")
        install.chmod(0o755)
        self.fake_binary = install
        self.fake_claude = {
            "version": "2.1.281",
            "platforms": {self.platform: {
                "sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
                "size": install.stat().st_size,
            }},
            "manifest_sha256": "1" * 64,
            "signature_sha256": "2" * 64,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
            "verified_at": "2026-09-25",
            "evidence": {self.platform: "battery", "receipt_sha256": "3" * 64},
        }
        self.execs: list[tuple[str, list[str], dict[str, str]]] = []
        self.runtime = self.make_runtime()

    # ------------------------------------------------------------ seams

    def _execve(self, path: str, argv: list[str], env: dict[str, str]) -> int:
        self.execs.append((path, list(argv), dict(env)))
        return 0

    def _health(self, _base: str, _path: str) -> int:
        return 200

    def make_runtime(self, **kwargs: Any) -> _Runtime:
        params: dict[str, Any] = dict(
            asset_root=FIXTURE_ROOT,
            environ=dict(self.env),
            cwd=self.project,
            health_get=self._health,
            listener_owner=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture gateway"),
            background_liveness=lambda: sessions.BackgroundLiveness(True, cli._live_background_prefixes(self.root / "daemon")),
            managed_root=self.root / "managed",
            proc_root=self.root / "proc",
            execve=self._execve,
            doctor_binary_callback=lambda _contract: ([], ["fixture binary verified."]),
            doctor_callback=lambda _runtime: [],
            served_models_callback=lambda _gateway, _token: (None, None),
        )
        params.update(kwargs)
        runtime = _Runtime(**params)
        docs = runtime.catalog.docs
        docs["native-contract"] = {**docs["native-contract"], "verified": [copy.deepcopy(self.fake_claude)]}
        return runtime

    # ---------------------------------------------------------- helpers

    @property
    def store(self) -> sessions.SessionStore:
        return self.runtime.session_store

    def live(self, mid: str) -> Path:
        return scope.scope_dir(self.store.root, mid)

    def prev(self, mid: str) -> Path:
        return self.store.root / "scopes" / f".{mid}.prev"

    def write_transcript(self, runtime_id: str, cwd: str | None = None) -> Path:
        path = (
            Path(self.env["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(cwd or self.runtime.cwd)
            / f"{runtime_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def profile_target(self, name: str = "balanced") -> cli.LaunchTarget:
        document = self.runtime.profiles.load(name)
        return cli.LaunchTarget("profile", document, name, True, f"Profile {name}")

    def direct_target(self, model: str = "sol") -> cli.LaunchTarget:
        return cli.LaunchTarget(
            "ad-hoc", profile.ad_hoc_direct(model), None, False, f"Direct {model}"
        )

    def launch_fresh(self, target: cli.LaunchTarget | None = None, **kwargs: Any) -> dict:
        prepared = self.runtime.prepare(
            target or self.profile_target(), action="fresh", passthrough=[], **kwargs
        )
        self.runtime.perform(prepared)
        mid = sessions.managed_id(prepared.record)
        self.write_transcript(mid)
        return self.store.load(mid)

    def prepare_resume(self, mid: str, target: cli.LaunchTarget | None = None, **kwargs: Any):
        record = self.store.load(mid)
        target = target or cli.LaunchTarget(
            "record", None, record.get("profile"), bool(record.get("follow")), f"Session {mid[:8]}"
        )
        return self.runtime.prepare(
            target, action="resume", passthrough=[], session_id=mid, **kwargs
        )

    def resume(self, mid: str, **kwargs: Any) -> dict:
        self.runtime.perform(self.prepare_resume(mid, **kwargs))
        return self.store.load(mid)

    def tree(self, path: Path) -> dict[str, bytes]:
        if not path.exists():
            return {}
        return {
            str(item.relative_to(path)): item.read_bytes()
            for item in sorted(path.rglob("*"))
            if item.is_file()
        }

    def record_bytes(self, mid: str) -> bytes | None:
        return self.store.read_record_bytes(mid)

    def save_v4(self, record: dict) -> None:
        self.store.save(record)

    def mutate(self, mid: str, **changes: Any) -> dict:
        """Rewrite a v4 record's top-level fields (a test-only launcher write)."""

        record = copy.deepcopy(self.store.load(mid))
        record.update(changes)
        if "applied" in changes:
            record["applied_hash"] = strict_json.bundle_digest(record["applied"])
        self.store.save(record)
        return record
