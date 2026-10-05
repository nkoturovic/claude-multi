"""The v4 hooks: the reconcile half and hooks during migration.

The 2.x hook argv (``session-event start|end --managed-id X --launch-epoch
N``, no ``--hook-protocol``) and the protocol-3 start on **v4** records: the
reconcile retargets the runtime id and keeps the record v4 with ``applied``
untouched; model evidence is normalised under the lock by the
``normalize`` callback (merged-catalog forms); the fork and repair texts come
from ``sessions``. The migration-lock wait (≤ 3 s) is driven with an
injected clock.

The record half: the protocol-3
``postmodel`` record seam (``hooks._postmodel`` -> ``record_lead_switch``) and
the resume notice's lead read before ``Runtime`` exists, on sessions really
launched through the v4 path (``_v4.V4Case``).
"""

from __future__ import annotations

import copy
import functools
import io
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _tripwire
from claude_multi import (
    cli,
    custom,
    hooks,
    launch,
    lineup_files,
    migrate,
    scope,
    sessions,
    state,
    strict_json,
    transition,
)
from _catalog import FIXTURE_ROOT
from _v4 import V4Case
from test_migrate import (
    FORK,
    LCAT,
    M1,
    M2,
    NOW,
    RUNTIME,
    SCHEMA,
    SOL_HIGH,
    _cli_environ,
    _local_cat,
    _retired_entry,
    _write_raw,
    legacy_rows,
    v3_managed,
    v3_ordinary,
)
import claude_multi.catalog
import claude_multi.sessions

_tripwire.install()

LEAD_SELECTOR = LCAT.lines["opus55"]["selector"]
LEAD_WIRE = LCAT.lines["opus55"]["wire_model"]


class HookV4TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-hooks-v4-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.root = sessions.state_root(self.environ)
        self.store = sessions.SessionStore(self.root, SCHEMA)
        sessions.write_state_marker(self.root)
        (self.tmp / "project").mkdir()
        self.cwd = str(self.tmp / "project")

    def runtime(self) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ, cwd=self.cwd)

    def migrated(self, raw: dict, cat=LCAT) -> dict:
        record = migrate.convert_record(raw, cat=cat, now=NOW).record
        self.store.save(record)
        return record

    def compiled(self, raw: dict) -> dict:
        """A v4 record with a 3.0 compile behind it (gen 1; fence + scope lead)."""

        record = migrate.convert_record(raw, cat=LCAT, now=NOW).record
        record.pop("migration")
        record["migrated_from_version"] = None
        record["lineup_generation"] = 1
        record["launch_fence"] = "sha256:" + "1" * 64
        record["scope_lead"] = sessions.lead_ref(record["applied"]["lead"])
        self.store.save(record)
        return record

    def hook(self, event: str, payload: dict, *, epoch: int, protocol: bool = False,
             runtime: cli.Runtime | None = None) -> tuple[int, str, str]:
        argv = ["session-event", event, "--managed-id", M1, "--launch-epoch", str(epoch)]
        if protocol:
            argv += ["--hook-protocol", "3"]
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", errors):
            code = cli.main(
                argv,
                runtime=runtime or self.runtime(),
                input_stream=io.StringIO(strict_json.canonical_bytes(payload).decode("utf-8")),
                output_stream=output,
                interactive=False,
            )
        return code, output.getvalue(), errors.getvalue()

    def start(self, source: str, *, epoch: int = 1, model: str | None = None,
              session_id: str = RUNTIME, **extra) -> tuple[int, str, str]:
        protocol = extra.pop("protocol", False)
        payload = {"hook_event_name": "SessionStart", "session_id": session_id,
                   "source": source, "cwd": self.cwd, **extra}
        if model is not None:
            payload["model"] = model
        return self.hook("start", payload, epoch=epoch, protocol=protocol)

    def disk(self) -> dict:
        return strict_json.loads((self.store.sessions_dir / f"{M1}.json").read_bytes())


