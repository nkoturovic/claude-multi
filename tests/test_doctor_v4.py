"""Doctor on v4 state, plus the settings radar, retirement radar and journal pass.

Sessions are real v4 launches (``_v4.V4Case``); switch sequences use the real
``claude-multi lineup`` and the protocol-3 ``postmodel`` seam. Retired /
renamed keys use test-local catalogs; the
retirement radar reads a test-local copy of the pinned-registry fixture with
an injected clock; the journal pass reads fixture text through its seam, so
no test reads the host journal, the operator's settings or a gateway.
"""

from __future__ import annotations

import copy
import errno
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _tripwire
from claude_multi import (
    catalog,
    cli,
    custom,
    endpoint,
    paths,
    hooks,
    lineup_log,
    launch,
    managed,
    management,
    quota,
    service,
    proxy,
    scope,
    sessions,
    state,
    strict_json,
    transition,
)
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from _v4 import V4Case
from test_lineup import LineupCase
from test_migrate import _local_cat, _retired_entry
from tests.test_quota import FIXTURES as QUOTA_FIXTURES, NOW as QUOTA_NOW, SENTINELS
import claude_multi.compiler
from claude_multi.cli import doctor as cli_doctor, text as cli_text
import claude_multi.launch
import claude_multi.paths
import claude_multi.retention

_tripwire.install()

REGISTRY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry"
OTHER_RID = "77777777-7777-4777-8777-777777777777"


def _doctor(case: V4Case, runtime: cli.Runtime | None = None) -> tuple[list[str], list[str], list[str]]:
    return cli._collect_doctor_reports(runtime or case.runtime)


def _run_doctor(case: V4Case, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch("sys.stderr", io.StringIO()):
        code = cli.main(["doctor", *argv], runtime=case.runtime, output_stream=out, interactive=False)
    return code, out.getvalue()


class _DoctorCase(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]
        self.m8 = self.mid[:8]
        self.rid = self.record["runtime_session_id"]

    def add_agents_line(self) -> None:
        """A test-local catalog copy that gained an agents-capable line (``sol2``)."""

        root = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, root)
        models_path = root / "catalog" / "models.json"
        document = json.loads(models_path.read_text())
        line = copy.deepcopy(document["models"]["sol"])
        line["display"] = "GPT-5.6 Sol Two"
        line["wire_model"] = "gpt-5.6-sol-two"
        for effort, spec in line["efforts"].items():
            spec["selector"] = f"gpt-multi-sol2-{effort}[1m]"
        document["models"]["sol2"] = line
        models_path.write_text(json.dumps(document, indent=2) + "\n")
        self.runtime = self.make_runtime(asset_root=root)


class CleanStateTests(_DoctorCase):
    def test_a_fresh_launch_is_ready_without_attention(self) -> None:
        problems, info, attention = _doctor(self)
        self.assertEqual((problems, attention), ([], []))
        self.assertIn(f"Scope: {self.m8} OK (lineup generation 1).", info)
        code, output = _run_doctor(self)
        self.assertEqual(code, 0, output)
        self.assertIn("Ready", output.splitlines())  # the verdict line
        self.assertNotIn("Attention", output)


class ClientIdentityTests(_DoctorCase):
    """Only fixture procfs metadata, no client execution/hash."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = self.fake_binary
        self.client = self.root / "versions" / "2.1.999"
        self.client.parent.mkdir()
        self.client.write_bytes(b"different client")
        self.process = self.runtime.proc_root / "123"
        self.process.mkdir(parents=True)
        (self.process / "cmdline").write_bytes(b"claude\0--session-id\0" + self.rid.encode() + b"\0")
        self.exe = self.process / "exe"
        self.exe.symlink_to(self.client)

    def _report(self):
        return cli._doctor_client_report(self.runtime, [self.record])

    def test_mismatch_is_exact_doctor_attention_only(self) -> None:
        expected = (f"session {self.m8} runs Claude 2.1.999, not the pinned {self.fake_claude['version']} "
                    "(it started before a re-pin, or the daemon adopted it after an auto-update); "
                    f"resume it with claude-multi -r {self.mid} after it exits")
        self.assertEqual(self._report(), [expected])
        argument = expected.split("claude-multi -r ", 1)[1].split(" after it exits", 1)[0]
        self.assertEqual(cli._resolve_resume_target(self.runtime, argument), self.mid)
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertIn(expected, attention)
        self.assertFalse((self.store.root / "lineup-log" / f"{self.mid}.log").exists())

    def test_matching_pin_and_hard_link_are_not_mismatches(self) -> None:
        hardlink = self.root / "hardlink" / self.fake_claude["version"]
        hardlink.parent.mkdir()
        os.link(self.pin, hardlink)
        for target in (self.pin, hardlink):
            self.exe.unlink()
            self.exe.symlink_to(target)
            self.assertEqual(self._report(), [])
        self.assertEqual(sessions.proc_session_ids(self.runtime.proc_root), frozenset({self.rid}))

    def test_device_is_compared_even_when_inode_numbers_match(self) -> None:
        pin_stat = self.pin.stat()
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            if path == self.exe:
                return mock.Mock(st_dev=pin_stat.st_dev + 1, st_ino=pin_stat.st_ino)
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat), \
                mock.patch.object(claude_multi.retention, "_digest", side_effect=AssertionError("identity must not hash")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("identity must not execute")):
            self.assertEqual(len(self._report()), 1)

    def test_retained_copy_when_original_gone(self) -> None:
        retained = paths.retained_root(self.runtime.environ) / self.fake_claude["version"]
        retained.parent.mkdir(parents=True)
        shutil.copyfile(self.pin, retained)
        self.pin.unlink()
        self.exe.unlink()
        self.exe.symlink_to(retained)
        self.assertEqual(self._report(), [])
        self.exe.unlink()
        self.exe.symlink_to(self.client)
        self.assertEqual(len(self._report()), 1)
        retained.unlink()
        self.assertEqual(self._report(), [])  # no known pin to compare

    def test_retained_client_still_matches_after_same_version_reinstall(self) -> None:
        retained = paths.retained_root(self.runtime.environ) / self.fake_claude["version"]
        retained.parent.mkdir(parents=True)
        os.link(self.pin, retained)
        self.pin.unlink()
        self.exe.unlink()
        self.exe.symlink_to(retained)
        self.assertEqual(self._report(), [])
        shutil.copyfile(retained, self.pin)  # same bytes, new inode
        self.assertNotEqual(self.pin.stat().st_ino, retained.stat().st_ino)
        with mock.patch.object(claude_multi.retention, "_digest", side_effect=AssertionError("identity must not hash")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("identity must not execute")):
            self.assertEqual(self._report(), [])
            self.exe.unlink()
            self.exe.symlink_to(self.pin)
            self.assertEqual(self._report(), [])  # both existing references accepted
            self.exe.unlink()
            self.exe.symlink_to(self.client)
            self.assertEqual(len(self._report()), 1)  # neither reference matches

    def test_unreadable_pin_is_unknown_even_with_retained_copy(self) -> None:
        retained = paths.retained_root(self.runtime.environ) / self.fake_claude["version"]
        retained.parent.mkdir(parents=True)
        os.link(self.pin, retained)
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == self.pin:
                raise PermissionError(13, "permission denied", str(path))
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(self._report(), [])
            self.assertEqual(sessions.proc_client_mismatches(
                [self.rid], self.pin, retained=retained, proc_root=self.runtime.proc_root,
            ), {})

    def test_launcher_before_exec_is_unknown(self) -> None:
        interpreter = self.root / "python3.14"
        interpreter.write_bytes(b"fixture interpreter")
        (self.process / "cmdline").write_bytes(
            b"python3.14\0/nix/store/fixture-claude-multi/bin/.claude-multi-wrapped\0--resume\0"
            + self.rid.encode() + b"\0"
        )
        self.exe.unlink()
        self.exe.symlink_to(interpreter)
        self.assertEqual(sessions.proc_session_ids(self.runtime.proc_root), frozenset({self.rid}))
        self.assertEqual(self._report(), [])

    def test_missing_deleted_and_unreadable_exe_are_unknown(self) -> None:
        self.exe.unlink()
        self.assertEqual(self._report(), [])
        self.exe.symlink_to(self.root / "absent")
        self.assertEqual(self._report(), [])
        self.exe.unlink()
        deleted = self.root / "2.1.999 (deleted)"
        deleted.touch()  # even a stat-able link marked deleted stays unknown
        self.exe.symlink_to(deleted)
        self.assertEqual(self._report(), [])
        self.exe.unlink()
        self.exe.symlink_to(self.client)
        original_stat = Path.stat
        for unreadable in (self.exe, self.pin):
            def stat(path, *args, **kwargs):
                if path == unreadable:
                    raise PermissionError("fixture inaccessible")
                return original_stat(path, *args, **kwargs)
            with mock.patch.object(Path, "stat", stat):
                self.assertEqual(self._report(), [])
        with mock.patch.object(os, "readlink", side_effect=PermissionError("fixture inaccessible")):
            self.assertEqual(self._report(), [])

    def test_alias_resume_forms_and_unrelated_process(self) -> None:
        self.record["runtime_aliases"] = [{"session_id": OTHER_RID}]
        for argv in (b"claude\0--resume\0" + OTHER_RID.encode(), b"claude\0--resume=" + OTHER_RID.encode()):
            (self.process / "cmdline").write_bytes(argv + b"\0")
            self.assertEqual(len(self._report()), 1)
        self.record["runtime_aliases"] = []
        with mock.patch.object(os, "readlink", side_effect=AssertionError("unrelated exe read")):
            self.assertEqual(self._report(), [])


class ProfileReportTests(_DoctorCase):
    """Every profile evaluated with current Settings; named bindings."""

    def _user_profile(self, name: str, mutate) -> None:
        document = self.runtime.profiles.load("balanced")
        document.pop("seed", None)
        document["name"] = name
        mutate(document)
        self.runtime.profiles.save(document, target=name)

    def test_an_invalid_profile_blocks_one_line_per_error(self) -> None:
        # an agents-only line is no lead (a profile evaluation error)
        self._user_profile("bad", lambda d: d.update(lead={"model": "gpt55", "effort": "high"}))
        problems, _info, _attention = _doctor(self)
        mine = [line for line in problems if line.startswith("profile bad: ")]
        self.assertTrue(mine, problems)
        self.assertFalse(any(line.startswith("profile balanced") for line in problems))

    def test_a_retired_agent_binding_is_attention(self) -> None:
        def bind(document):
            # the strong grade requires the plain one: unbind both paths cleanly
            document["agents"].pop("cm-reviewer-strong", None)
            document["agents"]["cm-reviewer"] = {"model": "muse-spark", "effort": "high"}
        self._user_profile("stale", bind)
        problems, _info, attention = _doctor(self)
        self.assertFalse([p for p in problems if "stale" in p], problems)
        self.assertTrue(
            any(line.startswith("profile stale: reviewer: 'muse-spark' was removed") for line in attention),
            attention,
        )

    def test_a_named_binding_on_a_retired_key_is_attention(self) -> None:
        path = sessions.config_root(self.runtime.environ) / "bindings.json"
        state.ensure_private_dir(path.parent)
        state.atomic_write(
            path,
            strict_json.canonical_file_bytes(
                {"version": 1, "bindings": {"old-review": {"model": "muse-spark", "effort": "high"}}}
            ),
        )
        _problems, _info, attention = _doctor(self)
        self.assertTrue(
            any(line.startswith("bindings.old-review.model: ") and line.endswith("bind the live line")
                for line in attention),
            attention,
        )

    def test_missing_credentials_are_attention_per_profile(self) -> None:
        self._user_profile("kimi-lead", lambda d: d.update(lead={"model": "kimi-k3", "effort": "max"}))
        (self.root / "secrets" / "claude.env").unlink()
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertIn(
            "profile kimi-lead: provider kimi has no credential ("
            + cli._connect_hint(self.runtime, "kimi") + ")",
            attention,
        )

    def test_a_stale_installed_seed_is_attention_with_the_reseed_command(self) -> None:
        # Doctor names a newer shipped seed, like
        # the card and the editor, so a script-only operator sees it.
        store = self.runtime.profiles
        store.install_seeds()
        seed = catalog.DEFAULT_SEED
        shipped = self.runtime.catalog.seed_profiles[seed]["seed"]["version"]
        _problems, _info, attention = _doctor(self)
        self.assertFalse(any("newer shipped version of this seed" in line for line in attention), attention)
        store.update(seed, lambda d: d.pop("seed"))
        _problems, _info, attention = _doctor(self)
        self.assertIn(
            f"profile {seed}: a newer shipped version of this seed exists (v? → v{shipped}): "
            f"claude-multi profile reseed {seed} (your version is kept as a backup)",
            attention,
        )
        if shipped >= 2:
            def older(document):
                document["seed"] = {"id": seed, "version": shipped - 1}
            store.update(seed, older)
            _problems, _info, attention = _doctor(self)
            self.assertIn(
                f"profile {seed}: a newer shipped version of this seed exists (v{shipped - 1} → v{shipped}): "
                f"claude-multi profile reseed {seed} (your version is kept as a backup)",
                attention,
            )

    def test_unreadable_settings_block(self) -> None:
        path = self.runtime.settings_store.path
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"{not json")
        problems, _info, _attention = _doctor(self)
        self.assertTrue(any(line.startswith("settings: cannot read operator settings") for line in problems),
                        problems)


class RecordReportTests(_DoctorCase):
    """Retired keys (needs a choice), a followed profile, and the follower checks."""

    def _with_lead_key(self, mid: str, key: str) -> None:
        record = self.store.load(mid)
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = key
        self.mutate(mid, applied=applied)

    def test_needs_a_choice_is_one_aggregated_line(self) -> None:
        second = self.launch_fresh()["managed_id"]
        self._with_lead_key(self.mid, "muse-spark")  # retired, successor null
        self._with_lead_key(second, "no-such-key")  # unknown to the merged catalog
        _problems, _info, attention = _doctor(self)
        line = next(a for a in attention if "need a profile or model choice" in a)
        self.assertTrue(line.startswith("2 session(s) need a profile or model choice (lead removed): "))
        self.assertIn("claude-multi lineup --session <runtime-id> --relaunch profile <name> | direct <model>", line)
        self.assertIn(self.m8, line)
        self.assertIn(second[:8], line)

    def test_externalized_provider_notice_only_while_referenced(self) -> None:
        # The absence of a sample is never a standing
        # warning; a session on an externalized provider's key gets one notice.
        entry = {**_retired_entry(None, since=36, wire="opfix-lan-1", provider="opfix-lan",
                                  selectors={"claude-multi-opfix-lan": None}),
                 "externalized": {"sample": "opfix-lan.json"}}
        local = _local_cat(retired={"opfix-lan-line": entry})
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local):
            _problems, _info, attention = _doctor(self)
        self.assertFalse(any("left the catalog" in line for line in attention), attention)
        self._with_lead_key(self.mid, "opfix-lan-line")
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local):
            _problems, _info, attention = _doctor(self)
        line = next(a for a in attention if "left the catalog" in a)
        self.assertTrue(line.startswith("1 session(s) lead on provider opfix-lan, which left the catalog"))
        self.assertIn("claude-multi providers add --preset opfix-lan", line)
        self.assertIn(self.m8, line)
        self.assertTrue(any("need a profile or model choice" in a for a in attention))

    def test_a_retired_lead_with_a_successor_names_it(self) -> None:
        local = _local_cat(retired={"opus@4.7": _retired_entry(
            "opus55", since=33, wire="claude-opus-4-7", selectors={"claude-opus-4-7[1m]": None},
            generation="4.7")})
        self._with_lead_key(self.mid, "opus@4.7")
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local):
            _problems, _info, attention = _doctor(self)
        self.assertIn(
            f"session {self.m8}: lead opus@4.7 retired → opus55 (applies at the next resume; "
            "thinking continuity is not carried across generations)",
            attention,
        )

    def test_a_pure_rename_compares_the_final_wire_and_provider(self) -> None:
        key = self.record["applied"]["lead"]["key"]
        entry = self.runtime.lineup_catalog().lines[key]
        old = _retired_entry(
            "intermediate-name", since=33, wire=entry["wire_model"],
            provider=entry["provider"], selectors={"old-selector": None},
        )
        intermediate = {**old, "successor": key}
        for change in ({}, {"last_wire": "older-wire"}, {"provider": "other-provider"}):
            with self.subTest(change=change):
                local = _local_cat(retired={"old-name": {**old, **change},
                                           "intermediate-name": intermediate})
                self._with_lead_key(self.mid, "old-name")
                with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local):
                    attention, _info = cli._doctor_record_report(self.runtime, [self.store.load(self.mid)])
                if change:
                    expected = (f"lead old-name retired → {key} (applies at the next resume; "
                                "thinking continuity is not carried across generations)")
                else:
                    expected = f"lead old-name renamed → {key} (same model; applies at the next resume)"
                self.assertIn(f"session {self.m8}: {expected}", attention)

    def test_a_removed_agent_key_is_unbound_at_the_next_resume(self) -> None:
        record = self.store.load(self.mid)
        applied = copy.deepcopy(record["applied"])
        applied["agents"]["cm-reviewer"]["key"] = "muse-spark"
        self.mutate(self.mid, applied=applied)
        _problems, _info, attention = _doctor(self)
        self.assertIn(
            f"session {self.m8}: reviewer muse-spark was removed — unbound at the next resume",
            attention,
        )

    def test_a_missing_followed_profile_is_attention_and_never_flips_follow(self) -> None:
        document = self.runtime.profiles.load("balanced")
        document.pop("seed", None)
        document["name"] = "mine"
        self.runtime.profiles.save(document, target="mine")
        mine = self.launch_fresh(self.profile_target("mine"))
        self.runtime.profiles.delete("mine")
        before = self.record_bytes(mine["managed_id"])
        _problems, _info, attention = _doctor(self)
        rid = mine["runtime_session_id"]
        self.assertIn(
            f"session {mine['managed_id'][:8]} follows profile 'mine', which no longer exists; it "
            f"keeps its applied lineup — pin it (claude-multi lineup --session {rid} pin) or "
            f"choose one (claude-multi lineup --session {rid} profile <name>)",
            attention,
        )
        self.assertEqual(self.record_bytes(mine["managed_id"]), before)

    def test_pending_lead_target_and_settings_drift_are_info(self) -> None:
        pending = {
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "quality",
            "follow": True, "document": None, "reasons": ["native agents differ"],
        }
        record = self.store.load(self.mid)
        lead = record["applied"]["lead"]
        target = {"key": lead["key"], "effort": "xhigh", "selector": lead["selector"]}
        self.mutate(self.mid, pending=pending, lead_target=target)
        store = self.runtime.settings_store
        document = store.load()
        document["compaction_percent"] = 80
        store.save(document, catalog=self.runtime.catalog)
        problems, info, attention = _doctor(self)
        self.assertEqual((problems, attention), ([], []))
        self.assertIn(
            f"session {self.m8}: pending relaunch change (native agents differ); applies at the next resume",
            info,
        )
        self.assertIn(
            f"session {self.m8}: requested lead {self.runtime.lineup_catalog().lines[lead['key']]['display']} "
            "· xhigh not switched yet "
            "(/model in the session, or the next resume)",
            info,
        )
        self.assertTrue(any(line.startswith(f"session {self.m8}: settings drift: ") for line in info), info)

    def test_v3_records_get_the_migrate_line_not_per_record_lines(self) -> None:
        from test_migrate import v3_managed, _write_raw

        _write_raw(self.store, v3_managed("11111111-1111-4111-8111-111111111111", cwd=self.runtime.cwd))
        _problems, info, attention = _doctor(self)
        self.assertIn("1 legacy record(s) (version ≤ 3) remain: rerun claude-multi migrate", attention)
        self.assertFalse(any("11111111" in line and "retired" in line for line in attention))
        self.assertTrue(any("legacy record · lead" in line for line in info), info)
        self.assertIn("Sessions: 2 recorded · 1 legacy record (claude-multi migrate).", info)

    def test_a_v3_record_gets_no_compile_or_compaction_diagnostics(self) -> None:
        # Doctor never compiles
        # a v1-3 record: a record carrying a 2.x context snapshot
        # yields only the migrate attention line, never a compile, fence,
        # compaction, context-window or trigger diagnostic about it.
        from test_migrate import v3_managed, _write_raw

        mid = "99999999-9999-4999-8999-999999999999"
        legacy = v3_managed(mid, cwd=self.runtime.cwd)
        self.assertIn("lead", legacy["snapshot"])  # the 2.x snapshot, with its own context facts
        _write_raw(self.store, legacy)
        problems, info, attention = _doctor(self)
        self.assertIn("1 legacy record(s) (version ≤ 3) remain: rerun claude-multi migrate", attention)
        words = ("compile", "fence", "compaction", "compacts", "context window", "trigger", "auto-compact")
        for line in [*problems, *attention, *info]:
            if mid[:8] in line or mid in line:
                with self.subTest(line=line):
                    self.assertFalse([w for w in words if w in line.lower()], line)
        self.assertFalse([line for line in problems if mid[:8] in line], problems)


class ScopeIntegrityTests(_DoctorCase):
    """Tamper, fence gaps, rebuilds, compile failures and the epoch bump."""

    def _launch_bytes(self) -> tuple[bytes, bytes]:
        live = self.live(self.mid)
        return (live / "settings.json").read_bytes(), (live / "lead-set.json").read_bytes()

    def test_launch_time_tamper_blocks_and_repair_restores(self) -> None:
        live = self.live(self.mid)
        launch_bytes = self._launch_bytes()
        for case in ("lead-set row", "env base url"):
            with self.subTest(case):
                if case == "lead-set row":
                    doc = strict_json.loads((live / "lead-set.json").read_bytes())
                    doc["rows"][0]["label"] = "edited"
                    state.atomic_write(live / "lead-set.json", strict_json.canonical_file_bytes(doc))
                else:
                    doc = strict_json.loads((live / "settings.json").read_bytes())
                    doc["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
                    state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
                problems, _info, _attention = _doctor(self)
                self.assertTrue(
                    any(p.startswith(f"session {self.m8}: scope differs from the record-authoritative compile")
                        for p in problems),
                    problems,
                )
                code, output = _run_doctor(self, "--repair", self.mid)
                self.assertEqual(code, 0, output)
                self.assertEqual(self._launch_bytes(), launch_bytes)
                problems, _info, attention = _doctor(self)
                self.assertEqual((problems, attention), ([], []))

    def test_a_fence_gap_blocks(self) -> None:
        live = self.live(self.mid)
        doc = strict_json.loads((live / "settings.json").read_bytes())
        reviewer = self.record["applied"]["agents"]["cm-reviewer"]["selector"]
        doc["availableModels"] = [s for s in doc["availableModels"] if s != reviewer]
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
        problems, _info, _attention = _doctor(self)
        self.assertIn(
            f"session {self.m8}: reviewer is bound to {reviewer}, which the compiled fence does not "
            "admit — the agent would silently run on the lead model; run claude-multi doctor "
            f"--repair {self.mid}",
            problems,
        )

    def test_a_catalog_only_difference_is_attention(self) -> None:
        # lineup.md states the launch-time lead set (its size, the Default
        # row): a catalog that gained a lead-class line never rewrites it
        # under the running session (fixed in transition.expected_plan).
        lineup_md = (self.live(self.mid) / "lineup.md").read_bytes()
        self.add_agents_line()
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        _run_doctor(self, "--repair", self.mid)
        self.assertEqual((self.live(self.mid) / "lineup.md").read_bytes(), lineup_md)
        self.assertIn(
            f"session {self.m8}: fence/picker differ from the installed catalog (catalog changed "
            "since launch); applies at the next resume",
            attention,
        )

    def test_a_rebuild_under_a_changed_catalog_is_attention_never_block(self) -> None:
        self.add_agents_line()
        (self.live(self.mid) / "settings.json").unlink()
        _run_doctor(self, "--repair", self.mid)
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertIn(
            f"session {self.m8}: launch-time files were rebuilt from the installed catalog after "
            f"launch; live agent changes need a relaunch — claude-multi -r {self.mid} after it exits",
            attention,
        )

    def test_compile_failures_never_crash(self) -> None:
        for exc in (KeyError("gone"), claude_multi.compiler.CompilerError("boom")):
            with self.subTest(type(exc).__name__), mock.patch.object(
                transition, "expected_plan", side_effect=exc
            ):
                problems, _info, attention = _doctor(self)
            self.assertTrue(any(line.startswith(f"session {self.m8}: scope not checked (") for line in attention),
                            attention)

    def test_an_epoch_bump_from_a_fork_decision_is_no_catalog_change(self) -> None:
        # resolve-fork bumps the parent's epoch; the proven env
        # still states the launch epoch, so the catalog comparison uses it. The
        # non-launch-time hooks carry record.launch_epoch exactly as 2.26 does
        # (the stale hook epoch is drift until a repair, 2.26 parity).
        fork = "66666666-6666-4666-8666-666666666666"
        self.mutate(self.mid, pending_forks=[{"session_id": fork, "observed_at": "2026-09-25T00:00:00Z"}],
                    identity_state=sessions.IDENTITY_PENDING_FORK)
        self.store.resolve_fork(self.mid, fork)
        self.assertGreater(self.store.load(self.mid)["launch_epoch"], self.record["launch_epoch"])
        problems, _info, attention = _doctor(self)
        self.assertFalse([line for line in attention if "catalog changed since launch" in line], attention)
        self.assertFalse([line for line in problems if "fence" in line], problems)
        code, output = _run_doctor(self, "--repair", self.mid)
        self.assertEqual(code, 0, output)
        problems, _info, attention = _doctor(self)
        self.assertEqual((problems, attention), ([], []))


class SwitchSequenceTests(LineupCase):
    """/model switches and live applies are never drift."""

    def setUp(self) -> None:
        super().setUp()
        self.lead_set = strict_json.loads((self.live(self.mid) / "lead-set.json").read_bytes())
        lead = self.record["applied"]["lead"]
        rows = [row for row in self.lead_set["rows"] if row["key"] != lead["key"]]
        self.assertGreaterEqual(len(rows), 2, "the fixture lead set needs two other rows")
        self.row_b, self.row_c = rows[0], rows[1]
        self.row_a = next(row for row in self.lead_set["rows"] if row["key"] == lead["key"])

    def postmodel(self, row: dict) -> None:
        record = self.store.load(self.mid)
        payload = {"hook_event_name": "PostModelSwitch", "session_id": self.rid, "source": "command",
                   "to_model": row["selector"], "from_model": record["applied"]["lead"]["selector"]}
        argv = ["session-event", "postmodel", "--managed-id", self.mid, "--launch-epoch",
                str(record["launch_epoch"]), "--hook-protocol", "3"]
        with mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(argv, runtime=self.runtime,
                            input_stream=io.StringIO(json.dumps(payload)),
                            output_stream=io.StringIO(), interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load(self.mid)["applied"]["lead"]["key"], row["key"])

    def live_set(self) -> None:
        code, out, _err = self.request("set implementer=opus55:xhigh")
        self.assertEqual(code, 0)
        self.assertIn("next: type /reload-plugins", out)  # the next-step line

    def assert_quiet_and_repair_keeps_bytes(self) -> None:
        problems, info, attention = cli._collect_doctor_reports(self.runtime)
        self.assertEqual((problems, attention), ([], []))
        self.assertIn(
            f"session {self.mid[:8]}: lead switched with /model since the last compile; restated "
            "at the next resume",
            info,
        )
        before = self.scope_files()
        code, output = _run_doctor(self, "--repair", self.mid)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.scope_files(), before)

    def test_a_switch_alone(self) -> None:
        self.postmodel(self.row_b)
        self.assert_quiet_and_repair_keeps_bytes()

    def test_switch_live_apply_switch(self) -> None:
        self.postmodel(self.row_b)
        self.live_set()
        self.assertEqual(self.store.load(self.mid)["scope_lead"]["key"], self.row_b["key"])
        self.postmodel(self.row_c)
        self.assert_quiet_and_repair_keeps_bytes()

    def test_switch_live_apply_switch_back(self) -> None:
        self.postmodel(self.row_b)
        self.live_set()
        self.postmodel(self.row_a)
        self.assert_quiet_and_repair_keeps_bytes()

    def gain_a_lead_class_line(self) -> None:
        root = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, root)
        models_path = root / "catalog" / "models.json"
        document = json.loads(models_path.read_text())
        line = copy.deepcopy(document["models"]["sol"])
        line["wire_model"] = "gpt-5.6-sol-two"
        for effort, spec in line["efforts"].items():
            spec["selector"] = f"gpt-multi-sol2-{effort}[1m]"
        document["models"]["sol2"] = line
        models_path.write_text(json.dumps(document, indent=2) + "\n")
        self.runtime = self.make_runtime(asset_root=root)

    def assert_catalog_attention_only(self) -> None:
        problems, _info, attention = cli._collect_doctor_reports(self.runtime)
        self.assertEqual(problems, [])
        self.assertEqual(
            attention,
            [f"session {self.mid[:8]}: fence/picker differ from the installed catalog (catalog "
             "changed since launch); applies at the next resume"],
        )

    def test_then_the_catalog_gains_a_line(self) -> None:
        self.postmodel(self.row_b)
        self.live_set()
        self.postmodel(self.row_c)
        self.gain_a_lead_class_line()
        self.assert_catalog_attention_only()

    def test_then_a_live_apply_under_the_gained_catalog(self) -> None:
        # (d) plus one more step: the live apply states lineup.md against the
        # proven launch-time lead set exactly as doctor/converge do, so it
        # never lands in BLOCK and --repair moves no scope byte.
        self.postmodel(self.row_b)
        self.live_set()
        self.postmodel(self.row_c)
        self.gain_a_lead_class_line()
        lineup_md = (self.live(self.mid) / "lineup.md").read_bytes()
        code, out, _err = self.request("set reviewer=qwen38")
        self.assertEqual(code, 0, out)
        self.assertIn("next: type /reload-plugins", out)  # the next-step line
        self.assertEqual(self.store.load(self.mid)["lineup_generation"], 3)
        after = (self.live(self.mid) / "lineup.md").read_bytes()
        self.assertEqual(
            [line for line in after.splitlines() if b"lead set (" in line],
            [line for line in lineup_md.splitlines() if b"lead set (" in line],
        )
        self.assert_catalog_attention_only()
        before = self.scope_files()
        code, output = _run_doctor(self, "--repair", self.mid)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.scope_files(), before)
        self.assert_catalog_attention_only()


class ModelsListingTests(V4Case):
    """``models`` lists the merged v2 lines and retired keys; no composition read."""

    def _models(self) -> str:
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["models"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_lines_then_retired_keys_with_successors(self) -> None:
        local = _local_cat(retired={"opus@4.7": _retired_entry(
            "opus55", since=33, wire="claude-opus-4-7", selectors={"claude-opus-4-7[1m]": None})})
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local), \
                mock.patch.object(type(self.runtime), "compositions",
                                  new_callable=mock.PropertyMock, side_effect=AssertionError):
            text = self._models()
        self.assertNotIn("not in default", text)
        rows = [line.split("\t") for line in text.splitlines() if not line.startswith("  ")]
        keys = [row[0] for row in rows]
        self.assertEqual(keys[: len(local.lines)], sorted(local.lines))
        self.assertIn(
            ["grok46", "Grok 4.6", "generation 4.6", "OpenRouter", "class grok",
             "efforts high,xhigh", "active", "wire=x-ai/grok-4.6"],
            rows,
        )
        self.assertIn("  in-session: /model claude-multi-grok46-high · /model claude-multi-grok46-xhigh",
                      text.splitlines())
        self.assertIn(["opus@4.7", "claude-opus-4-7", "retired (catalog 33)", "successor opus55"], rows)
        self.assertIn(["muse-spark", "Muse Spark 1.3", "retired (catalog 31)", "no successor"], rows)


class MarkerAndCensusTests(_DoctorCase):
    def test_a_newer_marker_blocks_before_records_are_parsed(self) -> None:
        state.atomic_write(self.store.root / "state-version", b"5\n")
        problems, info, attention = _doctor(self)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("state marker: "))
        self.assertEqual(info, ["Sessions: 1 recorded; not inspected by this launcher."])
        self.assertEqual(attention, [])

    def test_migration_lock_and_backups_are_info_and_the_census_ignores_backups(self) -> None:
        backup = self.store.backup_path(self.mid)
        state.atomic_write(backup, b"{}\n")
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        _problems, info, _attention = _doctor(self)
        self.assertIn("migration or restore in progress (session hooks skip record writes)", info)
        self.assertIn("1 legacy backup(s) kept for claude-multi restore-2x", info)
        self.assertIn("Sessions: 1 recorded.", info)


class UserSettingsTests(_DoctorCase):
    """claude-multi selectors saved in the user's Claude settings; /cm collisions."""

    def _user_settings(self, document: dict) -> Path:
        path = Path(self.env["HOME"]) / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    def test_selectors_are_flagged_and_the_file_is_never_written(self) -> None:
        sol = self.runtime.lineup_catalog().lines["sol"]
        selector = next(iter(sol["efforts"].values()))["selector"]
        path = self._user_settings({
            "model": selector,
            "modelSettings": {"claude-multi-kimi-k3[1m]": {}, "claude-opus-4-8": {}},
        })
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        _problems, _info, attention = _doctor(self)
        self.assertIn(
            f"{path}: model holds the claude-multi selector {selector!r} (saved by a /model choice "
            f"inside a claude-multi session); plain claude cannot use it. Revert: remove model from {path}",
            attention,
        )
        self.assertTrue(any('modelSettings["claude-multi-kimi-k3[1m]"]' in line for line in attention),
                        attention)
        self.assertFalse(any("claude-opus-4-8" in line for line in attention), attention)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_saved_selectors_are_read_where_managed_sessions_save_them(self) -> None:
        """A /model choice inside a managed session is saved
        to $HOME/.claude/settings.json (the session runs without
        CLAUDE_CONFIG_DIR), so that file is checked whatever the shell names."""

        sol = self.runtime.lineup_catalog().lines["sol"]
        selector = next(iter(sol["efforts"].values()))["selector"]
        other = self.root / "other-config"
        other.mkdir()
        (other / "settings.json").write_text(json.dumps({"model": selector}))
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(other)
        _problems, _info, attention = _doctor(self)
        self.assertFalse([line for line in attention if "holds the claude-multi selector" in line], attention)
        path = self._user_settings({"model": selector})
        _problems, _info, attention = _doctor(self)
        self.assertTrue([line for line in attention if line.startswith(f"{path}: model holds")], attention)

    def test_a_canonical_anthropic_model_is_not_flagged(self) -> None:
        self._user_settings({"model": "claude-opus-4-8[1m]"})
        _problems, _info, attention = _doctor(self)
        self.assertEqual(attention, [])

    def test_fallback_and_skill_shell_switches_are_info(self) -> None:
        path = self._user_settings({"switchModelsOnFlag": False, "disableSkillShellExecution": True})
        problems, info, attention = _doctor(self)
        self.assertEqual((problems, attention), ([], []))
        self.assertIn(f"{path}: switchModelsOnFlag disables the refusal model fallback "
                      "(switchModelsOnFlag: false)", info)
        self.assertIn(f"{path}: disableSkillShellExecution disables skill shell execution; "
                      "/cm cannot run", info)

    def test_a_user_cm_skill_may_shadow_the_session_skill(self) -> None:
        skill = Path(self.env["HOME"]) / ".claude" / "skills" / "cm" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: cm\n---\n")
        _problems, _info, attention = _doctor(self)
        self.assertIn(f"a skill named cm at {skill} may shadow the session's /cm skill", attention)