class V4ReconcileTests(HookV4TestCase):
    def test_2x_argv_on_gen0_and_gen1_records_reconciles_and_keeps_v4(self) -> None:
        for builder in (self.migrated, self.compiled):
            with self.subTest(builder=builder.__name__):
                record = builder(v3_managed(M1, cwd=self.cwd))
                code, output, errors = self.start("resume", model=LEAD_SELECTOR)
                self.assertEqual(code, 0, errors)
                self.assertEqual(output, "")
                disk = self.disk()
                self.assertEqual(disk["version"], 4)
                self.assertEqual(disk["runtime_session_id"], RUNTIME)
                self.assertIn(M1, [a["session_id"] for a in disk["runtime_aliases"]])
                self.assertEqual(disk["applied"], record["applied"])
                self.assertEqual(disk["applied_hash"], record["applied_hash"])
                self.assertEqual(disk["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
                self.assertNotIn("observed_model", disk)
                code, _out, errors = self.hook(
                    "end", {"hook_event_name": "SessionEnd", "session_id": RUNTIME,
                            "reason": "exit"}, epoch=1,
                )
                self.assertEqual(code, 0, errors)
                self.assertEqual(self.disk()["last_event_source"], "end")
                self.assertEqual(self.disk()["version"], 4)
                os.unlink(self.store.sessions_dir / f"{M1}.json")

    def test_migrated_managed_equivalent_forms_need_no_repair(self) -> None:
        cat = _local_cat(retired={"opus@4.8": _retired_entry(
            "opus", since=33, wire="claude-opus-old", generation="4.8",
            selectors={"claude-multi-opus-old[1m]": None})})
        for model in (LEAD_SELECTOR, LEAD_WIRE, LEAD_WIRE + "[1m]"):
            with self.subTest(model=model):
                self.migrated(v3_managed(M1, cwd=self.cwd))
                code, _out, errors = self.start("startup", model=model)
                self.assertEqual(code, 0, errors)
                self.assertNotIn("observed_model", self.disk())
                self.assertEqual(self.disk()["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
                os.unlink(self.store.sessions_dir / f"{M1}.json")
        # a record whose lead translated to a retired generation: last_wire
        raw = v3_managed(M1, cwd=self.cwd, catalog_version=32,
                         lead={"model": "opus", "client_selector": "claude-multi-opus-old[1m]"})
        record = self.migrated(raw, cat)
        self.assertEqual(record["applied"]["lead"]["key"], "opus@4.8")
        runtime = self.runtime()
        with mock.patch.object(runtime, "lineup_catalog", return_value=cat):
            for model in ("claude-opus-old", "claude-opus-old[1m]", "claude-multi-opus-old[1m]"):
                with self.subTest(model=model):
                    code, _out, errors = self.hook(
                        "start", {"session_id": RUNTIME, "source": "startup", "model": model},
                        epoch=1, runtime=runtime,
                    )
                    self.assertEqual(code, 0, errors)
                    self.assertNotIn("observed_model", self.disk())

    def test_migrated_ordinary_matches_any_form_of_its_line(self) -> None:
        self.migrated(v3_ordinary(M1, model="sol", cwd=self.cwd))
        forms = [selector for _e, selector, _c in claude_multi.catalog.line_selectors(LCAT.lines["sol"])]
        for model in (*forms, LCAT.lines["sol"]["wire_model"]):
            with self.subTest(model=model):
                code, _out, errors = self.start("resume", epoch=2, model=model)
                self.assertEqual(code, 0, errors)
                self.assertNotIn("observed_model", self.disk())
        # its retired entry's forms too (a record kept on a retired key)
        os.unlink(self.store.sessions_dir / f"{M1}.json")
        self.migrated(v3_ordinary(M1, model="muse-spark", cwd=self.cwd))
        for model in ("claude-multi-muse-spark-xhigh[1m]", "muse-spark-1.3[1m]"):
            with self.subTest(model=model):
                code, _out, errors = self.start("resume", epoch=2, model=model)
                self.assertEqual(code, 0, errors)
                self.assertNotIn("observed_model", self.disk())

    def test_migrated_ordinary_custom_lead_matches_its_custom_forms(self) -> None:
        custom.add_provider(self.environ, "my-lab", base_url="https://lab.example.com/v1",
                            auth_kind="bearer", secret_env="MY_LAB_API_KEY")
        custom.add_model(self.environ, "lab-model", wire_model="lab-wire-1",
                         provider="my-lab", context_tokens=200000, created_via="discover")
        runtime = self.runtime()
        self.migrated(v3_ordinary(M1, model="lab-model", context_profile="custom-1",
                                  cwd=self.cwd), runtime.lineup_catalog())
        for model in ("custom-lab-model", "lab-wire-1"):
            with self.subTest(model=model):
                code, _out, errors = self.start("resume", epoch=2, model=model)
                self.assertEqual(code, 0, errors)
                self.assertNotIn("observed_model", self.disk())
                self.assertEqual(self.disk()["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_genuine_switch_is_repair_evidence_and_compact_drops_the_model(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd))
        code, output, errors = self.start("startup", model=SOL_HIGH)
        self.assertEqual(code, 0, errors)
        disk = self.disk()
        self.assertEqual(disk["observed_model"], SOL_HIGH)
        self.assertEqual(disk["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        context = strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, sessions.relink_message(disk))
        self.assertIn(f"claude-multi -r {M1}", context)
        self.assertNotIn("sessions transition", context)
        # evidence survives a compact (model and cwd dropped) and a resume
        # start without a model
        self.start("compact", model=LEAD_SELECTOR)
        self.assertEqual(self.disk()["observed_model"], SOL_HIGH)
        self.start("resume")
        self.assertEqual(self.disk()["observed_model"], SOL_HIGH)
        self.assertEqual(self.disk()["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        # an equivalent report clears it
        self.start("resume", model=LEAD_SELECTOR)
        self.assertNotIn("observed_model", self.disk())
        self.assertEqual(self.disk()["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_compact_drops_model_and_cwd_on_v4(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd))
        payload = {"session_id": RUNTIME, "source": "compact", "model": SOL_HIGH, "cwd": "/else"}
        code, _out, errors = self.hook("start", payload, epoch=1)
        self.assertEqual(code, 0, errors)
        disk = self.disk()
        self.assertNotIn("observed_model", disk)
        self.assertNotIn("observed_cwd", disk)
        self.assertEqual(disk["last_event_source"], "compact")

    def test_fork_text_uses_profile_or_direct(self) -> None:
        for raw, flag in (
            (v3_managed(M1, cwd=self.cwd, composition_name="fable"), "--profile max"),
            (v3_ordinary(M1, model="sol", cwd=self.cwd), "--direct sol"),
        ):
            with self.subTest(flag=flag), legacy_rows():
                record = self.migrated(raw)
                code, output, errors = self.start(
                    "fork", epoch=record["launch_epoch"], session_id=FORK
                )
                self.assertEqual(code, 0, errors)
                context = strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"`claude-multi sessions link {FORK} {flag}`", context)
                self.assertNotIn("--composition", context)
                self.assertNotIn("--model", context)
                os.unlink(self.store.sessions_dir / f"{M1}.json")

    def test_session_end_from_a_fork_runtime_records_nothing(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd, last_event_source="resume"))
        before = (self.store.sessions_dir / f"{M1}.json").read_bytes()
        code, _out, errors = self.hook(
            "end", {"hook_event_name": "SessionEnd", "session_id": FORK, "reason": "exit"},
            epoch=1,
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.store.sessions_dir / f"{M1}.json").read_bytes(), before)

    def test_protocol_3_start_reconciles_a_v4_record(self) -> None:
        self.compiled(v3_managed(M1, cwd=self.cwd))
        code, _out, errors = self.hook(
            "start", {"session_id": RUNTIME, "source": "resume", "model": LEAD_SELECTOR},
            epoch=1, protocol=True,
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.disk()["runtime_session_id"], RUNTIME)
        self.assertEqual(self.disk()["version"], 4)

    def test_lineup_catalog_is_built_once_per_hook(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd))
        runtime = self.runtime()
        with mock.patch.object(runtime, "lineup_catalog", wraps=runtime.lineup_catalog) as spy:
            code, _out, errors = self.hook(
                "start", {"session_id": RUNTIME, "source": "startup", "model": SOL_HIGH},
                epoch=1, runtime=runtime,
            )
        self.assertEqual(code, 0, errors)
        self.assertEqual(spy.call_count, 1)


class NormalizeCallbackTests(HookV4TestCase):
    def test_admissibility_is_decided_on_the_locked_view(self) -> None:
        runtime = self.runtime()
        v4 = migrate.convert_record(v3_ordinary(M1, model="sol"), cat=LCAT, now=NOW).record
        v3 = v3_ordinary(M1, model="sol")
        compact = cli._normalize_model_evidence(runtime, source="compact", agent_context=False)
        # a v3 ordinary record keeps a compact's model (2.26 rule) ...
        self.assertEqual(compact(v3, SOL_HIGH, "/x"), sessions.Evidence("sol", "large", SOL_HIGH, "/x"))
        # ... the same event on the v4 view drops model and cwd
        self.assertEqual(compact(v4, SOL_HIGH, "/x"), sessions.Evidence(None, None, None, None))
        agent = cli._normalize_model_evidence(runtime, source="startup", agent_context=True)
        self.assertEqual(agent(v3, SOL_HIGH, "/x"), sessions.Evidence(None, None, None, None))
        startup = cli._normalize_model_evidence(runtime, source="startup", agent_context=False)
        self.assertEqual(startup(v4, None, "/x"), sessions.Evidence(None, None, None, "/x"))
        self.assertIs(startup(v4, SOL_HIGH, "/x").model, sessions.MODEL_EQUIVALENT)
        self.assertEqual(startup(v4, "other[1m]", None).model, "other[1m]")

    def test_line_forms_never_raise(self) -> None:
        self.assertEqual(cli._line_forms(LCAT, "no-such-key"), set())
        self.assertIn(LCAT.lines["opus55"]["wire_model"] + "[1m]", cli._line_forms(LCAT, "opus55"))
        self.assertEqual(cli._line_forms(object(), "opus55"), set())


class MigrationWindowTests(HookV4TestCase):
    """Hooks during migration: wait ≤ 3 s, then skip."""

    def hold(self) -> sessions.state.FileLock:
        lock = sessions.migration_lock(self.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        return lock

    def fake_wait(self, lock, release_at: float | None):
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if release_at is not None and clock[0] >= release_at:
                lock.release()

        return functools.partial(
            sessions.migration_window_wait, clock=lambda: clock[0], sleep=sleep
        ), clock

    def test_start_waits_for_a_short_migration_then_reconciles(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd))
        lock = self.hold()
        waiter, clock = self.fake_wait(lock, release_at=1.0)
        with mock.patch.object(claude_multi.sessions, "migration_window_wait", side_effect=waiter):
            code, _out, errors = self.start("resume", model=LEAD_SELECTOR)
        self.assertEqual(code, 0, errors)
        self.assertGreaterEqual(clock[0], 1.0)
        self.assertLess(clock[0], 3.0)
        self.assertEqual(self.disk()["runtime_session_id"], RUNTIME)

    def test_start_skips_after_three_seconds(self) -> None:
        # a still-v3 record: 2.x argv, the lock held past the bounded wait
        _write_raw(self.store, v3_managed(M1, cwd=self.cwd))
        before = (self.store.sessions_dir / f"{M1}.json").read_bytes()
        lock = self.hold()
        waiter, clock = self.fake_wait(lock, release_at=None)
        with mock.patch.object(claude_multi.sessions, "migration_window_wait", side_effect=waiter):
            code, output, errors = self.start("resume", model=LEAD_SELECTOR)
            end_code, _o, end_errors = self.hook(
                "end", {"hook_event_name": "SessionEnd", "session_id": M1, "reason": "exit"},
                epoch=1,
            )
        self.assertEqual((code, end_code), (0, 0))
        self.assertGreaterEqual(clock[0], 3.0)
        self.assertEqual(output, "")
        self.assertIn("migration in progress", errors)
        self.assertIn("migration in progress", end_errors)
        self.assertEqual((self.store.sessions_dir / f"{M1}.json").read_bytes(), before)

    def test_shared_launcher_holds_never_make_hooks_wait(self) -> None:
        self.migrated(v3_managed(M1, cwd=self.cwd))
        results = []
        with sessions.launcher_write_guard(self.root):
            with mock.patch.object(claude_multi.sessions, "migration_window_wait",
                                   wraps=sessions.migration_window_wait) as wait:
                threads = [
                    threading.Thread(target=lambda: results.append(
                        self.start("resume", model=LEAD_SELECTOR)[0]))
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(10)
        self.assertEqual(results, [0, 0])
        self.assertTrue(all(call.args for call in wait.call_args_list))
        self.assertEqual(self.disk()["runtime_session_id"], RUNTIME)


# ---------------------------------------------------------------- record half


class _SeamCase(V4Case):
    """A session really launched from ``balanced`` (gen 1, following)."""

    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]
        self.rid = self.record["runtime_session_id"]
        self.lead_set = strict_json.loads((self.live(self.mid) / "lead-set.json").read_bytes())
        lead = self.record["applied"]["lead"]
        # derived from the compiled lead set, never a pinned id: an in-set row
        # on another line (same family: an allow, not an ask)
        self.row = next(
            row for row in self.lead_set["rows"]
            if row["key"] != lead["key"] and row["family"] == self.lead_set["lead"]["family"]
        )

    def argv(self, event: str, *, epoch: int | None = None) -> list[str]:
        epoch = self.record["launch_epoch"] if epoch is None else epoch
        return ["session-event", event, "--managed-id", self.mid,
                "--launch-epoch", str(epoch), "--hook-protocol", "3"]

    def run_main(self, argv: list[str], payload: dict, **kwargs) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", errors):
            code = cli.main(
                argv,
                input_stream=io.StringIO(strict_json.canonical_bytes(payload).decode("utf-8")),
                output_stream=output,
                interactive=False,
                **kwargs,
            )
        return code, output.getvalue(), errors.getvalue()

    def postmodel(self, to_model: str | None = None, *, source: str = "command",
                  rid: str | None = None, epoch: int | None = None) -> tuple[int, str, str]:
        payload = {"hook_event_name": "PostModelSwitch", "session_id": rid or self.rid,
                   "source": source, "to_model": to_model or self.row["selector"],
                   "from_model": self.record["applied"]["lead"]["selector"]}
        return self.run_main(self.argv("postmodel", epoch=epoch), payload, runtime=self.runtime)

    def context(self, output: str) -> str:
        return strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]

    def pin(self) -> dict:
        """The record pinned (``follow: false``), so a switch prints nothing."""

        record = copy.deepcopy(self.store.load(self.mid))
        record["follow"] = False
        self.store.save(record)
        return record

    def expected_lead(self, before: dict) -> dict:
        lead = before["applied"]["lead"]
        return {**lead, "key": self.row["key"], "selector": self.row["selector"],
                "effort": self.row["effort"] or lead["effort"], "generation": None}


class PostmodelSeamTests(_SeamCase):
    def test_user_sources_record_the_switch(self) -> None:
        for source in sorted(hooks.USER_SWITCH_SOURCES):
            with self.subTest(source=source):
                before = self.pin()
                code, output, errors = self.postmodel(source=source)
                self.assertEqual((code, output), (0, ""), errors)
                saved = self.store.load(self.mid)
                self.assertEqual(saved["applied"]["lead"], self.expected_lead(before))
                self.assertNotEqual(saved["applied_hash"], before["applied_hash"])
                self.assertEqual(saved["applied_hash"], strict_json.bundle_digest(saved["applied"]))
                self.assertEqual(saved["mutation_token"], before["mutation_token"])
                self.assertEqual(saved["launch_epoch"], before["launch_epoch"])
                # back onto the launch lead for the next source
                self.store.save(before)

    def test_other_sources_and_out_of_set_targets_record_nothing(self) -> None:
        before = self.record_bytes(self.mid)
        for source in ("auto", "resume", "startup"):
            with self.subTest(source=source):
                code, output, errors = self.postmodel(source=source)
                self.assertEqual((code, output), (0, ""), errors)
        outside = next(
            selector for selector in strict_json.loads(
                (self.live(self.mid) / "settings.json").read_bytes())["availableModels"]
            if lineup_files.normalize_model(selector) not in {
                lineup_files.normalize_model(row["selector"]) for row in self.lead_set["rows"]}
        )
        code, output, errors = self.postmodel(outside)
        self.assertEqual(code, 0, errors)
        self.assertIn("outside its lead set", self.context(output))
        self.assertEqual(self.record_bytes(self.mid), before)

    def test_stale_epoch_fork_runtime_gen0_v3_and_absent_records_are_untouched(self) -> None:
        before = self.record_bytes(self.mid)
        for label, kwargs in (
            ("stale epoch", {"epoch": self.record["launch_epoch"] - 1}),
            ("newer epoch", {"epoch": self.record["launch_epoch"] + 1}),
            ("fork runtime", {"rid": FORK}),
        ):
            with self.subTest(label):
                code, output, errors = self.postmodel(**kwargs)
                self.assertEqual((code, output), (0, ""), errors)
                self.assertEqual(self.record_bytes(self.mid), before)
        gen0 = copy.deepcopy(self.record)
        gen0["lineup_generation"] = 0
        for key in ("launch_fence", "scope_lead"):
            gen0.pop(key)
        self.store.save(gen0)
        gen0_bytes = self.record_bytes(self.mid)
        self.assertEqual(self.postmodel()[:2], (0, ""))
        self.assertEqual(self.record_bytes(self.mid), gen0_bytes)
        os.unlink(self.store.sessions_dir / f"{self.mid}.json")
        _write_raw(self.store, v3_managed(self.mid, cwd=str(self.project)))
        v3_bytes = self.record_bytes(self.mid)
        self.assertEqual(self.postmodel(rid=self.mid)[:2], (0, ""))
        self.assertEqual(self.record_bytes(self.mid), v3_bytes)
        os.unlink(self.store.sessions_dir / f"{self.mid}.json")
        code, output, errors = self.postmodel()
        self.assertEqual((code, output), (0, ""), errors)
        self.assertFalse((self.store.sessions_dir / f"{self.mid}.json").exists())
        self.assertFalse((self.store.root / hooks.HOOK_ERRORS_LOG).exists())

    def test_exceptions_exit_zero_with_the_failure_message(self) -> None:
        before = self.record_bytes(self.mid)
        with mock.patch.object(sessions.SessionStore, "record_lead_switch",
                               side_effect=RuntimeError("boom")):
            code, output, errors = self.postmodel()
        self.assertEqual(code, 0)
        self.assertEqual(
            strict_json.loads(output),
            {"systemMessage": "claude-multi postmodel failed: RuntimeError — run claude-multi doctor"},
        )
        self.assertIn("postmodel hook ignored", errors)
        self.assertEqual(self.record_bytes(self.mid), before)
        log = (self.store.root / hooks.HOOK_ERRORS_LOG).read_bytes().splitlines()
        self.assertEqual(strict_json.loads(log[-1])["class"], "RuntimeError")

    def test_a_resume_prepared_before_the_switch_fails_its_cas(self) -> None:
        prepared = self.prepare_resume(self.mid)
        self.pin()
        self.postmodel()
        switched, execs = self.record_bytes(self.mid), len(self.execs)
        with self.assertRaisesRegex(launch.LaunchError, "lineup changed after preparation"):
            self.runtime.perform(prepared)
        self.assertEqual(self.record_bytes(self.mid), switched)
        self.assertEqual(len(self.execs), execs)

    def test_following_record_is_pinned_and_resume_keeps_the_switched_lead(self) -> None:
        self.assertTrue(self.record["follow"])
        code, output, errors = self.postmodel()
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.context(output), hooks.PIN_WARNING.format(m8=self.mid[:8]))
        saved = self.store.load(self.mid)
        self.assertFalse(saved["follow"])
        self.assertEqual(saved["profile"], "balanced")
        self.assertEqual(saved["applied"]["lead"], self.expected_lead(self.record))
        prepared = self.prepare_resume(self.mid)
        self.assertEqual(prepared.record["applied"]["lead"]["key"], self.row["key"])
        argv = list(prepared.result.argv)
        self.assertEqual(argv[argv.index("--model") + 1], self.row["selector"])
        self.runtime.perform(prepared)
        self.assertEqual(self.store.load(self.mid)["applied"]["lead"]["key"], self.row["key"])

    def test_a_switch_onto_lead_target_keeps_follow(self) -> None:
        record = copy.deepcopy(self.record)
        effort = self.row["effort"] or record["applied"]["lead"]["effort"]
        record["lead_target"] = {"key": self.row["key"], "effort": effort,
                                 "selector": self.row["selector"]}
        self.store.save(record)
        code, output, errors = self.postmodel()
        self.assertEqual((code, output), (0, ""), errors)
        saved = self.store.load(self.mid)
        self.assertTrue(saved["follow"])
        self.assertNotIn("lead_target", saved)
        self.assertEqual(saved["applied"]["lead"]["key"], self.row["key"])

    def test_the_seam_never_writes_scope_lead_launch_fence_or_scope_files(self) -> None:
        tree = self.tree(self.live(self.mid))
        self.postmodel()
        saved = self.store.load(self.mid)
        self.assertEqual(saved["scope_lead"], self.record["scope_lead"])
        self.assertEqual(saved["launch_fence"], self.record["launch_fence"])
        self.assertEqual(saved["lineup_generation"], self.record["lineup_generation"])
        self.assertEqual(self.tree(self.live(self.mid)), tree)


class PostmodelMigrationWindowTests(_SeamCase):
    def test_a_one_second_migration_is_waited_for_then_recorded(self) -> None:
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        timer = threading.Timer(1.0, lock.release)
        timer.start()
        self.addCleanup(timer.cancel)
        code, _output, errors = self.postmodel()
        timer.join(10)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.store.load(self.mid)["applied"]["lead"]["key"], self.row["key"])

    def test_a_lock_held_past_the_wait_records_nothing(self) -> None:
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        waiter = functools.partial(
            sessions.migration_window_wait, clock=lambda: clock[0], sleep=sleep
        )
        before = self.record_bytes(self.mid)
        with mock.patch.object(hooks.sessions, "migration_window_wait", side_effect=waiter):
            code, output, errors = self.postmodel()
        self.assertEqual((code, output), (0, ""), errors)
        self.assertGreaterEqual(clock[0], 3.0)
        self.assertIn("the lead switch was not recorded", errors)
        self.assertEqual(self.record_bytes(self.mid), before)


class ResumeNoticeLeadTests(_SeamCase):
    """The resume notice names ``applied.lead``."""

    def start(self, **kwargs) -> tuple[int, str, str]:
        payload = {"hook_event_name": "SessionStart", "session_id": self.rid,
                   "source": "resume", "cwd": str(self.project)}
        return self.run_main(self.argv("start"), payload, **kwargs)

    def recorded(self, row: dict, effort: str) -> str:
        return f"Your recorded lead is {row['display']} · {effort} (`{row['selector']}`)"

    def test_notice_names_the_launch_lead_then_the_switched_lead(self) -> None:
        lead = self.lead_set["lead"]
        code, output, errors = self.start(runtime=self.runtime)
        self.assertEqual(code, 0, errors)
        self.assertIn(self.recorded(lead, lead["effort"]), self.context(output))
        self.postmodel()
        code, output, errors = self.start(runtime=self.runtime)
        self.assertEqual(code, 0, errors)
        effort = self.row["effort"] or lead["effort"]
        self.assertIn(self.recorded(self.row, effort), self.context(output))
        self.assertNotIn(f"(`{lead['selector']}`)", self.context(output))

    def test_the_lead_is_read_before_runtime_exists(self) -> None:
        self.postmodel()
        effort = self.row["effort"] or self.lead_set["lead"]["effort"]
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch('claude_multi.cli.runtime.Runtime', side_effect=RuntimeError("no runtime in this test")
        ) as factory:
            code, output, errors = self.start()
        self.assertEqual(code, 0, errors)
        factory.assert_called()
        context = self.context(output)
        self.assertIn(self.recorded(self.row, effort), context)
        self.assertIn("could not record this session start", context)

    def test_unmatched_row_and_unreadable_record_fall_back(self) -> None:
        record = copy.deepcopy(self.store.load(self.mid))
        lead = record["applied"]["lead"]
        self.assertEqual(
            hooks.notice_lead({**record, "lineup_generation": 0}, self.live(self.mid)), None
        )
        self.assertIsNone(hooks.notice_lead({"version": 3}, self.live(self.mid)))
        self.assertIsNone(hooks.notice_lead({**record, "applied": {}}, self.live(self.mid)))
        empty = self.root / "no-scope"
        self.assertEqual(
            hooks.notice_lead(record, empty),
            {"display": lead["key"], "effort": lead["effort"], "family": "?",
             "selector": lead["selector"]},
        )
        # a damaged record: the scope fallback (lead-set.json["lead"])
        path = self.store.sessions_dir / f"{self.mid}.json"
        state.atomic_write(path, b"{not json")
        self.assertIsNone(cli._notice_record_lead(self.store.root, self.mid))
        other = "55555555-5555-4555-8555-555555555555"
        self.assertIsNone(cli._notice_record_lead(self.store.root, other))
        self.assertFalse((self.store.sessions_dir / f"{other}.json").exists())


class ConvergeAfterSwitchTests(_SeamCase):
    """A /model switch is never drift; converge and repair keep every byte."""

    def test_expected_plan_and_repair_after_a_switch(self) -> None:
        self.postmodel()
        record = self.store.load(self.mid)
        parts = self.runtime.converge_parts()
        live = self.live(self.mid)
        ep = transition.expected_plan(
            record, docs=parts.docs, prompt_bodies=parts.prompt_bodies,
            state_root=self.store.root, hook_command=parts.hook_command,
            token_helper_command=parts.token_helper_command,
            environ=parts.environ, managed_root=parts.managed_root, live=live,
        )
        self.assertIsNotNone(ep.plan, ep.reason)
        self.assertTrue(ep.lead_switched)
        self.assertTrue(ep.launch_files_kept)
        self.assertFalse(ep.catalog_launch_differs)
        disk = scope.read_disk_plan(live, ep.plan)
        self.assertEqual(scope.plan_hash(disk), scope.plan_hash(ep.plan))
        self.assertEqual(scope.live_drift(live, ep.plan), [])
        tree, record_bytes = self.tree(live), self.record_bytes(self.mid)
        output = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["doctor", "--repair", self.mid], runtime=self.runtime,
                            output_stream=output, interactive=False)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("live scope matches the record-authoritative compile", output.getvalue())
        self.assertIn("lead switched with /model since the last compile", output.getvalue())
        self.assertIn("restated at the next resume", output.getvalue())
        self.assertEqual(self.tree(live), tree)
        self.assertFalse(self.prev(self.mid).exists())
        self.assertEqual(self.record_bytes(self.mid), record_bytes)


if __name__ == "__main__":
    unittest.main()