class SettingsRadarTests(_DoctorCase):
    """Table-driven, names files and keys, never values."""

    def _write(self, path: Path, document: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    def test_every_table_key_in_user_and_record_cwd_settings(self) -> None:
        user = self._write(Path(self.env["HOME"]) / ".claude" / "settings.json", {
            "env": {"DISABLE_AUTO_COMPACT": "1", "MAX_THINKING_TOKENS": "secret-looking-1234"},
            "disableAllHooks": True,
            "alwaysThinkingEnabled": False,
        })
        other = self.root / "elsewhere"
        other.mkdir()
        record = self.store.load(self.mid)
        local = self._write(Path(record["cwd"]) / ".claude" / "settings.local.json", {"maxEffortLevel": "low"})
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        text = "\n".join(attention)
        self.assertIn(f"{user}: env.DISABLE_AUTO_COMPACT defeats the claude-multi session policy", text)
        self.assertIn(f"{user}: env.MAX_THINKING_TOKENS defeats", text)
        self.assertIn(f"{user}: disableAllHooks disables every hook", text)
        self.assertIn(f"{user}: alwaysThinkingEnabled turns extended thinking off", text)
        self.assertIn(f"{local}: maxEffortLevel caps every session's effort", text)
        self.assertNotIn("secret-looking-1234", text)

    def test_a_credential_key_is_attention_and_blocks_rotate_token(self) -> None:
        user = self._write(Path(self.env["HOME"]) / ".claude" / "settings.json",
                           {"env": {"ANTHROPIC_AUTH_TOKEN": "tok-value-never-printed"}})
        _problems, _info, attention = _doctor(self)
        hit = [line for line in attention if line.startswith(f"{user}: env.ANTHROPIC_AUTH_TOKEN")]
        self.assertEqual(len(hit), 1, attention)
        self.assertNotIn("tok-value-never-printed", "\n".join(attention))
        with self.assertRaises(cli.CLIError) as raised:
            cli._doctor_rotate_token(self.runtime, input_stream=io.StringIO(),
                                     output_stream=io.StringIO(), interactive=True)
        self.assertIn(f"{user}: env.ANTHROPIC_AUTH_TOKEN", str(raised.exception))
        self.assertNotIn("tok-value-never-printed", str(raised.exception))

    def test_claude_config_dir_is_not_the_managed_user_layer(self) -> None:
        """The radar and the --rotate-token credential gate
        read the user layer managed sessions apply, $HOME/.claude, never a
        $CLAUDE_CONFIG_DIR in the launcher's environment."""

        other = self.root / "other-config"
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(other)
        decoy = self._write(other / "settings.json",
                            {"env": {"ANTHROPIC_AUTH_TOKEN": "tok-value-never-printed"}, "disableAllHooks": True})
        _problems, _info, attention = _doctor(self)
        self.assertFalse([line for line in attention if str(decoy) in line], attention)
        user = self._write(Path(self.env["HOME"]) / ".claude" / "settings.json",
                           {"env": {"ANTHROPIC_AUTH_TOKEN": "tok-value-never-printed"}})
        _problems, _info, attention = _doctor(self)
        self.assertEqual(len([line for line in attention if line.startswith(f"{user}: env.ANTHROPIC_AUTH_TOKEN")]),
                         1, attention)
        self.assertFalse([line for line in attention if str(decoy) in line], attention)
        with self.assertRaises(cli.CLIError) as raised:
            cli._doctor_rotate_token(self.runtime, input_stream=io.StringIO(),
                                     output_stream=io.StringIO(), interactive=True)
        self.assertIn(f"{user}: env.ANTHROPIC_AUTH_TOKEN", str(raised.exception))
        self.assertNotIn(str(other), str(raised.exception))

    def test_the_subagent_model_line_keeps_its_wording(self) -> None:
        user = self._write(Path(self.env["HOME"]) / ".claude" / "settings.json",
                           {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "x"}})
        _problems, _info, attention = _doctor(self)
        self.assertTrue(any(line.startswith(f"CLAUDE_CODE_SUBAGENT_MODEL is set in {user} — ")
                            for line in attention), attention)

    def test_provider_secret_names_in_the_launcher_env_are_info_names_only(self) -> None:
        self.runtime.environ["KIMI_CLAUDE_API_KEY"] = "value-never-printed"
        _problems, info, _attention = _doctor(self)
        line = next(line for line in info if "provider secret name(s) present" in line)
        self.assertTrue(line.startswith("1 provider secret name(s) present in the launcher env (KIMI_CLAUDE_API_KEY)"))
        self.assertIn("stop exporting them where your environment sets them", line)
        self.assertNotIn(".profile", line)
        self.assertNotIn("value-never-printed", "\n".join(info))


class DoctorFactsTests(_DoctorCase):
    """The doctor facts with typed findings: names and states, never a value,
    a path or the HOME in the JSON report."""

    def json_report(self) -> dict:
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            cli.main(["doctor", "--json"], runtime=self.runtime, output_stream=out, interactive=False)
        return json.loads(out.getvalue())

    def codes(self, document: dict) -> dict[str, dict]:
        return {item["code"]: item for item in document["diagnostics"]}

    def test_an_exported_provider_secret_is_a_typed_finding_without_its_value(self) -> None:
        self.runtime.environ["KIMI_CLAUDE_API_KEY"] = "value-never-printed"
        document = self.json_report()
        finding = self.codes(document)["provider-secret-exported"]
        self.assertEqual(finding["severity"], "info")
        self.assertNotIn("value-never-printed", json.dumps(document))

    def test_an_ignored_claude_config_dir_is_named_with_its_consequences(self) -> None:
        moved = Path(self.env["HOME"]) / "claude-elsewhere"
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(moved)
        _problems, _info, attention = _doctor(self)
        line = next(line for line in attention if line.startswith("CLAUDE_CONFIG_DIR is set"))
        self.assertIn("(~/claude-elsewhere)", line)
        for consequence in ("settings", "MCP servers", "memory", "resumable sessions"):
            self.assertIn(consequence, line)
        document = self.json_report()
        self.assertEqual(self.codes(document)["claude-config-dir-ignored"]["severity"], "attention")
        self.assertNotIn("claude-elsewhere", json.dumps(document))

    def test_session_env_keep_names_kept_stripped_and_refused_variables(self) -> None:
        self.runtime.environ.update({"FIXTURE_TOOL_API_KEY": "kept-value", "OTHER_TOOL_API_KEY": "other-value",
                                     "KIMI_CLAUDE_API_KEY": "provider-value"})
        with mock.patch.object(type(self.runtime), "session_env_keep",
                               return_value=("FIXTURE_TOOL_API_KEY", "KIMI_CLAUDE_API_KEY")):
            _problems, info, attention = _doctor(self)
            document = self.json_report()
        line = next(line for line in info if line.startswith("Session environment:"))
        self.assertIn("keep FIXTURE_TOOL_API_KEY (session_env_keep)", line)
        self.assertIn("OTHER_TOOL_API_KEY", line.split("do not get", 1)[1])
        self.assertTrue(any(line.startswith("session_env_keep lists KIMI_CLAUDE_API_KEY, but managed sessions "
                                            "remove it") for line in attention), attention)
        text = json.dumps(document) + "\n".join(info + attention)
        for value in ("kept-value", "other-value", "provider-value"):
            self.assertNotIn(value, text)

    def test_a_new_shipped_model_names_its_admission_path(self) -> None:
        root = self.root / "assets-new"
        shutil.copytree(FIXTURE_ROOT, root)
        models_path = root / "catalog" / "models.json"
        document = json.loads(models_path.read_text())
        source = next(key for key in sorted(document["models"])
                      if document["models"][key]["provider"] == "kimi")
        line = copy.deepcopy(document["models"][source])
        line["status"] = "new"
        line["display"] = "Fixture New Line"
        line["wire_model"] = "fixture-new-wire"
        if isinstance(line.get("efforts"), dict):
            for spec in line["efforts"].values():
                spec["selector"] = "fixture-new-" + spec["selector"]
        if "selector" in line:
            line["selector"] = "fixture-new-" + line["selector"]
        document["models"]["fixture-new"] = line
        models_path.write_text(json.dumps(document, indent=2) + "\n")
        self.runtime = self.make_runtime(asset_root=root)
        problems, _info, attention = _doctor(self)
        self.assertFalse(any("fixture-new" in problem for problem in problems))
        self.assertIn("New model: Fixture New Line (fixture-new) — not admitted (optional badge): "
                      "claude-multi models admit fixture-new (or Models, then Enter; use does not require it)", attention)
        finding = self.codes(self.json_report())["model-newly-available"]
        self.assertEqual((finding["subject_id"], finding["remedy"]),
                         ("fixture-new", "claude-multi models admit fixture-new"))

    def test_a_deny_that_only_the_launch_environment_selects_is_attention(self) -> None:
        live = self.live(self.mid)
        doc = strict_json.loads((live / "settings.json").read_bytes())
        doc["permissions"]["deny"].append("Read(//fixture-moved-config/anthropic/**)")
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
        problems, _info, attention = _doctor(self)
        self.assertFalse([line for line in problems if self.m8 in line and "scope differs" in line], problems)
        line = next(line for line in attention if line.startswith(f"session {self.m8}: its secret-file denies"))
        self.assertIn("the launching shell added 1 rule(s) this shell does not select", line)
        self.assertIn(f"claude-multi doctor --repair {self.mid}", line)
        # Any other settings difference stays damage.
        doc["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
        problems, _info, _attention = _doctor(self)
        self.assertTrue(any(line.startswith(f"session {self.m8}: scope differs") for line in problems), problems)

    def test_a_key_file_pair_only_the_launch_environment_selects_is_attention(self) -> None:
        # Launched where CLAUDE_MULTI_SECRET_ENV selects a key file outside
        # the protected folders: the real generator denies it Read and Edit.
        live = self.live(self.mid)
        denies = set(strict_json.loads((live / "settings.json").read_bytes())["permissions"]["deny"])
        pair = {rule for rule in scope.secret_path_denies(self.env) if rule not in
                scope.secret_path_denies({k: v for k, v in self.env.items() if k != "CLAUDE_MULTI_SECRET_ENV"})}
        self.assertEqual({rule.split("(", 1)[0] for rule in pair}, {"Read", "Edit"})
        self.assertLessEqual(pair, denies)
        # Doctor from a shell that does not select that file.
        environ = {k: v for k, v in self.env.items() if k != "CLAUDE_MULTI_SECRET_ENV"}
        problems, _info, attention = _doctor(self, self.make_runtime(environ=environ))
        self.assertFalse([line for line in problems if self.m8 in line and "scope differs" in line], problems)
        line = next(line for line in attention if line.startswith(f"session {self.m8}: its secret-file denies"))
        self.assertIn("the launching shell added 2 rule(s) this shell does not select", line)
        # An Edit rule without its Read is not the generator's: damage.
        doc = strict_json.loads((live / "settings.json").read_bytes())
        doc["permissions"]["deny"] = [rule for rule in doc["permissions"]["deny"]
                                      if not (rule in pair and rule.startswith("Read("))]
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
        problems, _info, _attention = _doctor(self, self.make_runtime(environ=environ))
        self.assertTrue(any(line.startswith(f"session {self.m8}: scope differs") for line in problems), problems)

    def test_quota_health_names_the_source_of_the_recorded_backend(self) -> None:
        _problems, info, _attention = _doctor(self)
        line = next(line for line in info if line.startswith("Quota: unavailable in this build"))
        self.assertIn(quota.health_source(service.backend_of(self.runtime.home).name), line)
        self.assertNotIn("when supervised", line)

    def test_certificate_paths_are_home_relative(self) -> None:
        bundle = Path(self.env["HOME"]) / "certs" / "corp.pem"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("fixture\n")
        self.runtime.environ["SSL_CERT_FILE"] = str(bundle)
        _problems, info, _attention = _doctor(self)
        line = next(line for line in info if line.startswith("HTTPS trust ("))
        self.assertIn("SSL_CERT_FILE=~/certs/corp.pem", line)
        self.assertNotIn(self.env["HOME"], line)

    def test_relative_certificate_paths_are_shown_like_every_other_path(self) -> None:
        import contextlib

        home = Path(self.env["HOME"])
        (home / "certs").mkdir(parents=True)
        (home / "certs" / "corp.pem").write_text("fixture\n")
        # The directory is a prefix of the file: each value is formatted
        # whole, never by replacing text inside the line.
        self.runtime.environ["SSL_CERT_FILE"] = "certs/corp.pem"
        self.runtime.environ["SSL_CERT_DIR"] = "certs"
        with contextlib.chdir(home):  # where the launcher opens a relative path
            _problems, info, _attention = _doctor(self)
        line = next(line for line in info if line.startswith("HTTPS trust (update, Claude Code download): "))
        self.assertIn("SSL_CERT_FILE=~/certs/corp.pem and SSL_CERT_DIR=~/certs", line)
        self.assertNotIn("(missing)", line)
        self.assertNotIn("=certs", line)

    def test_detail_lines_need_verbose_and_findings_never_hide(self) -> None:
        code, plain = _run_doctor(self)
        self.assertEqual(code, 0, plain)
        self.assertNotIn(f"Scope: {self.m8} OK", plain)
        self.assertRegex(plain, r"\(\d+ more detail lines?: claude-multi doctor -v\)")
        code, verbose = _run_doctor(self, "-v")
        self.assertEqual(code, 0, verbose)
        self.assertIn(f"Scope: {self.m8} OK", verbose)
        self.assertNotIn("more detail line", verbose)
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / "elsewhere")
        code, plain = _run_doctor(self)
        self.assertEqual(code, 0, plain)
        self.assertEqual(plain.splitlines()[0], "Attention")
        self.assertNotIn("Ready", plain.splitlines())
        self.assertIn("CLAUDE_CONFIG_DIR is set", plain)
        self.assertIn("Nothing blocks a launch", plain)

    def test_the_json_environment_block_is_allowlisted(self) -> None:
        self.runtime.environ["TERM"] = "fixture-terminal-with-a-secret"
        document = self.json_report()
        environment = document["environment"]
        self.assertEqual(set(environment), {"schema", "platform", "machine", "wsl", "python", "terminal",
                                            "channel", "release", "roots"})
        self.assertEqual(environment["terminal"]["term"], "other")
        self.assertEqual(set(environment["roots"]), {"config", "state", "claude_settings", "gateway_set_up"})
        text = json.dumps(document)
        self.assertNotIn(self.env["HOME"], text)
        self.assertNotIn("fixture-terminal-with-a-secret", text)


class RetirementRadarTests(_DoctorCase):
    """The retirement radar over a test-local copy of the pinned-registry fixture."""

    def _registry(self, retirement_at: str | None) -> None:
        root = self.root / "registry"
        shutil.copytree(REGISTRY_FIXTURE, root)
        path = root / "codex_client_models.json"
        document = json.loads(path.read_text())
        wire = self.runtime.lineup_catalog().lines["gpt55"]["wire_model"]
        entry = next(item for item in document["models"] if item.get("slug") == wire)
        entry["upgrade"] = {"model": "gpt-successor", "retirement_at": retirement_at}
        entry.pop("retirement_at", None)
        path.write_text(json.dumps(document))
        self.runtime.environ[catalog.REGISTRY_DIR_ENV] = str(root)
        self.wire = wire

    def _now(self, value: str):
        return mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: datetime.fromisoformat(value))

    def test_a_retirement_within_30_days_is_attention(self) -> None:
        self._registry("2026-10-14T19:00:00Z")
        with self._now("2026-09-26T00:00:00+00:00"):
            problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertIn(
            f"line gpt55 (catalog): wire {self.wire} has an upstream retirement date "
            "2026-10-14T19:00:00Z (in 18 day(s); inferred from the pinned registry — upstream may "
            "refuse it after); upstream names gpt-successor as its upgrade",
            attention,
        )

    def test_a_distant_or_missing_date_says_nothing(self) -> None:
        for date in ("2027-06-01T00:00:00Z", None):
            with self.subTest(date=date):
                self._registry(date)
                with self._now("2026-09-26T00:00:00+00:00"):
                    _problems, _info, attention = _doctor(self)
                self.assertFalse([line for line in attention if "retirement date" in line], attention)
                shutil.rmtree(self.root / "registry")

    def test_no_registry_in_this_build_skips_the_radar(self) -> None:
        self.runtime.environ.pop(catalog.REGISTRY_DIR_ENV, None)
        with mock.patch.object(catalog, "registry_dir", return_value=None):
            self.assertEqual(cli._doctor_retirement_radar(self.runtime), [])


class JournalPassTests(_DoctorCase):
    """Fixture journal text; the host journal is never read."""

    NOW = datetime(2026, 9, 26, 12, 0, 0).astimezone()

    def _ts(self, minutes_ago: int) -> str:
        return (self.NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def _report(self, text: str) -> list[str]:
        with mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: self.NOW.astimezone(timezone.utc)):
            return cli._doctor_journal_report(self.runtime, text)

    def test_the_default_reader_never_runs_with_injected_seams(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            self.assertIsNone(cli._read_gateway_journal(self.runtime))
        run.assert_not_called()

    def test_sanitized_real_cat_format_matches_and_nonmatches(self) -> None:
        # B063: synthesized, never captured from a live journal. Pinned
        # CLIProxyAPI 7.3.15 source qmzgbddl997jjf2q8fnf81y30hm5lp22-source:
        # internal/logging/global_logger.go LogFormatter.Format (brackets,
        # local timestamp, padded level); gin_logger.go GinLogrusLogger
        # (status/latency/IP/method); sdk/cliproxy/auth/selector.go:1109 and
        # conductor_refresh.go:550 (message templates). All ids are fake.
        text = (REPO_ROOT / "tests/fixtures/journal/gateway-cat.log").read_text()
        lines = text.splitlines()
        with mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: self.NOW.astimezone(timezone.utc)):
            facts = cli._journal_facts(self.runtime, text)
            clean = cli._journal_facts(self.runtime, "\n".join([*lines[2:4], lines[5]]))
        self.assertEqual(facts.quota, {("fixture-quota-model", "403"): 1})
        self.assertEqual(facts.dead_pools, {"codex": (1, datetime(2026, 9, 26, 11, 32))})
        self.assertIsNone(facts.unattributed)
        self.assertEqual(clean, cli.JournalFacts({}, None, {}))
        report = self._report(text)
        self.assertEqual(len(report), 2, report)
        self.assertTrue(any("model fixture-quota-model got 1× 403" in line for line in report))
        self.assertTrue(any("1 invalid_grant line(s) since the codex pool" in line for line in report))
        self.assertNotIn("fixture-auth", "\n".join(report))
        self.assertNotIn("fixture-healthy-model", "\n".join(report))

    def test_per_model_429_402_403(self) -> None:
        lines = []
        for n in range(21):
            rid = f"r{n:07d}"
            lines.append(f"[{self._ts(10)}] [{rid}] [info ] [selector.go:468] session-affinity: cache miss | "
                         "session=abc auth=x provider=codex model=gpt-multi-sol-high[1m]")
            lines.append(f"[{self._ts(10)}] [{rid}] [warn ] [gin_logger.go:99] 429 |  1.2s | 127.0.0.1 | POST \"/v1/messages\"")
        lines.append(f"[{self._ts(300)}] [pay00001] [info ] [selector.go:468] x | model=claude-multi-kimi-k3[1m]")
        lines.append(f"[{self._ts(300)}] [pay00001] [warn ] [gin_logger.go:99] 402 |  0.1s | 127.0.0.1 | POST \"/v1/messages\"")
        # old 429s (over an hour) never count toward the hourly threshold
        for n in range(30):
            lines.append(f"[{self._ts(120)}] [old{n:05d}] [warn ] [gin_logger.go:99] 429 |  1s | x | POST \"/v1\"")
        report = self._report("\n".join(lines))
        self.assertIn(
            "gateway journal: model gpt-multi-sol-high[1m] got 21× 429 (rate limit / quota) in the last hour "
            "— quota pressure on its provider; choose a profile that does not depend on this provider. "
            "For an existing session, use /cm or Sessions -> T."
            " — a session may sit silently in a Retry-After wait of up to 6 h (watchdog); "
            "a foreground agent then blocks its lead",
            report,
        )
        self.assertTrue(any("model claude-multi-kimi-k3[1m] got 1× 402 (payment required) in the last 24 h" in line
                            for line in report), report)
        self.assertFalse(any("unattributed" in line for line in report), report)
        self.assertFalse(any("Retry-After" in line for line in report if "402" in line or "403" in line))

    def test_invalid_grant_after_the_credential_mtime(self) -> None:
        auth = self.runtime.home / self.runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        auth.mkdir(parents=True, exist_ok=True)
        cred = auth / "codex-user.json"
        cred.write_text("{}")
        written = (self.NOW - timedelta(hours=2)).timestamp()
        os.utime(cred, (written, written))
        text = "\n".join([
            f"[{self._ts(180)}] [--------] [error] [conductor.go:1] codex refresh failed: invalid_grant",
            f"[{self._ts(30)}] [--------] [error] [conductor.go:1] codex refresh failed: invalid_grant",
        ])
        report = self._report(text)
        self.assertEqual(len(report), 1, report)
        self.assertTrue(report[0].startswith("gateway journal: 1 invalid_grant line(s) since the codex pool "
                                             "credential was written"))
        self.assertTrue(report[0].endswith("sign in again: claude-multi providers sign-in openai"))

    def test_an_unattributed_invalid_grant_is_one_line_naming_the_candidates(self) -> None:
        auth = self.runtime.home / self.runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        auth.mkdir(parents=True, exist_ok=True)
        written = (self.NOW - timedelta(hours=2)).timestamp()
        for name in ("codex-user.json", "claude-user.json"):
            (auth / name).write_text("{}")
            os.utime(auth / name, (written, written))
        text = f"[{self._ts(30)}] [--------] [error] [auth.go:1] token refresh failed: invalid_grant"
        report = self._report(text)
        self.assertEqual(len(report), 1, report)
        self.assertTrue(report[0].startswith("gateway journal: 1 invalid_grant line(s) naming no pool "), report)
        self.assertIn("candidates: claude, codex;", report[0])
        self.assertTrue(report[0].endswith(
            "claude-multi providers sign-in anthropic or claude-multi providers sign-in openai"), report)
        self.assertNotIn("the refresh token is dead", report[0])
        # older than every credential write: stale, nothing reported
        self.assertEqual(self._report(
            f"[{self._ts(180)}] [--------] [error] [auth.go:1] token refresh failed: invalid_grant"), [])


class QuotaDoctorTests(_DoctorCase):
    """Optional quota never blocks or hides independent doctor facts."""

    def setUp(self):
        super().setUp()
        self.runtime.doctor_callback = None
        self.runtime.doctor_served_callback = mock.Mock(return_value=([], ["fixture served check"]))
        self.get = mock.Mock(return_value=(200, (QUOTA_FIXTURES / "auth-files-mixed.json").read_bytes()))
        self.runtime.management_callback = self.get
        # Quota reads exist only on the management channel (the Nix wrapper's).
        self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL
        self.now = QUOTA_NOW
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(quota, "_now", return_value=self.now).start()
        mock.patch('claude_multi.cli.gateway_facts._doctor_now', return_value=self.now).start()
        self.journal = mock.patch('claude_multi.cli.gateway_facts._read_gateway_journal', return_value="").start()
        self.restart = cli.gateway_service_hint("restart")

    def test_management_follows_the_channel_whatever_the_backend(self):
        port = endpoint.port_of(self.runtime.catalog.docs["gateway"]["gateway"]["base_url"])
        management.stage_rotation(self.runtime.home)
        for backend in (endpoint.ON_DEMAND, endpoint.SYSTEMD):
            with self.subTest(backend=backend):
                endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=port, backend=backend))
                # The health source named is the recorded backend's own.
                unavailable = quota.state_text(quota.PoolStatus("unavailable"), restart_hint=self.restart,
                                               backend=backend)
                # The management channel keeps its key state on either backend.
                self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL
                staged, info = cli._management_key_attention(self.runtime)
                self.assertIsNone(info)
                self.assertIn("management key rotation staged since", staged)
                self.assertIn(self.restart, staged)
                # Any other channel has no management, whatever the backend.
                for channel in ("bundle", None):
                    if channel is None:
                        self.runtime.environ.pop(management.CHANNEL_ENV)
                    else:
                        self.runtime.environ[management.CHANNEL_ENV] = channel
                    self.assertEqual(cli._management_key_attention(self.runtime), (None, unavailable))
                    problems, info, attention = self.report()
                    self.assertIn(unavailable, info)
                    self.assertFalse([line for line in attention if "management key" in line])
        self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL

    def enable(self):
        management.prepare_start(self.runtime.home, stopped=True)

    def report(self):
        self.runtime._pool_cache = None
        return _doctor(self)

    def grant(self, pool="claude"):
        stamp = (self.now - timedelta(minutes=10)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{stamp}] [--------] [error] [conductor.go:1] {pool} refresh failed: invalid_grant"

    def test_healthy_management_preserves_journal_refresh_failure(self):
        self.enable()
        self.get.return_value = (200, (QUOTA_FIXTURES / "auth-files-refresh-failing-active.json").read_bytes())
        self.journal.return_value = self.grant()
        problems, info, attention = self.report()
        self.assertEqual(problems, [])
        self.assertTrue(any("claude#1 5h 42%" in line for line in info))
        self.assertTrue(any("invalid_grant line(s) since the claude pool" in line for line in attention))
        self.get.assert_called_once()
        self.journal.assert_called_once_with(self.runtime)

    def test_only_equivalent_invalid_grant_dedupes(self):
        self.enable()
        self.journal.return_value = "\n".join([self.grant(), self.grant("codex"), self.grant("")])
        for reason in ("invalid_grant", "unauthorized", "token expired", "payment_required", "quota exhausted", ""):
            with self.subTest(reason=reason):
                self.get.return_value = (200, json.dumps({"files": [{
                    "provider": "claude", "status": "error", "status_message": reason,
                }]}).encode())
                problems, _info, attention = self.report()
                self.assertEqual(problems, [])
                self.assertEqual(any("invalid_grant line(s) since the claude pool" in line for line in attention),
                                 reason != "invalid_grant")
                self.assertTrue(any("invalid_grant line(s) since the codex pool" in line for line in attention))
                self.assertTrue(any("invalid_grant line(s) naming no pool" in line for line in attention))

    def test_management_states_are_attention_only(self):
        self.enable()
        for code, body, expected in (
            (401, b"", "mismatch"), (403, b"", "refused"), (404, b"", "management-off"),
            (500, b"", "error"), (200, b"invalid JSON", "malformed"),
        ):
            with self.subTest(code=code):
                self.get.return_value = code, body
                problems, info, attention = self.report()
                self.assertEqual(problems, [])
                text = quota.state_text(quota.PoolStatus(expected, code), restart_hint=self.restart)
                self.assertEqual(attention, [text])
                self.assertNotIn(text, info)
        self.get.side_effect = RuntimeError("private upstream body SENTINEL")
        problems, _info, attention = self.report()
        self.assertEqual(problems, [])
        self.assertEqual(attention, ["quota: the management read failed"])
        self.get.side_effect = ConnectionRefusedError()
        self.assertEqual(self.report()[2], [])  # readiness owns down diagnostics

    def test_all_credential_levels_reach_attention_without_pii_or_block(self):
        self.enable()
        for entry, fragment in (
            ({"status_message": "unauthorized"}, "credential rejected by the provider (unauthorized)"),
            ({"status_message": "token expired"}, "is unusable (access token expired and not refreshed)"),
            ({"status_message": "payment_required"}, "refused by the provider (payment required or forbidden)"),
            ({"status_message": "quota exhausted"}, None),
            ({"quota": {"observed_at": self.now.isoformat(), "signals": {
                "anthropic-ratelimit-unified-5h-utilization": "0.93"}}},
             "at 93% of its 5h window (reset unknown; observed just now; passive)"),
            ({"disabled": True}, None), ({}, None),
        ):
            with self.subTest(entry=entry):
                self.get.return_value = 200, json.dumps({"files": [{"provider": "claude", **entry}]}).encode()
                problems, info, attention = self.report()
                self.assertEqual(problems, [])
                self.assertEqual(len(attention), 1 if fragment else 0)
                if fragment:
                    self.assertIn(fragment, attention[0])
                self.assertTrue(any(line.startswith("Quota (local gateway, passive):") for line in info))
        self.get.return_value = 200, (QUOTA_FIXTURES / "auth-files-mixed.json").read_bytes()
        report = repr(self.report())
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, report)

    def test_local_key_states_are_reported_once_even_without_readiness(self):
        cases = [
            (lambda: None, "no-key", False),
            (lambda: management.disable(self.runtime.home), "disabled", False),
            (lambda: management.stage_rotation(self.runtime.home), "staged", True),
            (lambda: state.atomic_write(management.key_dir(self.runtime.home) / management.KEY_FILE,
                                       b"bad-shape-SENTINEL\n"), "key-unusable", True),
        ]
        for setup, status, is_attention in cases:
            setup()
            for readiness in (True, False):
                with self.subTest(status=status, readiness=readiness):
                    with mock.patch.object(self.runtime, "check_readiness",
                                           side_effect=None if readiness else launch.LaunchError("fixture down"),
                                           return_value="fixture-token"):
                        problems, info, attention = self.report()
                    self.assertEqual(bool(problems), not readiness)
                    expected_attention, expected_info = cli._management_key_attention(self.runtime)
                    expected = expected_attention if is_attention else expected_info
                    self.assertIsNotNone(expected)
                    self.assertEqual((attention + info).count(expected), 1)
                    self.assertIn(expected, attention if is_attention else info)
                    self.assertEqual(len([line for line in attention + info if
                        "management-key" in line or "management key" in line or "Quota: disabled" in line]), 1)
                    self.assertNotIn("bad-shape-SENTINEL", repr((problems, info, attention)))
        self.get.assert_not_called()

    @contextmanager
    def _foreign(self, *paths):
        """Report another uid for these paths only (no real chown needed)."""
        lstat, targets = os.lstat, {str(p) for p in paths}
        def fake(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if os.fspath(path) not in targets:
                return info
            fields = list(info)
            fields[4] = os.geteuid() + 1  # st_uid
            return os.stat_result(fields)
        # pathlib's lstat delegates to different os APIs across Python versions.
        with mock.patch.object(os, "lstat", fake), mock.patch.object(Path, "lstat", fake):
            yield

    def test_foreign_fixture_agrees_across_stat_apis(self):
        target = self.root / "foreign"
        target.touch()
        ordinary = self.root / "ordinary"
        ordinary.touch()
        # Python 3.11's pathlib uses stat(follow_symlinks=False), not os.lstat.
        with mock.patch.object(Path, "lstat", lambda path: path.stat(follow_symlinks=False)):
            with self._foreign(target):
                for read in (os.lstat, Path.lstat):
                    self.assertEqual(read(target).st_uid, os.geteuid() + 1)
                    self.assertEqual(read(ordinary).st_uid, os.geteuid())
        self.assertEqual(target.lstat().st_uid, os.geteuid())

    def _execute_remedy(self, remedy):
        # Run the exact printed procedure; a fixture-only shell function maps
        # the printed proxy command to the entry point copied into an
        # installed-style tree (a source checkout's entry names the source
        # channel, which has no management), with the Nix wrapper's channel.
        installed = self.root / "installed" / "bin" / "claude-multi-proxy"
        installed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / "bin/claude-multi-proxy", installed)
        script = 'claude-multi-proxy() { "$PYTHON" "$PROXY" "$@"; }; ' + remedy
        result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True,
            env={**self.runtime.environ, "PATH": os.environ.get("PATH", ""),
                 "PYTHON": sys.executable, "PROXY": str(installed),
                 "PYTHONPATH": str(REPO_ROOT / "src"),
                 management.PINNED_BIN_ENV: "/fixture/gateway",
                 management.PATCHES_ENV: management.ALLOWLIST_PATCH,
                 management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unusable_key_prints_and_executes_category_safe_repairs(self):
        directory = management.key_dir(self.runtime.home)
        active = directory / management.KEY_FILE
        target = self.root / "untouched-management-target"
        target.write_text("DO-NOT-FOLLOW-SENTINEL")
        for kind in ("shape", "mode", "symlink", "directory", "foreign"):
            with self.subTest(kind=kind):
                self.enable()
                if kind == "shape":
                    state.atomic_write(active, b"DO-NOT-PRINT-SENTINEL")
                elif kind == "mode":
                    active.chmod(0o644)
                elif kind != "foreign":
                    active.unlink()
                    active.symlink_to(target) if kind == "symlink" else active.mkdir()
                with self._foreign(active if kind == "foreign" else target):
                    problems, _info, attention = self.report()
                self.assertEqual(problems, [])
                self.assertEqual(len(attention), 1)
                text = attention[0]
                self.assertTrue(text.startswith("quota: management-key is unusable ("))
                self.assertNotIn("SENTINEL", text)
                remedy = text.split(" — ", 1)[1].split("; then restart the gateway:", 1)[0]
                if kind == "foreign":
                    # Disable cannot remove a foreign-owned file, so
                    # its repair must not route through disable-management-key.
                    self.assertIn("(foreign owner)", text)
                    self.assertNotIn("disable-management-key", remedy)
                self._execute_remedy(remedy)
                self.assertIsNone(management.read_active(self.runtime.home))
                self.enable()  # fixture-only stopped start selects the repaired slot
                self.assertIsNotNone(management.read_active(self.runtime.home))
                self.assertEqual(target.read_text(), "DO-NOT-FOLLOW-SENTINEL")
        self.get.assert_not_called()

    def test_disable_repairs_name_the_refused_slot_and_keep_disabling(self):
        # A marker that is a link/directory, or a foreign-owned slot,
        # is classified per path; the repair continues the disable.
        directory = management.key_dir(self.runtime.home)
        target = self.root / "untouched-marker-target"
        target.write_text("DO-NOT-FOLLOW-SENTINEL")
        for kind, filename, reason in (
            ("symlink", management.DISABLED_FILE, "symlink"),
            ("directory", management.DISABLED_FILE, "not a regular file"),
            ("foreign", management.KEY_FILE, "foreign owner"),
        ):
            with self.subTest(kind=kind):
                self.enable()
                marker = directory / management.DISABLED_FILE
                if kind == "symlink":
                    marker.symlink_to(target)
                elif kind == "directory":
                    marker.mkdir()
                with self._foreign(directory / filename if kind == "foreign" else target), \
                     self.assertRaises(management.ManagementKeyError) as caught:
                    management.disable(self.runtime.home)
                self.assertEqual((caught.exception.filename, caught.exception.reason), (filename, reason))
                remedy = caught.exception.remedy
                self.assertTrue(remedy.endswith("&& claude-multi-proxy disable-management-key"), remedy)
                self.assertNotIn("rotate-management-key", remedy)
                self.assertIn(f'"$HOME/.config/claude-multi/{filename}"', remedy)
                # Interrupted after the repair step and before the
                # retried disable, reads are still off (the selection is gone).
                repair, retry = remedy.rsplit(" && ", 1)
                self.assertEqual(retry, "claude-multi-proxy disable-management-key")
                self._execute_remedy(repair)
                self.assertIsNone(management.read_active(self.runtime.home))
                self._execute_remedy(retry)
                self.assertTrue(management.key_state(self.runtime.home).disabled)
                self.assertTrue(marker.is_file() and not marker.is_symlink())
                self.assertFalse(any((directory / name).exists() for name in
                                     (management.KEY_FILE, management.STAGED_FILE, management.PREPARED_FILE)))
                self.assertEqual(target.read_text(), "DO-NOT-FOLLOW-SENTINEL")
                management.stage_rotation(self.runtime.home)  # re-enable for the next case
        self.get.assert_not_called()

    def test_disable_write_failure_keeps_disable_intent(self):
        # An I/O failure (ENOSPC) while disabling must never print
        # a repair that ends by re-enabling reads.
        directory = management.key_dir(self.runtime.home)
        write = state.atomic_write
        for failing in (management.PREPARED_FILE, management.DISABLED_FILE):
            with self.subTest(failing=failing):
                self.enable()
                self.assertIsNotNone(management.read_active(self.runtime.home))
                def full(path, data, _failing=failing):
                    if Path(path).name == _failing:
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return write(path, data)
                with mock.patch.object(state, "atomic_write", full), \
                     self.assertRaises(management.ManagementKeyError) as caught:
                    management.disable(self.runtime.home)
                self.assertEqual(caught.exception.reason, "write failed")
                self.assertIn("could not be written; management reads may still be on", str(caught.exception))
                remedy = caught.exception.remedy
                self.assertNotIn("rotate-management-key", remedy)
                self.assertTrue(remedy.endswith("then claude-multi-proxy disable-management-key"), remedy)
                if failing == management.DISABLED_FILE:
                    # The selection was invalidated before the marker write.
                    self.assertIsNone(management.read_active(self.runtime.home))
                self._execute_remedy(remedy.rsplit("then ", 1)[1])  # the cause is gone: retry
                self.assertTrue(management.key_state(self.runtime.home).disabled)
                self.assertIsNone(management.read_active(self.runtime.home))
                self.assertFalse((directory / management.KEY_FILE).exists())
                management.stage_rotation(self.runtime.home)  # re-enable for the next case
        self.get.assert_not_called()

    def test_unusable_staged_slot_prints_its_repair_not_a_restart(self):
        # Start preparation refuses and keeps a corrupt .next, so a
        # restart alone would leave the same staged line forever.
        directory = management.key_dir(self.runtime.home)
        staged = directory / management.STAGED_FILE
        target = self.root / "untouched-staged-target"
        target.write_text("DO-NOT-FOLLOW-SENTINEL")
        self.get.return_value = (200, b'{"files": []}')  # the selected active key still reads
        for kind, reason in (("shape", "invalid shape"), ("mode", "unsafe file"), ("symlink", "symlink"),
                             ("directory", "not a regular file"), ("foreign", "foreign owner")):
            with self.subTest(kind=kind):
                self.enable()
                selected = management.read_active(self.runtime.home)
                management.stage_rotation(self.runtime.home)
                if kind == "shape":
                    state.atomic_write(staged, b"DO-NOT-PRINT-SENTINEL")
                elif kind == "mode":
                    staged.chmod(0o644)
                elif kind in ("symlink", "directory"):
                    staged.unlink()
                    staged.symlink_to(target) if kind == "symlink" else staged.mkdir()
                with self._foreign(staged if kind == "foreign" else target):
                    self.assertEqual(management.prepare_start(self.runtime.home, stopped=True),
                                     ("staged-unusable",))
                    problems, _info, attention = self.report()
                self.assertEqual(problems, [])
                self.assertEqual(len(attention), 1, attention)
                text = attention[0]
                self.assertTrue(text.startswith(f"quota: management-key.next is unusable ({reason}) — "), text)
                self.assertNotIn("rotation staged", text)
                self.assertNotIn("SENTINEL", text)
                # The active selection is untouched by a corrupt staged slot.
                self.assertEqual(management.read_active(self.runtime.home), selected)
                remedy = text.split(" — ", 1)[1].split("; then restart the gateway:", 1)[0]
                if kind == "shape":
                    self.assertEqual(remedy, "claude-multi-proxy rotate-management-key")
                self._execute_remedy(remedy)
                self.enable()
                current = management.read_active(self.runtime.home)
                self.assertIsNotNone(current)
                self.assertNotEqual(current, selected)
                self.assertFalse(os.path.lexists(staged))
                self.assertEqual(target.read_text(), "DO-NOT-FOLLOW-SENTINEL")

    def test_active_rotation_keeps_management_failure_and_both_key_radars(self):
        self.enable()
        management.stage_rotation(self.runtime.home)
        previous = proxy.config_dir(self.runtime.home) / "previous-key"
        state.atomic_write(previous, b"b" * 64 + b"\n")
        self.get.return_value = 401, b""
        _problems, _info, attention = self.report()
        self.assertIn(quota.state_text(quota.PoolStatus("mismatch"), restart_hint=self.restart), attention)
        self.assertTrue(any(line.startswith("management key rotation staged since ") for line in attention))
        self.assertTrue(any(line.startswith("gateway token rotation in progress ") for line in attention))
        self.get.assert_called_once()

    def test_staged_and_pending_start_key_texts_outside_live_branch(self):
        self.runtime.doctor_callback = lambda _runtime: []
        self.enable()
        management.stage_rotation(self.runtime.home)
        staged = management.key_dir(self.runtime.home) / management.STAGED_FILE
        os.utime(staged, (self.now.timestamp(), self.now.timestamp()))
        expected = f"management key rotation staged since 2026-10-01 12:00 UTC: restart the gateway to apply it: {self.restart}"
        self.assertIn(expected, self.report()[2])
        # Active bytes without start selection are not safe for client reads.
        staged.unlink()
        (management.key_dir(self.runtime.home) / management.PREPARED_FILE).unlink()
        self.assertEqual(cli._management_key_attention(self.runtime),
                         (quota.state_text(quota.PoolStatus("pending"), restart_hint=self.restart), None))
        self.get.assert_not_called()

    def test_doctor_override_and_readiness_failure_never_read_management(self):
        self.enable()
        self.runtime.doctor_callback = lambda _runtime: []
        with mock.patch.object(self.runtime, "pool_status", wraps=self.runtime.pool_status) as pool:
            self.report()
            pool.assert_not_called()
            self.runtime.doctor_callback = None
            self.runtime.health_get = mock.Mock(side_effect=launch.LaunchError("fixture down"))
            self.assertTrue(self.report()[0])
            pool.assert_not_called()
        self.get.assert_not_called()
        # Readiness failure must not hide a persistence-failure hold.
        # The explicit doctor override still skips the journal above.
        self.journal.assert_called_once_with(self.runtime)

    def test_missing_key_keeps_journal_fallback_and_seam_reads_nothing(self):
        self.journal.return_value = self.grant()
        problems, info, attention = self.report()
        self.assertEqual(problems, [])
        self.assertIn(quota.state_text(quota.PoolStatus("no-key"), restart_hint=self.restart,
                                       backend=endpoint.ON_DEMAND), info)
        self.assertTrue(any("invalid_grant line(s)" in line for line in attention))
        self.get.assert_not_called()
        self.enable()
        self.runtime.management_callback = None
        with mock.patch.object(management, "pool_status", side_effect=AssertionError("live read")):
            self.report()

    def test_off_the_management_channel_quota_is_unavailable_info_without_remedy(self):
        # A bundle (or source) install: key files a Nix install left behind
        # are neither read nor sent, and doctor offers no command for quota.
        self.enable()
        management.stage_rotation(self.runtime.home)
        self.journal.return_value = self.grant()
        for channel in ("bundle", None):
            with self.subTest(channel=channel):
                if channel is None:
                    self.runtime.environ.pop(management.CHANNEL_ENV, None)
                else:
                    self.runtime.environ[management.CHANNEL_ENV] = channel
                self.runtime._pool_cache = None
                with mock.patch.object(management, "key_state", side_effect=AssertionError("key read")):
                    problems, info, attention = self.report()
                self.assertEqual(problems, [])
                unavailable = quota.state_text(quota.PoolStatus("unavailable"), restart_hint=self.restart,
                                               backend=endpoint.ON_DEMAND)
                self.assertEqual(info.count(unavailable), 1)
                self.assertFalse(any("management" in line.lower() or "quota" in line.lower() for line in attention))
                self.assertTrue(any("invalid_grant line(s)" in line for line in attention))
                self.get.assert_not_called()

    def test_foreign_and_unknown_owner_keep_existing_served_safety(self):
        self.enable()
        for kind in ("foreign", "unknown"):
            with self.subTest(kind=kind):
                self.get.reset_mock()
                self.runtime.doctor_served_callback.reset_mock()
                verdict = service.OwnerVerdict(kind, "fixture owner")
                self.runtime.listener_owner = lambda _base, verdict=verdict: verdict
                problems, info, attention = self.report()
                self.assertEqual(problems, [])
                if kind == "foreign":
                    self.get.assert_not_called()
                    self.runtime.doctor_served_callback.assert_not_called()
                    self.assertIn(quota.state_text(quota.PoolStatus("not-ours"), restart_hint=self.restart), attention)
                    self.assertIn("Gateway listener ownership: the served-selector check was skipped; token not sent.", info)
                else:
                    self.get.assert_called_once()
                    self.runtime.doctor_served_callback.assert_called_once()
                    text = service.owner_attention(self.runtime.catalog.docs["gateway"]["gateway"]["base_url"], verdict)
                    self.assertEqual(attention.count(text), 1)

    def test_restart_substitutions_and_429_402_403_survive_quota_dedupe(self):
        self.enable()
        self.get.return_value = 200, b'{"files":[{"provider":"claude","status_message":"invalid_grant"}]}'
        stamp = (self.now - timedelta(minutes=10)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        lines = [self.grant()]
        for n, code in enumerate(["429"] * cli._JOURNAL_429_PER_HOUR + ["402", "403"]):
            prefix = f"[{stamp}] [r{n:07d}] [warn ] [fixture.go:1] "
            lines += [prefix + "selector model=fixture-quota-model", prefix + f"{code} | fixture"]
        lines.append(f'[{stamp}] [--------] [warn ] [fixture.go:1] codex executor: '
                     'upstream served model "replacement" for requested model "fixture-wire" (auth_index=discard)')
        self.journal.return_value = "\n".join(lines)
        self.runtime.doctor_served_callback.return_value = (["fixture served damage"], ["fixture served info"])
        with mock.patch.object(service, "pending_restart_lines", return_value=["fixture restart radar"]) as radar, \
             mock.patch('claude_multi.cli.doctor._doctor_managed_report', return_value=(["fixture managed damage"], ["fixture managed attention"])), \
             mock.patch.object(claude_multi.retention, "retention_report", return_value=(["fixture retention"], ["fixture retention info"])):
            problems, info, attention = self.report()
        radar.assert_called_once()
        # The radar asks the manager only about the recorded unit (none on demand).
        self.assertEqual(radar.call_args.kwargs["backend"], service.Backend())
        self.journal.assert_called_once()
        self.assertEqual(problems, ["fixture managed damage", "fixture served damage"])
        for text in ("fixture restart radar", "fixture managed attention", "fixture retention"):
            self.assertIn(text, attention)
        for text in ("fixture served info", "fixture retention info"):
            self.assertIn(text, info)
        self.assertTrue(any("Retry-After wait of up to 6 h" in line for line in attention))
        for code in ("429", "402", "403"):
            self.assertTrue(any(f"× {code} " in line for line in attention), attention)
        self.assertTrue(any(line.startswith("gateway substitution:") for line in attention))
        self.assertFalse(any("invalid_grant line(s) since the claude pool" in line for line in attention))


class HookErrorsAndPruneTests(_DoctorCase):
    def _hook_error(self, when: str) -> None:
        hooks.record_hook_error(self.store.root, event="prompt", managed_id=self.mid, exc=RuntimeError("x"),
                                clock=lambda: when)

    def test_hook_errors_since_the_last_launch_are_counted(self) -> None:
        settings_path = self.live(self.mid) / "settings.json"
        launched = datetime.fromtimestamp(settings_path.stat().st_mtime, timezone.utc)
        self._hook_error((launched - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._hook_error((launched + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        _problems, _info, attention = _doctor(self)
        line = next(a for a in attention if "protocol-3 hook failure" in a)
        self.assertTrue(line.startswith("1 protocol-3 hook failure(s) logged since the last launch (prompt 1)"), line)
        code, output = _run_doctor(self, "--prune")
        self.assertEqual(code, 0)
        self.assertIn("rotated hook-errors.log", output)
        _problems, _info, attention = _doctor(self)
        self.assertFalse([a for a in attention if "hook failure" in a])

    def test_prune_removes_orphan_seen_superseded_prompts_and_forgotten_logs_only(self) -> None:
        self.mutate(self.mid, last_event_source="end")
        for target, name, value in ((self.runtime, "background_liveness", sessions.BackgroundLiveness(True, frozenset())),
                                    (sessions, "proc_session_scan", sessions.ProcessScan(True, frozenset()))):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        notice = self.store.root / hooks.NOTICE_DIR
        hooks.write_seen(self.store.root, self.rid, "1 000000000000")
        hooks.write_seen(self.store.root, OTHER_RID, "1 000000000000")
        prompts = sorted(self.store.root.glob(f"lead-prompt-*-{self.mid}.md"))
        self.assertEqual(len(prompts), 1)
        current = prompts[0]
        old = self.store.root / f"lead-prompt-{'0' * 16}-{self.mid}.md"
        state.atomic_write(old, b"old prompt\n")
        past = time.time() - 3600
        os.utime(old, (past, past))
        forgotten = "88888888-8888-4888-8888-888888888888"
        lineup_log.append(self.store.root, forgotten, {"event": "apply", "time": "2026-09-25T00:00:00Z"})
        lineup_log.append(self.store.root, self.mid, {"event": "apply", "time": "2026-09-25T00:00:00Z"})
        backup = self.store.backup_path(self.mid)
        state.atomic_write(backup, b"{}\n")
        lock = sessions.migration_lock(self.store.root)
        lock.acquire(blocking=False)
        lock.release()
        code, output = _run_doctor(self, "--prune")
        self.assertEqual(code, 0, output)
        self.assertIn(f"notice marker for unknown runtime session {OTHER_RID}", output)
        self.assertIn(f"superseded lead prompt {old.name}", output)
        self.assertIn(f"lineup log of forgotten session {forgotten}", output)
        self.assertTrue((notice / f"{self.rid}.seen").exists())
        self.assertFalse((notice / f"{OTHER_RID}.seen").exists())
        self.assertTrue(current.exists())
        self.assertFalse(old.exists())
        self.assertEqual(lineup_log.log_ids(self.store.root), [self.mid])
        self.assertTrue(backup.exists())
        self.assertTrue((self.store.root / "migration.lock").exists())

    def test_prune_never_acts_by_id_while_a_record_is_unreadable(self) -> None:
        hooks.write_seen(self.store.root, OTHER_RID, "1 000000000000")
        state.atomic_write(self.store.sessions_dir / f"{OTHER_RID}.json", b"{bad")
        code, output = _run_doctor(self, "--prune")
        self.assertEqual(code, 0)
        self.assertIn("nothing pruned by session id: 1 unreadable record(s)", output)
        self.assertTrue(hooks.seen_path(self.store.root, OTHER_RID).exists())


class GatewayWorkdirTests(_DoctorCase):
    """A .env in the gateway's working directory is
    Attention naming the path; the file is never opened."""

    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.store.root / "gateway"
        self.dotenv = self.workdir / ".env"
        self.expected = (
            f"gateway working directory {self.workdir} holds a .env file: claude-multi-proxy "
            "refuses to start the gateway while it exists (CLIProxyAPI would load it: "
            "HOME_JWT, DEPLOY, WRITABLE_PATH and the store variables override the "
            "gateway's config); move it aside unless you put it there"
        )

    def _plant(self, kind: str) -> None:
        proxy.ensure_gateway_workdir(self.store.root)
        if kind == "mode-000 file":
            self.dotenv.write_bytes(b"HOME_JWT=planted-value\n")
            os.chmod(self.dotenv, 0)
        else:
            os.symlink(self.root / "nowhere" / ".env", self.dotenv)

    def _doctor_never_opening(self, runtime: cli.Runtime | None = None):
        target = str(self.dotenv)
        hits: list[str] = []
        real_open, real_io_open, real_os_open = open, io.open, os.open

        def guard(real):
            def opener(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)) and os.fsdecode(file) == target:
                    hits.append(target)
                    raise AssertionError(f"{target} was opened")
                return real(file, *args, **kwargs)
            return opener

        with mock.patch("builtins.open", guard(real_open)), \
                mock.patch("io.open", guard(real_io_open)), \
                mock.patch("os.open", guard(real_os_open)):
            result = _doctor(self, runtime)
        self.assertEqual(hits, [])
        return result

    def test_a_dotenv_is_attention_naming_the_path(self) -> None:
        for kind in ("mode-000 file", "dangling symlink"):
            with self.subTest(kind):
                if os.path.lexists(self.dotenv):
                    os.unlink(self.dotenv)
                self._plant(kind)
                problems, _info, attention = self._doctor_never_opening()
                self.assertEqual(problems, [])
                self.assertEqual(attention, [self.expected])
                self.assertTrue(os.path.lexists(self.dotenv))
        code, output = _run_doctor(self)
        self.assertEqual(code, 0, output)
        self.assertIn("Attention", output)
        self.assertIn(self.expected, output)

    def test_absent_no_line(self) -> None:
        self.assertFalse(self.workdir.exists())
        _problems, _info, attention = _doctor(self)
        self.assertEqual(attention, [])
        proxy.ensure_gateway_workdir(self.store.root)
        (self.workdir / "logs" / "main.log").write_text("")
        _problems, _info, attention = _doctor(self)
        self.assertEqual(attention, [])

    def test_the_line_shows_while_the_gateway_is_down(self) -> None:
        self._plant("mode-000 file")

        def refused(_base: str, _path: str) -> int:
            raise ConnectionRefusedError("gateway down")

        runtime = self.make_runtime(doctor_callback=None, health_get=refused)
        problems, _info, attention = self._doctor_never_opening(runtime)
        self.assertTrue(any(p.startswith("local gateway: ") for p in problems), problems)
        self.assertIn(self.expected, attention)


class CompactBoundaryWaitTests(_DoctorCase):
    """A launcher-driven stop waits out the boundary window."""

    def test_a_just_compacted_session_waits_before_stop(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        record = {"last_event_source": "compact",
                  "last_seen_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        slept: list[float] = []
        waited = cli._compact_boundary_wait(record, clock=lambda: now.timestamp(), sleep=slept.append)
        self.assertAlmostEqual(waited, cli.COMPACT_BOUNDARY_WAIT_SECONDS - 1, places=3)
        self.assertEqual(slept, [waited])
        # an old compaction, or another last event, waits for nothing
        for other in ({**record, "last_seen_at": "2026-09-26T11:00:00Z"},
                      {**record, "last_event_source": "resume"}, None):
            slept.clear()
            self.assertEqual(cli._compact_boundary_wait(other, clock=lambda: now.timestamp(),
                                                        sleep=slept.append), 0.0)
            self.assertEqual(slept, [])

    def test_stop_runs_upstream_stop_only_after_the_wait(self) -> None:
        calls: list[str] = []
        now = time.time()
        record = {"last_event_source": "compact",
                  "last_seen_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

        def runner(argv, **_kwargs):
            calls.append("stop")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def sleep(seconds: float) -> None:
            calls.append("wait")

        with mock.patch.object(claude_multi.launch, "resolve_claude") as resolve:
            resolve.return_value = mock.Mock(inspected_path=Path("/bin/true"))
            error = cli._stop_runtime(self.runtime, self.rid, runner=runner, record=record,
                                      clock=lambda: now, sleep=sleep)
        self.assertIsNone(error)
        self.assertEqual(calls, ["wait", "stop"])


class GatewayRemedyTests(_DoctorCase):
    def test_health_and_key_block_families_stay_distinct(self) -> None:
        self.runtime.doctor_callback = None
        for error, expected in (
            (launch.GatewayKeyError("missing", remedy="create key"), "local gateway key file: missing — create key"),
            (launch.LaunchError("fixture down", remedy=launch.gateway_unit_remedy()),
             f"local gateway: fixture down — start it with {service.hint_code('start')}; "
             f"why it stopped: {service.hint_code('why')}"),
            (launch.LaunchError("endpoint invalid", remedy="fix endpoint.json"),
             "local gateway: endpoint invalid — fix endpoint.json"),
        ):
            with self.subTest(error=type(error)), mock.patch.object(self.runtime, "check_readiness", side_effect=error):
                problems, _info, _attention = _doctor(self)
                self.assertIn(expected, problems)


class CustomRegistryRadarTests(_DoctorCase):
    def _write_registry(self, path, document):
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, strict_json.pretty_file_bytes(document))

    def test_header_violations_attention_and_conflicts_use_shell_path(self):
        from test_custom_registry import registry_fixture
        document = registry_fixture()
        document["models"]["sol"] = copy.deepcopy(document["models"]["clean-one"])
        path = custom.registry_path(self.runtime.environ)
        self._write_registry(path, document)
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        line = next(line for line in attention if line.startswith("custom provider acme was IGNORED:"))
        self.assertIn("auth header 'Authorization'", line)
        self.assertIn('set "header": "x-api-key"', line)
        self.assertIn(paths.display(path, self.runtime.environ), line)
        self.assertIn("claude-multi custom remove-provider acme", line)
        conflict = next(line for line in attention if "shadow catalog ids" in line)
        self.assertTrue(conflict.endswith(paths.display(path, self.runtime.environ)))
        self.assertNotIn("~/.config/claude-multi/custom.json", conflict)

    def test_parity_states_wired_into_attention(self):
        shell = custom.registry_path(self.runtime.environ)
        service = self.runtime.home / ".config/claude-multi/custom.json"
        empty = {"version": 1, "providers": {}, "models": {}}
        self.assertFalse(any("custom.json mismatch:" in line for line in _doctor(self)[2]))
        self._write_registry(shell, empty)
        self.assertIn("only " + paths.display(shell, self.runtime.environ) + " exists",
                      next(line for line in _doctor(self)[2] if line.startswith("custom.json mismatch:")))
        self._write_registry(service, empty)
        self.assertFalse(any("custom.json mismatch:" in line for line in _doctor(self)[2]))
        state.atomic_write(service, b'{"version":1,"providers":{},"models":{}}\n')
        self.assertIn("the two files differ",
                      next(line for line in _doctor(self)[2] if line.startswith("custom.json mismatch:")))
        shell.unlink()
        self.assertIn("only " + paths.display(service, self.runtime.environ) + " exists",
                      next(line for line in _doctor(self)[2] if line.startswith("custom.json mismatch:")))
        self.runtime.environ.pop("XDG_CONFIG_HOME")
        self.assertFalse(any("custom.json mismatch:" in line for line in _doctor(self)[2]))
        # An unreadable service registry is Attention, not a traceback.
        self.runtime.environ["XDG_CONFIG_HOME"] = str(shell.parents[1])
        real_read = Path.read_bytes

        def read_bytes(path):
            if path == service:
                raise PermissionError(13, "Permission denied")
            return real_read(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            self.assertTrue(any(line.startswith("custom.json unreadable: " + paths.display(service, self.runtime.environ))
                                for line in _doctor(self)[2]))
            code, out = _run_doctor(self)
        self.assertIn("custom.json unreadable:", out)

    def test_shell_secret_override_is_info_and_never_prints_its_value(self):
        _problems, info, _attention = _doctor(self)
        line = next(line for line in info if line.startswith("CLAUDE_MULTI_SECRET_ENV is set"))
        self.assertEqual(line, "CLAUDE_MULTI_SECRET_ENV is set in this shell; the gateway service ignores it "
                              "and reads ~/.config/claude-multi/secrets/provider-keys.env")
        self.assertNotIn(self.runtime.environ["CLAUDE_MULTI_SECRET_ENV"], line)
        self.runtime.environ.pop("CLAUDE_MULTI_SECRET_ENV")
        self.assertFalse(any(line.startswith("CLAUDE_MULTI_SECRET_ENV is set") for line in _doctor(self)[1]))


class SubstitutionRadarTests(_DoctorCase):
    def _line(self, requested, served="replacement", executor="codex"):
        # Go %q for fixture strings is JSON quoting; keep the production
        # format as the one fixture template (the Go literal is pinned too).
        message = cli.SUBSTITUTION_FORMAT.replace("%q", "%s") % (
            executor, json.dumps(served), json.dumps(requested), "fixture-credential-id")
        return "[2026-09-26 12:00:00] [request-never-printed] [warn ] [usage_helpers.go:297] " + message

    def test_counts_known_unknown_retired_and_shared_wire_labels(self):
        docs = self.runtime.catalog.docs
        key, entry = next(iter(docs["models"]["models"].items()))
        wire = entry["wire_model"]
        retired_key, retired = next(iter(docs["retired"]["retired"].items()))
        docs["models"]["models"]["shared-wire"] = copy.deepcopy(entry)
        text = "\n".join([self._line(wire)] * 2 + [self._line("unknown-wire"), self._line(retired["last_wire"])])
        facts = cli._journal_facts(self.runtime, text)
        by_label = {fact.label: fact for fact in facts.substitutions}
        shared_label = f"{', '.join(sorted((key, 'shared-wire')))} ({wire})"
        self.assertEqual(by_label[shared_label].count, 2)
        self.assertEqual(by_label["wire unknown-wire"].count, 1)
        self.assertIn(f"{retired_key} ({retired['last_wire']})", by_label)
        self.assertNotIn("fixture-credential-id", repr(facts))
        self.assertNotIn("request-never-printed", repr(facts))
        self.assertEqual(cli.JournalFacts({}, None, {}).substitutions, ())

    def test_custom_merged_wire_label(self):
        from test_custom_registry import registry_fixture
        path = custom.registry_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, strict_json.pretty_file_bytes(registry_fixture()))
        facts = cli._journal_facts(self.runtime, self._line("clean-wire"))
        self.assertEqual(facts.substitutions[0].label, "clean-one (clean-wire)")

    def test_hyphenated_executor_quotes_go_escape_and_malformed_line(self):
        facts = cli._journal_facts(self.runtime, self._line('wire"quoted', 'served"quoted', 'gemini-interactions'))
        self.assertEqual(facts.substitutions[0].label, 'wire wire"quoted')
        self.assertEqual(facts.substitutions[0].served, 'served"quoted')
        raw_go = self._line("wire").replace('"replacement"', '"served\\x7f"')
        self.assertEqual(cli._journal_facts(self.runtime, raw_go).substitutions[0].served, r"served\x7f")
        self.assertEqual(cli._journal_facts(self.runtime, "not a log line").substitutions, ())
        malformed = self._line("wire").replace('"replacement"', '"unterminated')
        self.assertEqual(cli._journal_facts(self.runtime, malformed).substitutions, ())

    def test_doctor_one_read_one_attention_and_lower_bound_count(self):
        wire = next(iter(self.runtime.catalog.lines.values()))["wire_model"]
        text = "\n".join([self._line(wire)] * 3)
        self.runtime.doctor_callback = None
        with mock.patch('claude_multi.cli.gateway_facts._read_gateway_journal', return_value=text) as read:
            _problems, _info, attention = _doctor(self)
        read.assert_called_once_with(self.runtime)
        mine = [line for line in attention if line.startswith("gateway substitution:")]
        self.assertEqual(len(mine), 1)
        self.assertIn("3× in 24 h, a lower bound", mine[0])
        self.assertIn("logged at most once per credential and model pair per 10 min", mine[0])
        self.assertNotIn("auth_index", mine[0])
        self.assertNotIn("request-never-printed", mine[0])
        with mock.patch('claude_multi.cli.gateway_facts._read_gateway_journal', return_value=""):
            self.assertFalse(any(line.startswith("gateway substitution:") for line in _doctor(self)[2]))


    def test_substitution_lines_label_every_production_shape(self):
        # A wire shared by two lines, a wire of one line and an unknown wire
        # each get the label doctor really prints.
        docs = self.runtime.catalog.docs
        key, entry = next(iter(docs["models"]["models"].items()))
        docs["models"]["models"]["shared-wire"] = copy.deepcopy(entry)
        known_key, known = next((k, e) for k, e in docs["models"]["models"].items()
                                if e["wire_model"] != entry["wire_model"])
        text = "\n".join([self._line(entry["wire_model"]), self._line(known["wire_model"]),
                          self._line("unknown-wire")])
        lines = cli._doctor_journal_report(self.runtime, text)
        labels = [f"{', '.join(sorted((key, 'shared-wire')))} ({entry['wire_model']})",
                  f"{known_key} ({known['wire_model']})", "wire unknown-wire"]
        self.assertEqual(sorted(line.split(" was answered by ")[0] for line in lines),
                         sorted("gateway substitution: " + label for label in labels))


class PermissionDefaultModeDriftTests(V4Case):
    """The permission default mode is decided once per
    launch or resume from the settings layers; doctor and repair of a live
    scope keep its on-disk presence or absence and never BLOCK on it."""

    def _settings(self, mid: str) -> dict:
        return strict_json.loads((self.live(mid) / "settings.json").read_bytes())

    def _mode(self, mid: str):
        return self._settings(mid)["permissions"].get("defaultMode")

    def _layer(self, path: Path, mode: str | None) -> None:
        if mode is None:
            path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes({"permissions": {"defaultMode": mode}}))

    def _doctor_lines(self, mid: str) -> tuple[list[str], list[str]]:
        problems, attention, _info = cli_doctor._check_scope_integrity(self.runtime, self.store.load(mid))
        return problems, attention

    def _repair_all_include_live(self) -> str:
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset()):
            code, output = _run_doctor(self, "--repair-all", "--include-live")
        self.assertEqual(code, 0, output)
        return output

    def test_launch_and_resume_decide_from_layers(self):
        home = Path(self.env["HOME"])
        layers = (home / ".claude/settings.json", self.project / ".claude/settings.json",
                  self.project / ".claude/settings.local.json",
                  self.runtime.managed_root / "managed-settings.json",
                  self.runtime.managed_root / "managed-settings.d/10.json")
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.assertEqual(self._mode(mid), scope.PERMISSION_DEFAULT_MODE)
        for path in layers:
            with self.subTest(layer=str(path)):
                self._layer(path, "bypassPermissions")
                self.resume(mid)
                self.assertNotIn("defaultMode", self._settings(mid)["permissions"])
                self._layer(path, None)
                self.resume(mid)
                self.assertEqual(self._mode(mid), scope.PERMISSION_DEFAULT_MODE)
        # Presence decides, never the value (which is never reported).
        self._layer(layers[0], "default")
        self.resume(mid)
        self.assertNotIn("defaultMode", self._settings(mid)["permissions"])
        self._layer(layers[0], None)

    def test_user_layer_is_home_never_claude_config_dir(self):
        """At the decision seam: the launcher unsets
        CLAUDE_CONFIG_DIR for the client, so only $HOME/.claude decides."""

        other = self.root / "other-config"
        self._layer(other / "settings.json", "acceptEdits")
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(other)
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.assertEqual(self._mode(mid), scope.PERMISSION_DEFAULT_MODE)
        self._layer(Path(self.env["HOME"]) / ".claude/settings.json", "plan")
        self._layer(other / "settings.json", None)
        # The resume gate looks for native metadata under the explicit root.
        transcript = (other / "projects" / cli._native_project_slug(self.runtime.cwd)
                      / f"{record['runtime_session_id']}.jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        self.resume(mid)
        self.assertNotIn("defaultMode", self._settings(mid)["permissions"])
        self.assertIn("CLAUDE_CONFIG_DIR", claude_multi.compiler.V2_ENV_UNSET)

    def test_layer_edit_after_launch_never_blocks_and_repair_keeps_presence(self):
        user = Path(self.env["HOME"]) / ".claude/settings.json"
        for configured in (False, True):
            with self.subTest(configured_at_launch=configured):
                if configured:
                    self._layer(user, "bypassPermissions")
                record = self.launch_fresh()
                mid = record["managed_id"]
                before = self.tree(self.live(mid))
                self.assertEqual("defaultMode" in self._settings(mid)["permissions"], not configured)
                # The operator edits the layer after launch (either direction).
                self._layer(user, None if configured else "bypassPermissions")
                problems, attention = self._doctor_lines(mid)
                self.assertEqual(problems, [])
                self.assertFalse([line for line in attention if "permission default mode" in line])
                self._repair_all_include_live()
                self.assertEqual(self.tree(self.live(mid)), before)
                if not configured:
                    self._layer(user, None)

    def test_default_mode_only_delta_is_attention_and_repair_never_adds_or_removes(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        path = self.live(mid) / "settings.json"
        before = path.read_bytes()
        document = strict_json.loads(before)
        document["permissions"]["defaultMode"] = "acceptEdits"
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        problems, attention = self._doctor_lines(mid)
        self.assertEqual(problems, [])
        self.assertIn(cli_text.DEFAULT_MODE_DRIFT.format(m8=mid[:8], mid=mid), attention)
        self._repair_all_include_live()
        self.assertEqual(path.read_bytes(), before)
        # A removed key stays removed: the live scope's absence is the input.
        document["permissions"].pop("defaultMode")
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        problems, attention = self._doctor_lines(mid)
        self.assertEqual(problems, [])
        self._repair_all_include_live()
        self.assertNotIn("defaultMode", self._settings(mid)["permissions"])
        # Any other permissions change stays BLOCK.
        document["permissions"]["deny"].append("Bash(rm:*)")
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        problems, _attention = self._doctor_lines(mid)
        self.assertTrue(problems)


class CarryInPolicyTests(V4Case):
    def write_settings(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes(doc))

    def test_settings_only_proxies_in_every_readable_layer(self):
        home = Path(self.env["HOME"])
        paths_to_check = [home / ".claude/settings.json", self.project / ".claude/settings.json",
                          self.project / ".claude/settings.local.json",
                          self.runtime.managed_root / "managed-settings.json",
                          self.runtime.managed_root / "managed-settings.d/01.json"]
        for path in paths_to_check:
            with self.subTest(path=path):
                self.write_settings(path, {"env": {"HTTPS_PROXY": "credential-not-for-scope", "NO_PROXY": "internal"}})
                prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
                env = prepared.result.scope_plan.settings["env"]
                self.assertEqual(env["NO_PROXY"], "127.0.0.1,localhost,::1,internal")
                self.assertEqual(env["NO_PROXY"], prepared.result.env_set["no_proxy"])
                self.assertNotIn("credential-not-for-scope", repr(prepared.result))
                path.unlink()

    def test_shell_only_bypass_survives_proxy_free_repair_resume_and_daemon_read(self):
        self.runtime.environ.update(HTTPS_PROXY="fixture", NO_PROXY="internal")
        record = self.launch_fresh()
        mid = record["managed_id"]
        before = self.tree(self.live(mid))
        self.runtime.environ.pop("HTTPS_PROXY")
        self.runtime.environ.pop("NO_PROXY")
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset()):
            code, output = _run_doctor(self, "--repair-all")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.tree(self.live(mid)), before)
        # Repair of unsafe settings permissions also keeps the only on-disk
        # copy of a shell-origin bypass (the private launch-file read fails).
        (self.live(mid) / "settings.json").chmod(0o644)
        transition.converge(self.store.root, self.store, mid, runtime_parts=self.runtime.converge_parts())
        self.assertEqual(self.tree(self.live(mid)), before)
        # A daemon-style relaunch reads only durable settings: no parent env.
        daemon_env = strict_json.loads((self.live(mid) / "settings.json").read_bytes())["env"]
        self.assertEqual(daemon_env["no_proxy"], "127.0.0.1,localhost,::1,internal")
        resumed = self.prepare_resume(mid)
        self.assertEqual(resumed.result.scope_plan.settings["env"]["NO_PROXY"], daemon_env["NO_PROXY"])
        parts = self.runtime.converge_parts()
        ep = transition.expected_plan(record, docs=parts.docs, prompt_bodies=parts.prompt_bodies,
                                      state_root=self.store.root, hook_command=parts.hook_command,
                                      token_helper_command=parts.token_helper_command, live=self.live(mid),
                                      environ=parts.environ, managed_root=parts.managed_root)
        self.assertFalse(ep.catalog_launch_differs)
        self.assertEqual(scope.live_drift(self.live(mid), ep.plan), [])

    def test_proxy_added_after_launch_repair_preserves_the_launch_fence(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        before = strict_json.loads((self.live(mid) / "settings.json").read_bytes())
        self.write_settings(self.project / ".claude/settings.json", {"env": {"ALL_PROXY": "fixture"}})
        transition.converge(self.store.root, self.store, mid, runtime_parts=self.runtime.converge_parts())
        after = strict_json.loads((self.live(mid) / "settings.json").read_bytes())
        self.assertEqual(after["availableModels"], before["availableModels"])
        self.assertEqual(after["env"]["NO_PROXY"],
                         "127.0.0.1,localhost,::1")
        self.assertEqual(sessions.launch_digest(after, (self.live(mid) / scope.LEAD_SET_JSON).read_bytes()),
                         record["launch_fence"])
        # No second-repair widening/rebuild from a now-unproven model fence.
        self.assertIn("live scope matches the record-authoritative compile",
                      transition.converge(self.store.root, self.store, mid,
                                          runtime_parts=self.runtime.converge_parts()))

    def test_doctor_proxy_effective_values_and_settings_override(self):
        path = self.project / ".claude/settings.json"
        self.write_settings(path, {"env": {"HTTPS_PROXY": "secret-value", "no_proxy": "internal"}})
        problems, _ = cli._doctor_proxy_report(self.runtime, [])
        self.assertEqual(problems, [])  # flag settings protect a settings-only proxy
        self.write_settings(path, {"env": {"no_proxy": "internal"}})
        self.runtime.environ["HTTP_PROXY"] = "fixture"
        self.assertEqual(cli._doctor_proxy_report(self.runtime, [])[0], [])
        path.unlink()
        problems, info = cli._doctor_proxy_report(self.runtime, [])
        self.assertEqual(problems, [])
        self.assertIn("compile a sticky", str(info))
        code, output = _run_doctor(self)
        self.assertEqual(code, 0, output)
        record = self.launch_fresh()
        self.assertEqual(cli._doctor_proxy_report(self.runtime, [record])[0], [])
        code, output = _run_doctor(self)
        self.assertEqual(code, 0, output)

    def test_claude_config_dir_never_decides_the_bypass(self):
        """Managed launches unset CLAUDE_CONFIG_DIR, so only
        $HOME/.claude/settings.json is the user layer of the NO_PROXY read,
        whatever the launcher's environment names (fake-proxy controls)."""

        other = self.root / "other-config"
        self.runtime.environ["CLAUDE_CONFIG_DIR"] = str(other)
        home_settings = Path(self.env["HOME"]) / ".claude/settings.json"
        # Negative control: a proxy only in $CLAUDE_CONFIG_DIR never reaches a
        # managed session, so nothing is compiled and doctor stays quiet.
        self.write_settings(other / "settings.json", {"env": {"HTTPS_PROXY": "fake-proxy-value"}})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertNotIn("NO_PROXY", prepared.result.scope_plan.settings["env"])
        self.assertEqual(cli._doctor_proxy_report(self.runtime, []), ([], []))
        # Positive control: the same proxy in HOME's file is what the managed
        # client applies; the bypass is compiled though CLAUDE_CONFIG_DIR is set.
        (other / "settings.json").unlink()
        self.write_settings(home_settings, {"env": {"HTTPS_PROXY": "fake-proxy-value", "NO_PROXY": "internal"}})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertEqual(prepared.result.scope_plan.settings["env"]["NO_PROXY"], "127.0.0.1,localhost,::1,internal")
        self.assertNotIn("fake-proxy-value", repr(prepared.result))
        # An unsafe bypass in $CLAUDE_CONFIG_DIR is never attributed; HOME's is.
        self.write_settings(other / "settings.json", {"env": {"NO_PROXY": "internal-only"}})
        self.write_settings(home_settings, {"env": {"HTTPS_PROXY": "fake-proxy-value", "no_proxy": "internal"}})
        self.runtime.managed_root = self.runtime.home / "managed-policy"
        self.write_settings(self.runtime.managed_root / "managed-settings.json", {"env": {"NO_PROXY": "internal"}})
        problems, _info = cli._doctor_proxy_report(self.runtime, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("~/managed-policy/managed-settings.json: env.NO_PROXY"))
        self.assertNotIn("other-config", "\n".join(problems))
        self.assertNotIn("fake-proxy-value", "\n".join(problems))
        layers = [path for path, _env in managed.env_layers(
            {**self.runtime.environ, "HOME": str(self.runtime.home)}, self.runtime.cwd, self.runtime.managed_root)]
        self.assertEqual(layers[0], self.runtime.home / ".claude/settings.json")
        self.assertNotIn(other / "settings.json", layers)

    def test_managed_proxy_override_blocks_and_names_only_its_actual_source(self):
        policy = self.runtime.home / "managed-policy"
        self.runtime.managed_root = policy
        path = policy / "managed-settings.json"
        for name in ("NO_PROXY", "no_proxy"):
            with self.subTest(name=name):
                self.write_settings(path, {"env": {"HTTPS_PROXY": "secret-value", name: "internal"}})
                problems, _ = cli._doctor_proxy_report(self.runtime, [])
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith("~/managed-policy/managed-settings.json: env." + name))
                self.assertNotIn("secret-value", str(problems))
                self.assertNotIn(str(self.runtime.home), str(problems))
        self.write_settings(path, {"env": {"HTTPS_PROXY": "secret-value"}})
        self.assertEqual(cli._doctor_proxy_report(self.runtime, [])[0], [])

    def test_managed_policy_still_outranks_flags_when_home_is_the_project(self):
        self.runtime.cwd = str(self.runtime.home)
        self.runtime.environ["HTTPS_PROXY"] = "fixture"
        self.runtime.managed_root = self.runtime.home / "managed-policy"
        self.write_settings(self.runtime.managed_root / "managed-settings.json",
                            {"env": {"no_proxy": "internal"}})
        self.assertEqual(len(cli._doctor_proxy_report(self.runtime, [])[0]), 1)
        record = self.launch_fresh()
        problems, _ = cli._doctor_proxy_report(self.runtime, [record])
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("~/managed-policy/managed-settings.json:"))
        self.assertNotIn("--repair", problems[0])  # compiled scope is already safe

    def test_existing_scope_without_bypass_names_source_scope_and_repair(self):
        record = self.launch_fresh()  # a pre-proxy scope
        path = self.runtime.home / ".claude/settings.json"
        self.write_settings(path, {"env": {"HTTPS_PROXY": "secret-value"}})
        problems, _ = cli._doctor_proxy_report(self.runtime, [record])
        self.assertEqual(len(problems), 2)
        for problem in problems:
            self.assertTrue(problem.startswith("~/.claude/settings.json:"))
            self.assertIn(str(self.live(record["managed_id"]) / "settings.json"), problem)
            self.assertIn(f"claude-multi doctor --repair {record['managed_id']}", problem)
            self.assertNotIn("secret-value", problem)
        path.unlink()
        self.runtime.environ["HTTPS_PROXY"] = "fixture"
        problems, _ = cli._doctor_proxy_report(self.runtime, [record])
        self.assertTrue(all(problem.startswith("launch environment:") for problem in problems))
        self.assertNotIn("~/.claude/settings.json", str(problems))

    def test_hook_target_missing_blocks_launch_without_committing(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        prepared = self.prepare_resume(mid)
        target = scope.hook_targets(prepared.result.scope_plan.settings)[0]
        before = self.tree(self.live(mid)), self.record_bytes(mid), self.store.last(self.runtime.cwd)
        target.unlink()
        with self.assertRaisesRegex(launch.LaunchError, "compiled hook target .* missing or not executable"):
            self.runtime.perform(prepared)
        self.assertEqual(before, (self.tree(self.live(mid)), self.record_bytes(mid), self.store.last(self.runtime.cwd)))
        problems, info = cli._doctor_hook_targets(self.runtime, [record])
        self.assertIn("compiled hook target", str(problems))
        self.assertEqual(info, [])
        self.assertIn("compiled hook target", str(_doctor(self)[0]))
        self.assertIn(claude_multi.paths.display(target, self.runtime.environ), str(problems))

    def test_a_non_executable_helper_target_blocks_every_launch_kind(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        prepared = self.prepare_resume(mid)
        targets = scope.hook_targets(prepared.result.scope_plan.settings)
        helper = Path(shlex.split(prepared.result.scope_plan.settings["apiKeyHelper"])[0])
        self.assertIn(helper, targets)
        helper.chmod(0o600)
        self.addCleanup(helper.chmod, 0o700)
        with self.assertRaisesRegex(launch.LaunchError, f"compiled hook target {helper} is missing or not executable"):
            self.runtime.perform(prepared)
        fresh = self.runtime.prepare(self.direct_target(), action="fresh", passthrough=[])
        with self.assertRaisesRegex(launch.LaunchError, "missing or not executable"):
            self.runtime.perform(fresh)
        self.assertIsNone(self.record_bytes(fresh.record["managed_id"]))

    def test_hook_target_report_uses_home_relative_paths(self):
        record = self.launch_fresh()
        target = self.runtime.home / "missing-hook"
        with mock.patch.object(scope, "missing_hook_targets", return_value=[target]):
            problems, _ = cli._doctor_hook_targets(self.runtime, [record])
        self.assertIn("compiled hook target ~/missing-hook", problems[0])
        self.assertNotIn(str(self.runtime.home), problems[0])

    def test_hook_targets_are_executable_files_and_doctor_reports_success(self):
        record = self.launch_fresh()
        settings = strict_json.loads((self.live(record["managed_id"]) / "settings.json").read_bytes())
        targets = scope.hook_targets(settings)
        self.assertEqual(len(targets), 2)  # six hooks use one shim, plus apiKeyHelper
        self.assertEqual(scope.missing_hook_targets(settings), [])
        self.assertIn("every compiled hook command exists (1 session(s))", str(cli._doctor_hook_targets(self.runtime, [record])[1]))
        targets[0].chmod(0o600)
        self.assertEqual(scope.missing_hook_targets(settings), [targets[0]])
        targets[0].unlink()
        targets[0].mkdir()
        self.assertEqual(scope.missing_hook_targets(settings), [targets[0]])

    def test_managed_doctor_block_notes_and_launch_preflight_without_values(self):
        policy = self.runtime.managed_root / "managed-settings.json"
        self.write_settings(policy, {"disableAllHooks": True, "model": "secret-value",
                                     "apiKeyHelper": "secret-value", "forceLoginMethod": "secret-value"})
        problems, attention = cli._doctor_managed_report(self.runtime)
        self.assertEqual(len(problems), 3)  # hooks off, model lock, credential helper
        self.assertEqual(len(attention), 1)  # the login method
        self.assertNotIn("secret-value", repr((problems, attention)))
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        with self.assertRaisesRegex(launch.LaunchError, "hooks do not run.*apiKeyHelper") as caught:
            self.runtime.perform(prepared)
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertEqual(self.execs, [])
        self.assertIsNone(self.record_bytes(prepared.record["managed_id"]))
        # A model lock alone does not refuse; it is named on stderr.
        self.write_settings(policy, {"model": "secret-value"})
        output = io.StringIO()
        with mock.patch("sys.stderr", output):
            self.launch_fresh()
        self.assertIn("managed policy", output.getvalue())
        self.assertIn("model choice is locked", output.getvalue())
        self.assertNotIn("secret-value", output.getvalue())

    def test_foreign_listener_blocks_launch_and_skips_doctor_served_check(self):
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.runtime.listener_owner = lambda _base: service.OwnerVerdict("foreign", "another user (uid 9000)", 9000)
        with self.assertRaisesRegex(launch.GatewayOwnerError, "BLOCK:.*token was not sent"):
            self.runtime.perform(prepared)
        self.assertEqual(self.execs, [])
        self.assertIsNone(self.record_bytes(prepared.record["managed_id"]))
        self.runtime.doctor_callback = None
        self.runtime.doctor_served_callback = mock.Mock(side_effect=AssertionError("token leaked"))
        _, info, attention = _doctor(self)
        self.assertIn("served-selector check was skipped", str(info))
        self.assertIn("another user", str(attention))
        self.runtime.doctor_served_callback.assert_not_called()

    def test_unknown_listener_warns_then_launches(self):
        self.runtime.listener_owner = lambda _base: service.OwnerVerdict("unknown", "another process of yours")
        output = io.StringIO()
        with mock.patch("sys.stderr", output):
            self.launch_fresh()
        self.assertTrue(self.execs)
        self.assertIn("Attention", output.getvalue())


class FeedbackPreferenceDoctorTests(_DoctorCase):
    """Activation over existing scopes, a toggle and a
    rollback each give a preference-only Attention, never BLOCK; proven launch
    files are kept; real damage still BLOCKs; the next resume applies it."""

    LINE = "managed-session preference changed; applies at the next resume"

    def _set(self, value: str | None) -> None:
        store = self.runtime.preferences_store
        if value is None:
            store.path.unlink(missing_ok=True)
        else:
            store.update(lambda document: document.__setitem__("claude_feedback_drafts", value))

    def _live_env(self) -> dict:
        return json.loads((self.live(self.mid) / "settings.json").read_text())["env"]

    def _only_preference_attention(self) -> None:
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertIn(f"session {self.m8}: {self.LINE}", attention)
        self.assertFalse(any("fence/picker differ" in line for line in attention), attention)

    def test_toggle_and_rollback_are_attention_never_block(self) -> None:
        self.assertEqual(self._live_env()["CLAUDE_CODE_SEND_FEEDBACK"], "false")
        self._set("notify")  # toggle (also what a pre-3.1 compile, which omits the key, compares like)
        self._only_preference_attention()
        code, _output = _run_doctor(self, "--repair", self.mid)
        self.assertEqual(self._live_env()["CLAUDE_CODE_SEND_FEEDBACK"], "false")  # proven files kept
        self._set(None)  # back to the default: nothing to report
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertFalse(any(self.LINE in line for line in attention), attention)

    def test_activation_over_a_scope_without_the_key(self) -> None:
        self._set("notify")
        record = self.launch_fresh()  # a scope compiled without the key (pre-3.1 shape)
        self.mid, self.m8 = record["managed_id"], record["managed_id"][:8]
        self.assertNotIn("CLAUDE_CODE_SEND_FEEDBACK", self._live_env())
        self._set(None)  # 3.1 activation: absent preference = off
        self._only_preference_attention()
        self.resume(self.mid)  # the next resume applies it
        self.assertEqual(self._live_env()["CLAUDE_CODE_SEND_FEEDBACK"], "false")
        problems, _info, attention = _doctor(self)
        self.assertFalse(any(self.LINE in line for line in attention), attention)

    def test_edited_launch_files_still_block(self) -> None:
        path = self.live(self.mid) / "settings.json"
        document = json.loads(path.read_text())
        document["env"].pop("CLAUDE_CODE_SEND_FEEDBACK")
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        problems, _info, _attention = _doctor(self)
        self.assertTrue(any("scope differs from the record-authoritative compile" in line
                            for line in problems), problems)

    def test_unreadable_preferences_are_attention(self) -> None:
        path = self.runtime.preferences_store.path
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"[]")
        problems, _info, attention = _doctor(self)
        self.assertEqual(problems, [])
        self.assertTrue(any("managed sessions use the default claude_feedback_drafts off" in line
                            for line in attention), attention)


class CatalogAgentDriftTests(_DoctorCase):
    """agent-file drift that comes
    only from a changed catalog (the record's catalog hash differs), where
    every drifted file parses as a cm-* agent file whose name, model and
    effort match the record's binding, is Attention; every other drift stays
    BLOCK."""

    def _changed_role_text(self) -> str:
        """A test-local catalog whose analyst role text changed (a catalog
        content change that moves agent-file bytes, never a binding)."""

        root = self.root / "assets-roles"
        shutil.copytree(FIXTURE_ROOT, root)
        for directory, _dirs, files in os.walk(root):
            os.chmod(directory, 0o755)
            for name in files:
                os.chmod(Path(directory) / name, 0o644)
        slot = next(rid for rid in sorted(self.record["applied"]["agents"]))
        path = root / "catalog" / "roles.json"
        document = json.loads(path.read_text())
        document["roles"][slot]["description"] += " Revised."
        path.write_bytes(strict_json.pretty_file_bytes(document))
        self.runtime = self.make_runtime(asset_root=root)
        self.assertNotEqual(self.runtime.catalog.bundle_sha256, self.record["catalog_hash"])
        return slot

    def _agent(self, slot: str) -> Path:
        return self.live(self.mid) / ".claude" / "agents" / f"{slot}.md"

    def test_catalog_only_agent_content_drift_is_attention_and_repairs(self) -> None:
        # The catalog-change regression: this once BLOCKed every session
        # that existed at a catalog release.
        slot = self._changed_role_text()
        # Every generated agent file (isolation and disallowedTools roles
        # included) matches the frontmatter grammar the downgrade requires.
        from claude_multi.cli import doctor as doctor_mod

        for path in sorted(self._agent(slot).parent.glob("cm-*.md")):
            self.assertIsNotNone(doctor_mod._agent_frontmatter(path.read_bytes()), path.name)
        problems, _info, attention = _doctor(self)
        self.assertEqual([p for p in problems if self.m8 in p], [])
        self.assertIn(
            f"session {self.m8}: agent definitions differ from the installed catalog ({slot}; catalog "
            "content changed since launch, bindings unchanged); applies at the next resume "
            f"(claude-multi -r {self.mid}) — or claude-multi doctor --repair {self.mid} now",
            attention,
        )
        code, output = _run_doctor(self, "--repair", self.mid)
        self.assertEqual(code, 0, output)
        self.assertIn(b"Revised.", self._agent(slot).read_bytes())
        problems, _info, attention = _doctor(self)
        self.assertFalse([line for line in problems + attention if "agent definitions differ" in line])

    def test_the_same_catalog_with_edited_agent_content_blocks(self) -> None:
        slot = next(iter(sorted(self.record["applied"]["agents"])))
        path = self._agent(slot)
        state.atomic_write(path, path.read_bytes() + b"tampered\n")
        problems, _info, attention = _doctor(self)
        self.assertTrue(any(p.startswith(f"session {self.m8}: scope differs") for p in problems), problems)
        self.assertFalse([line for line in attention if "agent definitions differ" in line])

    def test_everything_but_validated_agent_content_still_blocks(self) -> None:
        slot = self._changed_role_text()
        binding = self.record["applied"]["agents"][slot]
        original = self._agent(slot).read_bytes()
        cases = {
            "model rebound": original.replace(f"model: {binding['selector']}\n".encode(), b"model: other-model\n"),
            "effort changed": original.replace(f"effort: {binding['effort']}\n".encode(), b"effort: low\n"),
            "name changed": original.replace(f"name: {slot}\n".encode(), b"name: cm-other\n"),
            "not an agent file": b"no frontmatter here\n",
            "not utf-8": b"---\xff\n",
        }
        # Invalid YAML (or YAML the writer never emits) with the
        # binding fields intact is still corruption, never catalog drift.
        head, fence, body = original.partition(b"\n---\n")
        lines = head.split(b"\n")
        description = next(line for line in lines if line.startswith(b"description: "))
        effort = f"effort: {binding['effort']}".encode()
        model = f"model: {binding['selector']}".encode()
        def edited(new_lines: list[bytes]) -> bytes:
            return b"\n".join(new_lines) + fence + body
        cases.update({
            "unterminated description": original.replace(description, description[:-1]),
            "stray quote in description": original.replace(description, description[:-1] + b'"x"'),
            "flow collection": edited([*lines, b"tools: ["]),
            "unknown key": edited([*lines, b"tools: Read"]),
            "duplicate key": edited([*lines, model]),
            "keys out of order": edited([line for line in lines if line != effort][:2] + [effort] +
                                        [line for line in lines if line != effort][2:]),
            "description without the sentinel": original.replace(description, b'description: "Revised."'),
            "no blank line after the fence": edited(lines)[: len(head) + len(fence)] + body.lstrip(b"\n"),
        })
        for codepoint in (0x80, 0x9F, 0xFFFE, 0xFFFF):
            bad_description = description.replace(
                b" Managed cm session:",
                chr(codepoint).encode("utf-8") + b" Managed cm session:",
            )
            self.assertNotEqual(bad_description, description)
            cases[f"non-printable U+{codepoint:04X}"] = original.replace(description, bad_description)
        for label, data in cases.items():
            with self.subTest(label):
                self.assertNotEqual(data, original)
                state.atomic_write(self._agent(slot), data)
                problems, _info, attention = _doctor(self)
                self.assertTrue(any(p.startswith(f"session {self.m8}: scope differs") for p in problems), problems)
                self.assertFalse([line for line in attention if "agent definitions differ" in line])
        state.atomic_write(self._agent(slot), original)
        # A drifted non-agent file next to catalog-only agent drift blocks too.
        lineup_md = self.live(self.mid) / "lineup.md"
        state.atomic_write(lineup_md, lineup_md.read_bytes() + b"edited\n")
        problems, _info, attention = _doctor(self)
        self.assertTrue(any(p.startswith(f"session {self.m8}: scope differs") for p in problems), problems)
        state.atomic_write(lineup_md, lineup_md.read_bytes()[: -len(b"edited\n")])
        # An unexpected agent file or a mode change is not validated content.
        extra = self.live(self.mid) / ".claude" / "agents" / "cm-unbound.md"
        state.atomic_write(extra, original)
        problems, _info, _attention = _doctor(self)
        self.assertTrue(any(p.startswith(f"session {self.m8}: scope differs") for p in problems), problems)
        extra.unlink()
        os.chmod(self._agent(slot), 0o644)
        problems, _info, attention = _doctor(self)
        self.assertTrue(any(p.startswith((f"session {self.m8}: scope differs", f"session {self.m8}: scope unreadable"))
                            for p in problems), problems)
        self.assertFalse([line for line in attention if "agent definitions differ" in line])


import test_operator_agents  # module import: no test classes re-exported


class OperatorAgentA06Tests(test_operator_agents.AgentCase):
    """Doctor A06 is the contract-stale line, verbatim,
    for a live session that records an operator agent whose qualification
    predates the current pins (Attention, never BLOCK)."""

    def test_a06_exact_text(self) -> None:
        from claude_multi import continuity
        from claude_multi.cli import doctor as doctor_mod

        key = test_operator_agents.AGENT_KEY
        self.install(self.runtime, self.acme_files(), keys=(key,))
        test_operator_agents.record_passes(self.env, key, self.digest(),
                                           contracts=test_operator_agents.OTHER_CONTRACTS)
        live = "m" * 8
        record = {"version": sessions.RECORD_VERSION,
                  "applied": {"agents": {"cm-reviewer": {"key": key, "effort": "high"}}}}
        with mock.patch.object(continuity, "scan_records", return_value=continuity.RecordScan(
                refs={}, live=frozenset({live}), unreadable=(), notices=())), \
                mock.patch.object(self.runtime.session_store, "scan_uuid_records",
                                  return_value=[Path(f"{live}.json")]), \
                mock.patch.object(self.runtime.session_store, "load_raw", return_value=(record, b"")):
            blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertIn(
            f"{key}: qualification predates the current pins (stale evidence)",
            attention,
        )
        self.assertFalse([line for line in blocks if key in line])


class GeneratedPreviewTests(unittest.TestCase):
    def test_prune_preview_never_touches_claude_or_backups(self):
        import io
        import os
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from claude_multi import sessions
        from claude_multi.cli import doctor_actions
        with tempfile.TemporaryDirectory(prefix='cm-preview-') as temporary:
            home = Path(temporary)
            root = home / 'state'
            root.mkdir(mode=0o700)
            for name in ('.claude/keep', 'state/auth.pre-fixture/keep', 'state/restored-2x/keep'):
                path = home / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            store = sessions.SessionStore(root, sessions.default_schema(), read_only=True)
            runtime = SimpleNamespace(session_store=store, report_value=lambda key, read: read())
            before = {str(p): (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in home.rglob('*')}
            out = io.StringIO()
            self.assertEqual(doctor_actions._doctor_prune_preview(runtime, out), 0)
            self.assertIn('no files removed', out.getvalue())
            self.assertEqual(before, {str(p): (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in home.rglob('*')})

    def test_cleanup_does_not_classify_unadmitted_declaration_as_garbage(self):
        from pathlib import Path
        import io
        import tempfile
        from types import SimpleNamespace
        from claude_multi import sessions
        from claude_multi.cli import doctor_actions
        with tempfile.TemporaryDirectory(prefix='cm-unadmitted-') as temporary:
            home = Path(temporary)
            declaration = home / '.config/claude-multi/providers.d/unadmitted.json'
            declaration.parent.mkdir(parents=True); declaration.write_bytes(b'fixture declaration')
            store = sessions.SessionStore(home / 'state', sessions.default_schema(), read_only=True)
            out = io.StringIO()
            self.assertEqual(doctor_actions._doctor_prune_preview(SimpleNamespace(session_store=store, report_value=lambda key, read: read()), out), 0)
            self.assertNotIn('Candidate:', out.getvalue())
            self.assertEqual(declaration.read_bytes(), b'fixture declaration')


class GeneratedPromptPruneRaceTests(_DoctorCase):
    def setUp(self):
        super().setUp()
        self.old = self.store.root / f"lead-prompt-{'0' * 16}-{self.mid}.md"
        state.atomic_write(self.old, b'old prompt')
        os.utime(self.old, (1, 1))
        self.mutate(self.mid, last_event_source='end')
        for target, name, value in ((self.runtime, 'background_liveness', sessions.BackgroundLiveness(True, frozenset())),
                                    (sessions, 'proc_session_scan', sessions.ProcessScan(True, frozenset()))):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_hook_restart_before_prune_commit_preserves_generated_prompt(self):
        real_index = self.store.runtime_index_lock
        def restart_then_lock():
            self.mutate(self.mid, last_event_source='startup')
            return real_index()
        with mock.patch.object(self.store, 'runtime_index_lock', side_effect=restart_then_lock):
            _run_doctor(self, '--prune')
        self.assertTrue(self.old.exists())

    def test_unreadable_fence_preserves_generated_prompt(self):
        state.atomic_write(self.live(self.mid) / 'settings.json', b'{broken')
        code, output = _run_doctor(self, '--prune')
        self.assertTrue(self.old.exists())
        self.assertIn('unreadable scope fence', output)


class ManagedPreflightLaunchTests(V4Case):
    """Managed policy that would break a managed session refuses the launch
    before anything is written; doctor names the same settings (names only)."""

    def policy(self, doc):
        path = self.runtime.managed_root / "managed-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes(doc))

    def refused(self, pattern):
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        with self.assertRaisesRegex(launch.LaunchError, pattern) as caught:
            self.runtime.perform(prepared)
        self.assertEqual(self.execs, [])
        self.assertIsNone(self.record_bytes(prepared.record["managed_id"]))
        return str(caught.exception)

    def test_version_bounds_and_providers_refuse_with_an_explanation(self):
        self.policy({"requiredMinimumVersion": "9.0.0"})
        text = self.refused("requiredMinimumVersion: Claude Code 2.1.281, the version claude-multi runs")
        self.assertIn("ask the policy owner", text)
        self.assertNotIn("9.0.0", text)
        self.policy({"allowedProviders": ["anthropic"]})
        self.refused("allowedProviders: it does not list customEndpoint")
        self.policy({"allowManagedHooksOnly": True})
        self.refused("allowManagedHooksOnly: hooks do not run")
        self.policy({"forceLoginOrgUUID": "SECRET-ORG"})
        self.assertNotIn("SECRET-ORG", self.refused("forceLoginOrgUUID"))
        problems, _attention = cli._doctor_managed_report(self.runtime)
        self.assertEqual(len(problems), 1)
        self.assertNotIn("SECRET-ORG", problems[0])

    def test_the_policy_remedy_names_no_particular_computer(self):
        # The launch refusal and doctor share one neutral remedy.
        from claude_multi import managed

        self.assertEqual(managed.POLICY_REMEDY, "ask the policy owner, or use plain claude")
        self.policy({"requiredMinimumVersion": "9.0.0"})
        refusal = self.refused("requiredMinimumVersion")
        problems, _attention = cli._doctor_managed_report(self.runtime)
        self.policy({"requiredMinimumVersion": "9.0.0", "x": 1})
        path = self.runtime.managed_root / "managed-settings.json"
        path.write_bytes(b"{" + b" " * (1 << 20) + b"}")
        unchecked, _attention = cli._doctor_managed_report(self.runtime)
        for text in (refusal, *problems, *unchecked):
            with self.subTest(text=text[:60]):
                self.assertTrue(text.endswith(" — " + managed.POLICY_REMEDY), text)
                self.assertNotIn("this machine", text)

    def test_duplicate_keys_and_split_files_are_judged_like_the_client(self):
        path = self.runtime.managed_root / "managed-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"requiredMaximumVersion": "1.0.0", "requiredMaximumVersion": "9.9.9",'
                         b' "allowManagedHooksOnly": false, "allowManagedHooksOnly": true}')
        self.refused("allowManagedHooksOnly: hooks do not run")
        path.unlink()
        base_url = endpoint.gateway_endpoint(self.runtime.catalog.docs["gateway"]).base_url
        drop_ins = self.runtime.managed_root / "managed-settings.d"
        drop_ins.mkdir()
        (drop_ins / "10-providers.json").write_text('{"allowedProviders": ["customEndpoint"]}')
        (drop_ins / "20-endpoint.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": base_url}}))
        self.launch_fresh()  # the merged policy admits the gateway
        self.assertEqual(len(self.execs), 1)

    def test_a_policy_file_that_cannot_be_used_is_named(self):
        path = self.runtime.managed_root / "managed-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"allowManagedHooksOnly": true,')
        problems, attention = cli._doctor_managed_report(self.runtime)
        self.assertEqual(problems, [])
        self.assertEqual(len(attention), 1)
        self.assertIn("managed-settings.json is not valid JSON: the managed client cannot apply it", attention[0])
        path.write_bytes(b'{"model": "' + b"x" * (1 << 20) + b'"}')
        problems, _attention = cli._doctor_managed_report(self.runtime)
        self.assertEqual(len(problems), 1)
        self.assertIn("is larger than 1 MiB", problems[0])
        self.refused("is larger than 1 MiB")

    def test_denied_models_refuse_only_a_lineup_that_binds_one(self):
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        lead = prepared.lineup.lead.binding.selector
        self.policy({"deniedModels": ["no-such-model-family-x"]})
        self.launch_fresh()  # Attention only: nothing bound is denied
        problems, attention = cli._doctor_managed_report(self.runtime)
        self.assertEqual(problems, [])
        self.assertIn("deniedModels", attention[0])
        self.policy({"deniedModels": [lead]})
        self.execs.clear()
        self.assertIn(lead, self.refused("deniedModels: it denies"))

    def test_the_card_shows_a_policy_block(self):
        self.policy({"apiKeyHelper": "SECRET"})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        blocks = self.runtime.managed_launch_blocks(prepared.lineup)
        self.assertEqual(len(blocks), 1)
        self.assertIn("apiKeyHelper", blocks[0])
        self.assertNotIn("SECRET", blocks[0])


class SettingsSkewLaunchTests(V4Case):
    """A user settings file with keys the pin does not know: the launch
    compiles the default permission mode and names the keys."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_claude["settings_keys"] = ["env", "model", "permissions"]
        self.runtime = self.make_runtime()
        self.user = Path(self.env["HOME"]) / ".claude/settings.json"
        self.user.parent.mkdir(parents=True, exist_ok=True)

    def write_user(self, doc):
        self.user.write_bytes(strict_json.canonical_file_bytes(doc))

    def compiled_mode(self, prepared):
        return prepared.result.scope_plan.settings.get("permissions", {}).get("defaultMode")

    def test_unknown_keys_compile_the_default_mode_and_an_attention_line(self):
        self.write_user({"permissions": {"defaultMode": "acceptEdits"}})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertIsNone(self.compiled_mode(prepared))
        self.assertFalse([line for line in prepared.notices if "does not know" in line])
        self.write_user({"permissions": {"defaultMode": "acceptEdits"}, "newerSetting": "SECRET", "alsoNew": 1})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertEqual(self.compiled_mode(prepared), scope.PERMISSION_DEFAULT_MODE)
        lines = [line for line in prepared.notices if "does not know" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("~/.claude/settings.json", lines[0])
        self.assertIn("(alsoNew, newerSetting)", lines[0])
        self.assertIn("Claude Code 2.1.281", lines[0])
        self.assertNotIn("SECRET", lines[0])
        self.assertTrue(lines[0].startswith("! "))
        _problems, _info, attention = _doctor(self)
        self.assertIn("(alsoNew, newerSetting)", str(attention))

    def test_another_layers_mode_never_bypasses_the_skew_default(self):
        self.write_user({"permissions": {"deny": ["Read(./secret)"]}, "newerSetting": True})
        project = Path(self.runtime.cwd) / ".claude/settings.json"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_bytes(strict_json.canonical_file_bytes({"permissions": {"defaultMode": "auto"}}))
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertEqual(self.compiled_mode(prepared), scope.PERMISSION_DEFAULT_MODE)
        lines = [line for line in prepared.notices if "does not know" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("this session starts in the default permission mode", lines[0])
        # An explicit command-line mode still wins in the client; the line says so.
        prepared = self.runtime.prepare(self.profile_target(), action="fresh",
                                        passthrough=["--permission-mode", "plan"])
        self.assertEqual(self.compiled_mode(prepared), scope.PERMISSION_DEFAULT_MODE)
        self.assertIn("the permission mode given on the command line",
                      [line for line in prepared.notices if "does not know" in line][0])
        # A managed policy mode outranks the flag settings: nothing compiled,
        # and the line names the policy's mode instead of promising default.
        policy = self.runtime.managed_root / "managed-settings.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_bytes(strict_json.canonical_file_bytes({"permissions": {"defaultMode": "plan"}}))
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertIsNone(self.compiled_mode(prepared))
        line = [line for line in prepared.notices if "does not know" in line][0]
        self.assertIn("this session uses the managed policy's permission mode", line)
        self.assertNotIn("default permission mode", line)

    def test_a_contract_without_keys_checks_nothing(self):
        self.fake_claude.pop("settings_keys")
        self.runtime = self.make_runtime()
        self.write_user({"permissions": {"defaultMode": "acceptEdits"}, "newerSetting": True})
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertIsNone(self.compiled_mode(prepared))
        self.assertFalse([line for line in prepared.notices if "does not know" in line])


class PinFactsDoctorTests(V4Case):
    """Doctor's pin facts: the pin, its evidence class here, the user's own
    claude by real path, and a staleness Attention without a command."""

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.root / "bin"
        versions = self.root / "home/.local/share/claude/versions"
        versions.mkdir(parents=True)
        self.user_build = versions / "2.1.290"
        self.user_build.write_bytes(b"#!/bin/sh\n")
        self.user_build.chmod(0o755)
        bin_dir.mkdir()
        (bin_dir / "claude").symlink_to(self.user_build)
        self.env["PATH"] = str(bin_dir)
        self.runtime = self.make_runtime(pin_report=True)

    def report(self, today):
        with mock.patch("claude_multi.cli.gateway_facts._doctor_now",
                        lambda: datetime(*today, tzinfo=timezone.utc)):
            return cli_doctor._doctor_pin_report(self.runtime)

    def test_pin_and_user_client_facts(self):
        attention, info = self.report((2026, 9, 26))
        from claude_multi import pin as pin_mod

        platform = pin_mod.host_platform()
        self.assertIn(f"Claude Code pin: 2.1.281 (verified 2026-09-25; evidence on {platform}: battery).", info)
        self.assertIn("Your Claude Code is 2.1.290 (~/.local/share/claude/versions/2.1.290); "
                      "this claude-multi runs 2.1.281.", info)
        # The user's claude is newer: stale, with no command to run.
        self.assertEqual(len(attention), 1)
        self.assertIn("your Claude Code is 2.1.290", attention[0])
        self.assertNotIn("claude-multi update", attention[0])
        self.assertNotIn("run `", attention[0])

    def test_age_alone_makes_the_pin_stale(self):
        self.user_build.unlink()
        older = self.user_build.with_name("2.1.270")
        older.write_bytes(b"#!/bin/sh\n")
        (self.root / "bin/claude").unlink()
        (self.root / "bin/claude").symlink_to(older)
        self.assertEqual(self.report((2026, 10, 20))[0], [])
        attention = self.report((2026, 10, 30))[0]
        self.assertEqual(len(attention), 1)
        self.assertIn("it was verified 35 days ago", attention[0])

    def test_unknown_or_missing_user_client(self):
        (self.root / "bin/claude").unlink()
        info = self.report((2026, 9, 26))[1]
        self.assertIn("Your Claude Code: none on PATH; this claude-multi runs its own copy of 2.1.281.", info)
        script = self.root / "bin/claude"
        script.write_bytes(b"#!/bin/sh\n")
        script.chmod(0o755)
        info = self.report((2026, 9, 26))[1]
        self.assertIn("its path names no version; it is never run to ask", " ".join(info))

    def test_an_injected_binary_seam_turns_the_facts_off(self):
        runtime = self.make_runtime()
        self.assertFalse(runtime.pin_report)
        self.assertEqual(cli_doctor._doctor_pin_report(runtime), ([], []))
