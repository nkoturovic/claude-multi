"""The served-change plan, the gateway mutation barrier and the
single-root authority.

Every test runs in a temp HOME/state root against fixture assets; no test
reaches a gateway or a provider. Barrier contention from "another process"
is a raw ``flock`` on a separate descriptor (flock locks belong to the open
file description, exactly as between processes).
"""

from __future__ import annotations

import ast
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_multi
from claude_multi import cli, continuity, lineup, migrate, proxy, render, sessions, state, strict_json, transition
from claude_multi import served_plan
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _layout import ASSET_ENTRIES, INSTALLED_RESOURCES_RELATIVE, REPO_ROOT, RESOURCES_RELATIVE, RESOURCES_ROOT, fake_checkout
from _v4 import V4Case
from test_lineup import LineupCase
import test_cli
import test_restore2x
import claude_multi.cli.commands.models as models_cmd
import claude_multi.cli.commands.plan as plan_cmd
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.consent as consent_mod
import claude_multi.cli.doctor as doctor_mod
import claude_multi.cli.launch_flow as launch_flow
import claude_multi.cli.runtime as runtime_mod

SRC = REPO_ROOT / "src" / "claude_multi"
LIVE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ENDED = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
# sha256 of schemas/continuity.schema.json at the 3.0.2 release (54ea3ef).
CONTINUITY_SCHEMA_302 = "b26230897ba03d5c90cb0cd09db6f5cb774a3735279482c99ba60360aa707fce"

LAN_ADD = ["providers", "add", "lan", "--kind", "openai-compatible-lan", "--base-url",
           "http://box.lan:8000/v1", "--auth", "none", "--family", "local"]
LAN_MODEL = ["models", "add", "lan", "lan-model", "--as", "custom-lan-model", "--context", "32768",
             "--source", "operator"]
LAN_ALIAS = "custom-lan-model"


def barrier_lock_path(home: Path | str) -> Path:
    return Path(home) / ".config" / "claude-multi" / "served-change.lock"


def barrier_free(home: Path | str) -> bool:
    """Another open file description can take the barrier right now."""

    path = barrier_lock_path(home)
    if not path.exists():
        return True
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def foreign_barrier(home: Path | str):
    """The barrier held by "another process" (a separate descriptor)."""

    directory = state.ensure_private_dir(Path(home) / ".config" / "claude-multi")
    descriptor = os.open(directory / "served-change.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def lenient_record(stem: str, selector: str, *, event: str | None = None) -> dict:
    """A record shape ``continuity.scan_records`` reads (a v3 managed lead)."""

    return {"version": 3, "managed_id": stem, "session_type": "managed-composition",
            "last_event_source": event,
            "snapshot": {"lead": {"model": "x", "client_selector": selector}, "variants": []}}


# ------------------------------------------------------------ the pure planner
class PlannerTests(unittest.TestCase):
    def document(self, **changes) -> dict:
        document = served_plan.parse_restricted_yaml(
            (GOLDENS_ROOT / "render" / "gateway-default.yaml").read_text())
        document.update(changes)
        return document

    def test_published_yaml_inverse_roundtrip_strips_only_key_values(self) -> None:
        text = (GOLDENS_ROOT / "render" / "gateway-default.yaml").read_text()
        document = served_plan.parse_restricted_yaml(text)
        self.assertEqual(render.emit_yaml(document), text)  # a byte-exact inverse
        document["claude-api-key"][0]["headers"] = {"X-Org": "org-secret-value"}
        document["openai-compatibility"][0]["base-url"] = "http://LLM-LOCAL.invalid:8010/v1/"
        emitted = render.emit_yaml(document)
        self.assertEqual(served_plan.parse_restricted_yaml(emitted), document)
        routes = served_plan.published_routes(emitted)
        identity = json.dumps({s: r.as_document() for s, r in routes.items()})
        for value in ("dummy-kimi-key", "a" * 64, "org-secret-value"):
            self.assertNotIn(value, identity)
        kimi = routes["claude-multi-kimi-k3"]
        self.assertEqual((kimi.section, kimi.route, kimi.wire, kimi.auth_header, kimi.header_names),
                         (served_plan.SECTION_KEY, "https://api.kimi.com/coding", "k3", "x-api-key", ("X-Org",)))
        self.assertTrue(kimi.contract)
        self.assertEqual(routes["claude-opus-5"].section, served_plan.SECTION_OAUTH)
        self.assertEqual(routes["claude-multi-qwen-flash-next"].route,
                         "llm-local http://llm-local.invalid:8010/v1")
        self.assertFalse(any(s.startswith(served_plan.SENTINEL_PREFIX) for s in routes))
        for broken in ("", "a:\n\tb: 1\n", "a: [1]\n", "- x\nb: 1\n", "a: b c\n"):
            with self.subTest(broken=broken), self.assertRaises(served_plan.PlanParseError):
                served_plan.parse_restricted_yaml(broken)

    def test_plan_detects_same_selector_wire_retarget(self) -> None:
        before = served_plan.routes_from_document(self.document())
        changed = self.document()
        changed["claude-api-key"][0]["models"][0]["name"] = "k3-turbo"
        plan = served_plan.build_plan(
            before, served_plan.routes_from_document(changed),
            references=served_plan.References({"claude-multi-kimi-k3": frozenset({LIVE})}, frozenset({LIVE}),
                                              slots={LIVE: {"claude-multi-kimi-k3": ("lead",)}}),
            state_root="/state", gateway="127.0.0.1:8317")
        self.assertEqual([s for s, _o, _n in plan.retargeted], ["claude-multi-kimi-k3"])
        self.assertEqual((plan.added, plan.removed), ((), ()))
        text = plan.text()
        self.assertIn("Retargeted\n  claude-multi-kimi-k3: key route https://api.kimi.com/coding (x-api-key) · k3", text)
        self.assertIn("→ key route https://api.kimi.com/coding (x-api-key) · k3-turbo", text)
        self.assertIn(f"Live session impact\n  {LIVE[:8]} lead: claude-multi-kimi-k3 retargets", text)
        self.assertEqual(plan.as_document()["retargeted"][0]["changed"], ["wire"])
        # The same selector, same wire, another effort contract: a retarget too.
        contract = self.document()
        contract["payload"]["override"][0]["params"] = {"output_config.effort": "high"}
        plan = served_plan.build_plan(before, served_plan.routes_from_document(contract),
                                      references=served_plan.References({}, frozenset()),
                                      state_root="/state", gateway="g")
        self.assertEqual([s for s, _o, _n in plan.retargeted], ["claude-multi-kimi-k3"])

    def test_plan_digest_changes_on_input_or_reference_change(self) -> None:
        before = served_plan.routes_from_document(self.document())
        after = dict(before)
        after.pop("claude-multi-qwen-flash-next")
        refs = served_plan.References({}, frozenset())

        def digest(after_routes, references, **kwargs):
            return served_plan.build_plan(before, after_routes, references=references, state_root="/s",
                                          gateway="g", **kwargs).digest

        first = digest(after, refs)
        self.assertEqual(first, digest(dict(after), served_plan.References({}, frozenset())))
        self.assertNotEqual(first, digest(before, refs))
        holder = served_plan.References({"claude-multi-qwen-flash-next": frozenset({ENDED})}, frozenset())
        self.assertNotEqual(first, digest(after, holder))
        live = served_plan.References({"claude-multi-qwen-flash-next": frozenset({ENDED})}, frozenset({ENDED}))
        self.assertNotEqual(digest(after, holder), digest(after, live))
        self.assertNotEqual(first, digest(after, refs, inputs={"candidate": "other"}))
        self.assertNotEqual(first, digest(after, refs, authority_after={"line x": "admitted"}))

    def test_unreadable_fence_makes_destructive_coverage_unknown(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="cm-served-"))
        self.addCleanup(shutil.rmtree, root, True)
        os.chmod(root, 0o700)
        sessions_dir = state.ensure_private_dir(root / "sessions")
        state.atomic_write(sessions_dir / f"{ENDED}.json",
                           strict_json.canonical_file_bytes(lenient_record(ENDED, "claude-opus-5", event="end")))
        scope = state.ensure_private_dir(root / "scopes" / ENDED)
        state.atomic_write(scope / "settings.json", b'{"availableModels": ["claude-multi-qwen-flash-next"]}')
        os.chmod(scope / "settings.json", 0o644)  # group/other readable: refused as unsafe
        scan = continuity.scan_records(root)
        self.assertEqual(scan.fence_unreadable, (ENDED,))
        references = served_plan.references_from_scan(scan)
        self.assertIn(f"unreadable scope fences {ENDED[:8]}", references.unknown)
        before = served_plan.routes_from_document(self.document())
        after = dict(before)
        after.pop("claude-multi-qwen-flash-next")
        plan = served_plan.build_plan(before, after, references=references, state_root=str(root), gateway="g")
        self.assertEqual(plan.coverage, "unknown")
        self.assertEqual(plan.refusal(), served_plan.UNKNOWN_IMPACT.format(reason=references.unknown))
        self.assertIn("This operation cannot safely remove or retarget selectors.", plan.text())
        # An addition alone is not destructive: no refusal.
        additive = served_plan.build_plan(after, before, references=references, state_root=str(root), gateway="g")
        self.assertIsNone(additive.refusal())

    def test_malformed_fence_structure_is_unknown_coverage(self) -> None:
        """A parseable but structurally invalid fence (null, a
        non-list or non-string availableModels, a non-string model) is an
        unknown fence, never an empty one — a live session whose extra alias
        sits only in its fence keeps the destructive plan refused."""

        root = Path(tempfile.mkdtemp(prefix="cm-served-"))
        self.addCleanup(shutil.rmtree, root, True)
        os.chmod(root, 0o700)
        live = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        sessions_dir = state.ensure_private_dir(root / "sessions")
        state.atomic_write(sessions_dir / f"{live}.json",
                           strict_json.canonical_file_bytes(lenient_record(live, "claude-opus-5", event="start")))
        scope = state.ensure_private_dir(root / "scopes" / live)
        before = served_plan.routes_from_document(self.document())
        after = dict(before)
        after.pop("claude-multi-qwen-flash-next")
        for settings in (None, {"availableModels": "claude-multi-qwen-flash-next"},
                         {"availableModels": ["claude-multi-qwen-flash-next", 7]}, {"model": None},
                         {"model": 7}, ["x"]):
            with self.subTest(settings=settings):
                state.atomic_write(scope / "settings.json", strict_json.canonical_file_bytes(settings))
                scan = continuity.scan_records(root)
                self.assertEqual(scan.fence_unreadable, (live,))
                self.assertEqual(scan.notices, (f"record {live[:8]}: scope settings malformed (fence unknown), "
                                                "skipped",))
                plan = served_plan.build_plan(before, after, references=served_plan.references_from_scan(scan),
                                              state_root=str(root), gateway="g")
                self.assertEqual(plan.coverage, "unknown")
                self.assertIsNotNone(plan.refusal())
        # Documented compiled shapes (absent keys included) stay complete.
        for settings in ({}, {"model": "claude-opus-5"}, {"availableModels": ["claude-multi-qwen-flash-next"]}):
            with self.subTest(settings=settings):
                state.atomic_write(scope / "settings.json", strict_json.canonical_file_bytes(settings))
                scan = continuity.scan_records(root)
                self.assertEqual((scan.fence_unreadable, scan.notices), ((), ()))

    def test_continuity_schema_unchanged_for_302(self) -> None:
        schema = (RESOURCES_ROOT / "schemas" / "continuity.schema.json").read_bytes()
        self.assertEqual(hashlib.sha256(schema).hexdigest(), CONTINUITY_SCHEMA_302)
        self.assertEqual((FIXTURE_ROOT / "schemas" / "continuity.schema.json").read_bytes(), schema)
        # The authority is the existing field: no field is added by 3.1.
        self.assertEqual(set(continuity.empty()),
                         {"version", "seeded_through_catalog", "state_root", "aliases", "pruned"})


# ------------------------------------------------------------ the barrier primitive
class BarrierPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="cm-barrier-"))
        os.chmod(self.home, 0o700)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.directory = sessions.barrier_dir(self.home)

    def test_inner_services_require_held_token_without_reacquire(self) -> None:
        token = state.acquire_served_barrier(self.directory, timeout=0.1)
        try:
            # The same thread never re-acquires (it would deadlock on itself).
            with self.assertRaises(state.BarrierTokenError):
                state.acquire_served_barrier(self.directory, timeout=0.1)
            self.assertIs(state.require_barrier(token), token)
        finally:
            token.release()
        for stale in (None, object(), token):
            with self.subTest(token=stale), self.assertRaises(state.BarrierTokenError):
                state.require_barrier(stale)
        with self.assertRaises(state.BarrierTokenError):
            proxy.render_runtime_config(self.home, environ={"HOME": str(self.home)}, barrier=token)
        with self.assertRaises(state.BarrierTokenError):
            proxy.prune_aliases(self.home, self.home / "state", None, environ={"HOME": str(self.home)},
                                barrier=token)
        # Another thread waits like another process, then times out neutrally.
        held = state.acquire_served_barrier(self.directory, timeout=0.1)
        box: list = []
        worker = threading.Thread(target=lambda: box.append(
            self._attempt(lambda: state.acquire_served_barrier(self.directory, timeout=0.2))))
        worker.start()
        worker.join(5)
        held.release()
        self.assertIsInstance(box[0], state.BarrierBusyError)
        self.assertEqual(box[0].strerror, state.BARRIER_BUSY_TEXT)

    @staticmethod
    def _attempt(call):
        try:
            return call()
        except BaseException as exc:
            return exc


class InnerServiceTokenTests(LineupCase):
    """Lineup decide/converge/propagation/render consume the held
    token and never acquire; a released token is refused."""

    def test_inner_services_require_held_token_without_reacquire(self) -> None:
        acquire = mock.Mock(side_effect=AssertionError("an inner service re-acquired the barrier"))
        with lineup.served_phase(self.runtime) as token:
            with mock.patch.object(state, "acquire_served_barrier", acquire):
                report = transition.converge(self.store.root, self.store, self.mid,
                                             runtime_parts=self.runtime.converge_parts(), barrier=token)
                self.assertTrue(report)
                decision = lineup.locked_decision(self.runtime, self.mid, lambda r: lineup.Decision("skip"),
                                                  barrier=token)
                self.assertEqual(decision.kind, "skip")
                self.assertEqual(lineup.on_saved(self.runtime, ["nobody"], apply_live=True, out=io.StringIO(),
                                                 barrier=token), 0)
            acquire.assert_not_called()
        for call in (
            lambda: transition.converge(self.store.root, self.store, self.mid,
                                        runtime_parts=self.runtime.converge_parts(), barrier=token),
            lambda: lineup.locked_decision(self.runtime, self.mid, lambda r: lineup.Decision("skip"),
                                           barrier=token),
            lambda: lineup.on_saved(self.runtime, ["x"], apply_live=True, out=io.StringIO(), barrier=token),
            lambda: lineup.on_removed(self.runtime, "x", renamed_to=None, out=io.StringIO(), barrier=token),
        ):
            with self.assertRaises(state.BarrierTokenError):
                call()


# ------------------------------------------------------------ propagation inside a served phase
class ServedPropagationTests(LineupCase):
    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        self.save_profile("mine", lambda d: None)
        self.follower = self.launch_fresh(self.profile_target("mine"))
        self.mid = self.follower["managed_id"]
        self.runtime.profiles.update("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))

    def test_import_with_live_followers_does_not_self_deadlock(self) -> None:
        # An import or a served mutation saves profiles inside its own
        # phase and propagates with the held token: no second acquisition.
        done = threading.Event()
        box: list = []

        def served_mutation() -> None:
            try:
                with lineup.served_phase(self.runtime) as token:
                    out = io.StringIO()
                    box.append(lineup.on_saved(self.runtime, ["mine"], apply_live=True, out=out,
                                               barrier=token, served=True))
                    box.append(out.getvalue())
                    # Re-entering propagation without the token refuses at
                    # once — never a hang on its own descriptor.
                    try:
                        lineup.on_saved(self.runtime, ["mine"], apply_live=True, out=io.StringIO())
                    except state.BarrierTokenError as exc:
                        box.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=served_mutation, daemon=True)
        worker.start()
        self.assertTrue(done.wait(20), "a served mutation deadlocked on its own barrier")
        self.assertEqual(box[0], 1)
        self.assertIn("recorded as pending", box[1])
        self.assertIsInstance(box[2], state.BarrierTokenError)
        self.assertTrue(barrier_free(self.env["HOME"]))

    def test_served_followers_get_pending_not_live(self) -> None:
        generation = self.store.load(self.mid)["lineup_generation"]
        scope_before = self.tree(self.live(self.mid))
        with lineup.served_phase(self.runtime) as token:
            out = io.StringIO()
            written = lineup.on_saved(self.runtime, ["mine"], apply_live=True, out=out, barrier=token,
                                      served=True)
        self.assertEqual(written, 1)
        record = self.store.load(self.mid)
        self.assertIn("pending", record)
        self.assertEqual(record["lineup_generation"], generation)
        self.assertEqual(self.tree(self.live(self.mid)), scope_before)
        self.assertNotIn("applied live", out.getvalue())
        # Explicit (unrelated) profile propagation keeps LIVE under one phase.
        self.mutate(self.mid, applied=record["applied"])
        record = self.store.load(self.mid)
        record.pop("pending")
        record["mutation_token"] = sessions.new_mutation_token()
        self.store.save(record)
        out = io.StringIO()
        lineup.on_saved(self.runtime, ["mine"], apply_live=True, out=out)
        self.assertIn("applied live", out.getvalue())


# ------------------------------------------------------------ /cm and relaunch
class LineupBarrierTests(LineupCase):
    def test_lineup_relaunch_releases_barrier_before_confirm_and_launch(self) -> None:
        home = self.env["HOME"]
        seen: list = []
        real_acquire = state.acquire_served_barrier

        def acquire(*args, **kwargs):
            seen.append("acquire")
            return real_acquire(*args, **kwargs)

        def confirm(*_args, **_kwargs):
            seen.append(("confirm", barrier_free(home)))
            return True

        real_perform = runtime_mod.Runtime.perform

        def perform(runtime, prepared, **kwargs):
            seen.append(("perform", barrier_free(home)))
            return real_perform(runtime, prepared, **kwargs)

        self.write_transcript(self.rid)
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch('claude_multi.cli.screens.transition._transition_confirm', side_effect=confirm), \
                mock.patch.object(state, "acquire_served_barrier", side_effect=acquire), \
                mock.patch.object(runtime_mod.Runtime, "perform", autospec=True, side_effect=perform):
            code, out, err = self.lineup("--relaunch", "profile", "quality", skill=False,
                                         interactive=True, text="")
        self.assertEqual(code, 0, out + err)
        # decide (one phase) -> released -> confirm and perform barrier-free
        # -> perform_launch takes its own bounded barrier.
        self.assertEqual(seen, ["acquire", ("confirm", True), ("perform", True), "acquire"])
        self.assertTrue(barrier_free(home))
        self.assertEqual(self.store.load(self.mid)["profile"], "quality")

    def test_cm_apply_waits_bounded_then_refuses_neutrally(self) -> None:
        self.runtime.served_barrier_timeout = 0.2
        before = self.record_bytes(self.mid)
        with foreign_barrier(self.env["HOME"]):
            started = time.monotonic()
            code, out, err = self.request("set implementer=opus55:xhigh")
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(lineup.R27, out + err)
        self.assertEqual(self.record_bytes(self.mid), before)


# ------------------------------------------------------------ launch
class LaunchBarrierTests(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.env["HOME"]

    def test_launch_barrier_timeout_never_spans_card(self) -> None:
        self.runtime.served_barrier_timeout = 0.2
        acquire = mock.Mock(side_effect=AssertionError("the card/preparation touched the barrier"))
        with foreign_barrier(self.home):
            with mock.patch.object(state, "acquire_served_barrier", acquire):
                prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
            acquire.assert_not_called()
            started = time.monotonic()
            with self.assertRaises(claude_multi.launch.LaunchError) as raised:
                self.runtime.perform(prepared)
            self.assertLess(time.monotonic() - started, 5)
        self.assertIn(state.BARRIER_BUSY_TEXT, str(raised.exception))
        self.assertIn("nothing was launched", str(raised.exception))
        self.assertEqual(self.store.scan_uuid_records(), [])
        self.assertEqual(self.execs, [])
        # With the barrier free the same plan launches, and exec drops it.
        self.runtime.perform(prepared)
        self.assertEqual(len(self.execs), 1)
        self.assertTrue(barrier_free(self.home))

    def test_launch_revalidates_operator_authority_inside_barrier(self) -> None:
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        order: list[str] = []
        real_acquire = state.acquire_served_barrier
        real_revalidate = runtime_mod.Runtime.revalidate_operator

        def acquire(*args, **kwargs):
            token = real_acquire(*args, **kwargs)
            order.append("barrier")
            # Between the card and the commit the gateway authority moved
            # to another root (an adoption): the launch must see it.
            document = continuity.read(Path(self.home)) or continuity.empty()
            document["state_root"] = str(Path(self.root) / "other-root")
            continuity.write(Path(self.home), document)
            return token

        def revalidate(runtime, prepared_arg):
            order.append("operator")
            return real_revalidate(runtime, prepared_arg)

        with mock.patch.object(state, "acquire_served_barrier", side_effect=acquire), \
                mock.patch.object(runtime_mod.Runtime, "revalidate_operator", autospec=True,
                                  side_effect=revalidate):
            with self.assertRaises(claude_multi.launch.LaunchError) as raised:
                self.runtime.perform(prepared)
        self.assertIn("This gateway is managed for state root", str(raised.exception))
        self.assertIn("No served change was applied.", str(raised.exception))
        # The first operator check ran before the barrier (perform's own),
        # the root check inside it refused before any write.
        self.assertEqual(order, ["operator", "barrier"])
        self.assertEqual(self.store.scan_uuid_records(), [])
        self.assertEqual(self.execs, [])
        self.assertTrue(barrier_free(self.home))
        # Same root again: the operator eligibility is re-checked inside too.
        document = continuity.read(Path(self.home))
        document["state_root"] = str(self.store.root)
        continuity.write(Path(self.home), document)
        order.clear()
        with mock.patch.object(runtime_mod.Runtime, "revalidate_operator", autospec=True,
                               side_effect=revalidate):
            self.runtime.perform(prepared)
        self.assertEqual(order, ["operator", "operator"])


# ------------------------------------------------------------ hooks
class HookBarrierTests(LineupCase):
    def test_hook_never_acquires_served_barrier(self) -> None:
        for relative in ("hooks.py", "cli/session_events.py", "lineup_log.py"):
            source = (SRC / relative).read_text()
            for name in ("served_change_phase", "acquire_served_barrier", "served_phase", "require_barrier"):
                self.assertNotIn(name, source, f"{relative} names {name}")
        lead_set = strict_json.loads((self.live(self.mid) / "lead-set.json").read_bytes())
        row = next(r for r in lead_set["rows"] if r["key"] == "fable")
        acquire = mock.Mock(side_effect=AssertionError("a hook path took the barrier"))
        with foreign_barrier(self.env["HOME"]), mock.patch.object(state, "acquire_served_barrier", acquire):
            started = time.monotonic()
            self.store.reconcile_runtime(self.mid, observed_runtime_id=self.rid, source="startup",
                                         cwd=self.runtime.cwd, launch_epoch=1)
            self.assertIn(self.store.record_lead_switch(self.mid, observed_runtime_id=self.rid,
                                                        launch_epoch=1, row=row), ("switched", "pinned"))
            self.store.record_session_end(self.mid, observed_runtime_id=self.rid, reason="exit",
                                          launch_epoch=1)
            self.assertLess(time.monotonic() - started, 3)
        acquire.assert_not_called()
        self.assertEqual(self.store.load(self.mid)["last_event_source"], "end")


# ------------------------------------------------------------ restore-2x
class RestoreConfirmationTests(test_restore2x.RestoreTestCase):
    def test_restore_confirmation_precedes_locks(self) -> None:
        self.migrate(test_restore2x.v3_managed(test_restore2x.M1, last_event_source="resume"))
        home = self.root.parent / "home"
        seen: list = []

        def confirm(pairs):
            seen.append((tuple(p[0] for p in pairs), claude_multi.hooks.migration_lock_held(self.root),
                         barrier_free(home)))
            return True

        order: list[str] = []
        real_acquire = state.acquire_served_barrier

        def acquire(*args, **kwargs):
            order.append("barrier" if claude_multi.hooks.migration_lock_held(self.root) else "barrier-unguarded")
            return real_acquire(*args, **kwargs)

        with mock.patch.object(state, "acquire_served_barrier", side_effect=acquire):
            report = migrate.restore_2x(self.store, home=home, confirm=confirm,
                                        is_live=lambda _view: False)
        self.assertEqual(seen, [((test_restore2x.M1,), False, True)])
        self.assertEqual(order, ["barrier"])  # EX migration first, then the barrier
        self.assertTrue(report.marker_removed)
        self.assertTrue(barrier_free(home))

    def test_restore_revalidates_the_confirmed_snapshot(self) -> None:
        self.migrate(test_restore2x.v3_managed(test_restore2x.M1, last_event_source="resume"))
        home = self.root.parent / "home"

        def confirm(pairs):
            # A record changes between the confirmation and the commit.
            raw, _bytes = self.store.load_raw(test_restore2x.M1)
            changed = {**raw, "mutation_token": sessions.new_mutation_token()}
            self.store._save_lifecycle(changed)
            return True

        before_marker = self.marker()
        with self.assertRaises(migrate.RestoreRefused) as raised:
            migrate.restore_2x(self.store, home=home, confirm=confirm, is_live=lambda _view: False)
        self.assertEqual(list(raised.exception.lines), [migrate.RESTORE_CHANGED])
        self.assertEqual(self.marker(), before_marker)

    def test_restore_refuses_a_resume_under_a_new_runtime_during_confirmation(self) -> None:
        self.migrate(test_restore2x.v3_managed(test_restore2x.M1, last_event_source="resume"))
        home = self.root.parent / "home"
        before = self.store.load(test_restore2x.M1)
        resumed = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

        def confirm(pairs):
            # A real SessionStart resumes the session under a new runtime UUID;
            # neither the last event nor the mutation token moves.
            self.store.reconcile_runtime(test_restore2x.M1, observed_runtime_id=resumed, source="resume",
                                         launch_epoch=before["launch_epoch"])
            return True

        with self.assertRaises(migrate.RestoreRefused) as raised:
            migrate.restore_2x(self.store, home=home, confirm=confirm, is_live=lambda _view: False)
        self.assertEqual(list(raised.exception.lines), [migrate.RESTORE_CHANGED])
        after = self.store.load(test_restore2x.M1)
        self.assertEqual(after["runtime_session_id"], resumed)
        self.assertEqual((after["last_event_source"], after.get("mutation_token")),
                         (before["last_event_source"], before.get("mutation_token")))
        self.assertEqual(after["version"], sessions.RECORD_VERSION)  # not restored, not ended
        self.assertTrue(self.marker())

    def test_restore_commit_rereads_liveness_evidence(self) -> None:
        self.migrate(test_restore2x.v3_managed(test_restore2x.M1, last_event_source="resume"))
        home = self.root.parent / "home"
        probes: list = []

        def refresh():
            # The process inventory read inside the commit phase now names it.
            probes.append((claude_multi.hooks.migration_lock_held(self.root), barrier_free(home)))
            return lambda view: view.get("managed_id") == test_restore2x.M1

        with self.assertRaises(migrate.RestoreRefused) as raised:
            migrate.restore_2x(self.store, home=home, confirm=lambda pairs: True,
                               is_live=lambda _view: False, refresh_live=refresh)
        self.assertEqual(list(raised.exception.lines), [migrate.RESTORE_CHANGED])
        self.assertEqual(probes, [(True, False)])
        self.assertTrue(self.marker())


# ------------------------------------------------------------ operator verbs
class ServedOperatorTests(test_cli.OperatorCommandCase):
    """The shared preflight, the barrier and the root boundary at the verbs."""

    def published(self) -> None:
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)

    def lan_line(self) -> None:
        for argv in (LAN_ADD, LAN_MODEL):
            code, _out, err = self.op(argv)
            self.assertEqual(code, 0, err)

    def config(self) -> Path:
        return proxy.config_dir(self.runtime.home) / "config.yaml"

    def record(self, stem: str, selector: str, *, event: str | None = None) -> Path:
        directory = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        path = directory / f"{stem}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(lenient_record(stem, selector, event=event)))
        return path

    def hand_edit(self, file_id: str, mutate) -> None:
        path = claude_multi.operator.providers_dir(self.env) / f"{file_id}.json"
        document = strict_json.loads(path.read_bytes())
        mutate(document)
        state.atomic_write(path, strict_json.pretty_file_bytes(document))

    def test_all_served_mutators_use_common_preflight(self) -> None:
        # Every phase names its preflight. Only evidence and local admission
        # metadata phases omit the served plan; route mutations never do.
        metadata_phases = {}
        for relative in ("cli/commands/providers.py", "cli/commands/models.py", "cli/onboarding.py"):
            tree = ast.parse((SRC / relative).read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) \
                        == "operator_write":
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    self.assertIn("preflight", keywords, f"{relative}:{node.lineno}")
                    if isinstance(keywords["preflight"], ast.Constant):
                        self.assertIsNone(keywords["preflight"].value)
                        owner = parents[node]
                        while not isinstance(owner, ast.FunctionDef):
                            owner = parents[owner]
                        phase = (relative, owner.name)
                        metadata_phases[phase] = metadata_phases.get(phase, 0) + 1
        self.assertEqual(metadata_phases, {
            ("cli/commands/models.py", "_run_smoke"): 1,
            ("cli/commands/models.py", "_models_qualify"): 1,
            ("cli/commands/models.py", "_models_admit"): 1,
            ("cli/commands/models.py", "_models_revoke"): 2,
        })
        # Dynamic: each served verb plans before its first write and commits
        # with that plan.
        real_preflight = providers_cmd.served_preflight
        real_write = providers_cmd.operator_write
        calls: list = []

        def preflight(runtime, verb, **kwargs):
            calls.append(("preflight", verb, self.state_bytes()))
            return real_preflight(runtime, verb, **kwargs)

        def operator_write(runtime, *, preflight):
            calls.append(("write", preflight.verb if preflight is not None else None))
            return real_write(runtime, preflight=preflight)

        verbs = [
            (["providers", "apply"], ""), ([*test_cli.ACME_ADD, "--declare-only"], ""),
            (["providers", "approve", "acme"], "y\n"), (test_cli.SMALL_ADD, ""), (LAN_ADD, ""),
            (LAN_MODEL, ""), (["models", "rm", "custom-lan-model", "--yes"], ""),
            (["providers", "rm", "lan"], "y\n"),
        ]
        with mock.patch.object(providers_cmd, "served_preflight", side_effect=preflight), \
                mock.patch.object(providers_cmd, "operator_write", side_effect=operator_write):
            for argv, text in verbs:
                calls.clear()
                before = self.state_bytes()
                with self.subTest(argv=argv):
                    code, out, err = self.op(argv, text)
                    self.assertEqual(code, 0, out + err)
                    first = calls[0]
                    self.assertEqual(first[0], "preflight")
                    self.assertEqual(first[2], before)  # planned before any write
                    self.assertIn(("write", first[1]), calls)
                    self.assertIn(served_plan.HEADER, err)

    def test_admit_and_revoke_keep_served_plan_and_sentinel_unchanged(self) -> None:
        self.declare_small()
        before_plan, before_identity = plan_cmd.build(self.runtime)
        before_config = self.config().read_bytes()
        before_sentinel = render.document_sentinel(served_plan.parse_restricted_yaml(before_config.decode()))
        before_candidate = proxy.candidate_document(
            self.runtime.home, environ=self.runtime.gateway_environ(), asset_root=self.runtime.asset_root,
            state_root=self.runtime.session_store.root,
        )
        self.assertFalse(before_plan.changed)
        real_write = providers_cmd.operator_write
        real_confirm = consent_mod.confirm
        phases = []

        @contextlib.contextmanager
        def operator_write(runtime, *, preflight):
            self.assertIsNone(preflight)
            with real_write(runtime, preflight=preflight) as barrier:
                phases.append(("commit", barrier_free(runtime.home)))
                yield barrier

        def confirm(text, **kwargs):
            phases.append(("consent", barrier_free(self.runtime.home)))
            return real_confirm(text, **kwargs)

        with mock.patch.object(providers_cmd, "served_preflight", side_effect=AssertionError("badge planned render")), \
                mock.patch.object(providers_cmd, "operator_write", side_effect=operator_write), \
                mock.patch.object(consent_mod, "confirm", side_effect=confirm), \
                mock.patch.object(models_cmd, "render_identity", side_effect=AssertionError("badge observed gateway")), \
                mock.patch.object(self.runtime, "render_gateway", side_effect=AssertionError("badge rendered")), \
                mock.patch.object(self.runtime, "verify_reload", side_effect=AssertionError("badge reloaded")), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("badge smoked")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("badge qualified")):
            for verb in ("admit", "revoke"):
                with self.subTest(verb=verb):
                    code, out, err = self.op(["models", verb, "custom-acme-small"], "y\n")
                    self.assertEqual(code, 0, out + err)
                    self.assertNotIn(served_plan.HEADER, err)
                    self.assertIn("optional", err)
                    self.assertEqual("custom-acme-small" in self.runtime.current_effective().admitted_lines,
                                     verb == "admit")
                    after_plan, after_identity = plan_cmd.build(self.runtime)
                    self.assertEqual(after_plan, before_plan)
                    self.assertEqual(after_identity, before_identity)
                    self.assertEqual(self.config().read_bytes(), before_config)
                    candidate = proxy.candidate_document(
                        self.runtime.home, environ=self.runtime.gateway_environ(), asset_root=self.runtime.asset_root,
                        state_root=self.runtime.session_store.root,
                    )
                    # The planning render uses placeholder keys, so its
                    # sentinel is compared with its own pre-badge value.
                    self.assertEqual(candidate, before_candidate)
                    self.assertEqual(render.document_sentinel(candidate), render.document_sentinel(before_candidate))
                    published = served_plan.parse_restricted_yaml(self.config().read_text())
                    self.assertEqual(render.document_sentinel(published), before_sentinel)
        self.assertEqual(phases, [("consent", True), ("commit", False),
                                  ("consent", True), ("commit", False)])
        self.assertTrue(barrier_free(self.runtime.home))
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_qualify_smoke_holds_no_barrier_across_consent_or_call(self) -> None:
        self.op(test_cli.ACME_ADD, "y\n")
        self.op(test_cli.SMALL_ADD)
        self.serve_current()
        home = self.runtime.home
        observed: list = []
        real_acquire = state.acquire_served_barrier
        acquisitions: list = []

        def acquire(*args, **kwargs):
            acquisitions.append(len(observed))
            return real_acquire(*args, **kwargs)

        class Answers(io.StringIO):
            def confirm(self, text):
                observed.append(("consent", barrier_free(home)))
                return True

        self.during_smoke = lambda: observed.append(("call", barrier_free(home)))
        err = io.StringIO()
        with mock.patch.object(state, "acquire_served_barrier", side_effect=acquire), \
                mock.patch.object(consent_mod, "stdio_ttys", return_value=True), contextlib.redirect_stderr(err):
            code = cli.main(["models", "qualify", "custom-acme-small", "--smoke"], runtime=self.runtime,
                            input_stream=Answers(), output_stream=io.StringIO(), interactive=True)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(observed, [("consent", True), ("call", True)])
        self.assertEqual(self.calls, [])
        self.assertEqual([label for label, _body in self.http_calls], ["smoke"])
        # The evidence-only commit follows the consented call; qualification
        # never adds an admission phase or holds a barrier across the request.
        self.assertEqual(acquisitions, [2])
        self.assertNotIn("custom-acme-small", self.runtime.current_effective().admitted_lines)
        self.assertTrue(barrier_free(home))

    def test_served_mutation_refuses_during_rotation(self) -> None:
        self.published()
        before = self.state_bytes()
        lock = state.FileLock(proxy.config_dir(self.runtime.home) / "token-rotation")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            code, out, err = self.op(LAN_ADD)
        finally:
            lock.release()
        self.assertEqual(code, 1)
        self.assertIn(providers_cmd.SERVED_BUSY, err)
        self.assertIn("another gateway operation is in progress", err)
        self.assertEqual(self.state_bytes(), before)

    def test_cross_root_mutation_refuses(self) -> None:
        self.published()
        other = str(self.root / "other-state")
        document = continuity.read(self.runtime.home)
        document["state_root"] = other
        continuity.write(self.runtime.home, document)
        before = self.state_bytes()
        code, out, err = self.op(LAN_ADD)
        self.assertEqual(code, 1)
        self.assertIn(served_plan.ROOT_REFUSAL.format(managed=other, requested=self.runtime.session_store.root),
                      err)
        self.assertEqual(self.state_bytes(), before)
        problems, _info, _attention = doctor_mod._doctor_served_checks(self.runtime, self.runtime.gateway_token())
        self.assertIn(served_plan.ROOT_BLOCK.format(managed=other, requested=self.runtime.session_store.root),
                      problems)

    def test_unreadable_root_authority_refuses_every_boundary(self) -> None:
        """An existing but unreadable continuity.json is
        unknown authority, never "no authority yet" — served mutations, plan
        application, new references and the alias prune refuse."""

        self.published()
        path = continuity.path(self.runtime.home)
        state.atomic_write(path, b"{")
        before = self.state_bytes()
        refusal = self.runtime.root_authority_refusal()
        self.assertIsNotNone(refusal)
        self.assertIn("state-root authority is unknown", refusal)
        self.assertEqual(proxy.managed_state_root(self.runtime.home), None)  # display fallback only
        self.assertEqual(proxy.root_authority(self.runtime.home)[0], None)
        self.assertIsNotNone(proxy.root_authority(self.runtime.home)[1])
        code, _out, err = self.op(LAN_ADD)
        self.assertEqual(code, 1)
        self.assertIn("gateway continuity set unreadable", err)
        self.assertEqual(self.state_bytes(), before)
        plan, _identity = plan_cmd.build(self.runtime)
        self.assertEqual(plan_cmd.application_refusal(self.runtime, plan), refusal)
        self.assertTrue(any(note.startswith("Root: gateway continuity set unreadable")
                            for note in plan.notes))
        with self.assertRaises(plan_cmd.PlanError):
            plan_cmd.plan_application(self.runtime, plan.digest)
        preview = proxy.prune_preview(self.runtime.home, self.runtime.session_store.root, None,
                                      environ=self.runtime.gateway_environ())
        self.assertEqual(preview.outcome.code, 1)
        self.assertIn("state-root authority is unknown", "\n".join(preview.outcome.lines))
        self.assertEqual(self.state_bytes(), before)

    def test_legacy_registry_change_after_preview_refuses_the_commit(self) -> None:
        """The legacy custom.json registry is a candidate input;
        a wire retarget between the preview and the commit phase refuses."""

        from claude_multi import custom

        env = self.runtime.gateway_environ()
        custom.add_provider(env, "legacyp", base_url="https://legacy.example.invalid/v1",
                            auth_kind="bearer", secret_env="LEGACY_TOKEN")
        custom.add_model(env, "review-model", wire_model="wire-one", provider="legacyp",
                         context_tokens=128_000, created_via="manual")
        self.published()
        preflight = providers_cmd.served_preflight(self.runtime, "fixture verb", show=False)
        self.assertFalse(preflight.plan.destructive)
        custom.add_model(env, "review-model", wire_model="wire-two", provider="legacyp",
                         context_tokens=128_000, created_via="manual")
        entered = []
        with self.assertRaises(providers_cmd.OperatorCommandError) as raised:
            with providers_cmd.operator_write(self.runtime, preflight=preflight):
                entered.append(True)
        self.assertEqual(entered, [])
        self.assertIn(served_plan.CHANGED_REFUSAL, str(raised.exception))
        # Its secret's presence is a sampled dependency too.
        self.assertIn("LEGACY_TOKEN", providers_cmd._secret_names(self.runtime))

    def test_plan_before_side_is_published_render(self) -> None:
        self.published()
        # The published file says k2; the declarations (catalog) say k3. A
        # re-render would hide this; the plan reads what is published.
        config = self.config()
        published = config.read_text().replace('- name: "k3"', '- name: "k2"')
        state.atomic_write(config, published.encode())
        code, out = self.run_cli(["plan"], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertIn("Retargeted\n  claude-multi-kimi-k3: key route https://api.kimi.com/coding (x-api-key) · k2",
                      out)
        self.assertIn("→ key route https://api.kimi.com/coding (x-api-key) · k3", out)
        self.assertEqual(config.read_text(), published)  # nothing written
        document = json.loads(self.run_cli(["plan", "--json"], interactive=False)[1])
        self.assertEqual(document["kind"], "served-change-plan")
        self.assertEqual(document["retargeted"][0]["before"]["wire"], "k2")
        self.assertIsNone(document["published_unknown"])

    def test_doctor_reports_pending_served_drift(self) -> None:
        self.published()
        self.assertIsNone(doctor_mod._pending_served_change(self.runtime))
        # An out-of-band declaration (a hand or configuration-manager edit).
        directory = state.ensure_private_dir(claude_multi.operator.providers_dir(self.env))
        state.atomic_write(directory / "lan.json", claude_multi.operator.document_bytes({
            "version": 1,
            "provider": {"display": "LAN", "kind": "openai-compatible-lan", "base_url": "http://box.lan:8000/v1",
                         "auth": {"kind": "none"}, "independence_family": "local"},
            "lines": {LAN_ALIAS: {"wire_model": "lan-model", "display": "LAN model", "efforts": ["high"],
                                  "default_effort": "high", "context": {"declared_tokens": 32768,
                                                                        "source": "operator"}}},
        }))
        before = self.state_bytes()
        fact = doctor_mod._pending_served_change(self.runtime)
        self.assertEqual(fact, served_plan.PENDING_FACT.format(added=1, retargeted=0, removed=0, impact="none"))
        _problems, _info, attention = doctor_mod._doctor_served_checks(self.runtime, self.runtime.gateway_token())
        self.assertIn(fact, attention)
        self.assertEqual(self.state_bytes(), before)  # read-only

    def _across(self, expected_route: str) -> None:
        plan = doctor_mod.pending_served_plan(self.runtime)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.doctor_fact(), served_plan.PENDING_FACT.format(
            added=0, retargeted=len(plan.retargeted), removed=0, impact="1 live session(s)"))
        code, out = self.run_cli(["plan"], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertIn(expected_route, out)
        return out

    def test_base_url_path_change_across_plan_doctor_live_impact(self) -> None:
        self.lan_line()
        self.record(LIVE, LAN_ALIAS)
        self.assertIsNone(doctor_mod.pending_served_plan(self.runtime).doctor_fact())
        self.hand_edit("lan", lambda d: d["provider"].__setitem__("base_url", "http://box.lan:8000/v2"))
        out = self._across(f"  {LAN_ALIAS}: lan http://box.lan:8000/v1 · lan-model → lan http://box.lan:8000/v2")
        self.assertIn(f"Live session impact\n  {LIVE[:8]} lead: {LAN_ALIAS} retargets", out)
        # The verb that publishes it shows the same plan before it writes.
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{LIVE[:8]} lead: {LAN_ALIAS} retargets", err)
        self.assertIsNone(doctor_mod.pending_served_plan(self.runtime).doctor_fact())

    def test_oauth_to_key_change_across_plan_doctor_live_impact(self) -> None:
        self.published()
        self.record(LIVE, "claude-opus-5[1m]")
        key = self.root / "platform.key"
        key.write_text("platform-dummy-key\n")
        os.chmod(key, 0o600)
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file", str(key)],
                                  "n\n")
        self.assertEqual(code, 3)
        preview = err.split(served_plan.HEADER)[-1]
        self.assertIn("  claude-opus-5: claude OAuth pool · claude-opus-5 → key route https://api.anthropic.com",
                      preview)
        self.assertIn(f"{LIVE[:8]} lead: claude-opus-5 retargets", preview)
        # Recorded but not yet published (the state between a ledger write
        # and the next render): doctor and plan see the same retarget.
        claude_multi.secret_store.default_store(self.runtime.gateway_environ()).set(
            "PLATFORM_ANTHROPIC_API_KEY", "platform-dummy-key")
        alternative = claude_multi.operator.transport_alternative("anthropic", "api-key")
        schemas = claude_multi.operator.load_schemas(test_cli.CATALOG_ROOT)

        def select(document):
            document["transport_choices"]["anthropic"] = "api-key"
            document["routes"]["anthropic"] = claude_multi.operator.transport_route_record(
                alternative, at=claude_multi.operator.utc_stamp())

        claude_multi.operator.update_ledger(self.env, schemas, select)
        out = self._across("  claude-opus-5: claude OAuth pool · claude-opus-5 → key route https://api.anthropic.com")
        self.assertIn(f"{LIVE[:8]} lead: claude-opus-5 retargets", out)

    def retarget_lan(self) -> None:
        """A published LAN line, then a hand edit of its base-url path: the
        next ``providers apply`` retargets the alias (a destructive plan)."""

        self.lan_line()
        self.hand_edit("lan", lambda d: d["provider"].__setitem__("base_url", "http://box.lan:8000/v2"))

    def test_new_session_reference_invalidates_confirmed_plan(self) -> None:
        self.retarget_lan()
        before = self.state_bytes()
        real = providers_cmd.served_preflight
        new_record = self.runtime.session_store.root / "sessions" / f"{LIVE}.json"

        def preflight_then_new_session(runtime, verb, **kwargs):
            planned = real(runtime, verb, **kwargs)
            self.assertTrue(planned.plan.destructive)
            # A new session starts on the alias after the plan was shown.
            self.record(LIVE, LAN_ALIAS)
            return planned

        with mock.patch.object(providers_cmd, "served_preflight", side_effect=preflight_then_new_session):
            code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn(f"providers apply: {served_plan.CHANGED_REFUSAL}", err)
        after = self.state_bytes()
        after.pop(str(new_record))
        self.assertEqual(after, before)  # nothing applied (the config still has v1)
        self.assertIn('"http://box.lan:8000/v1"', self.config().read_text())

    def test_session_started_after_the_plan_scan_invalidates_the_shown_plan(self) -> None:
        # The window between the displayed plan's record scan and the CAS
        # sample: the sample must come from the plan's own inputs.
        self.retarget_lan()
        before = self.state_bytes()
        new_record = self.runtime.session_store.root / "sessions" / f"{LIVE}.json"
        real_build = served_plan.build_plan
        built: list = []

        def build_then_new_session(*args, **kwargs):
            plan = real_build(*args, **kwargs)
            if plan.retargeted and not built:
                built.append(plan)
                self.record(LIVE, LAN_ALIAS)
            return plan

        with mock.patch.object(served_plan, "build_plan", side_effect=build_then_new_session):
            code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0].impact_summary(), "none")  # what the operator saw
        self.assertEqual(code, 1, err)
        self.assertIn(f"providers apply: {served_plan.CHANGED_REFUSAL}", err)
        after = self.state_bytes()
        after.pop(str(new_record))
        self.assertEqual(after, before)
        self.assertIn('"http://box.lan:8000/v1"', self.config().read_text())

    def test_hook_liveness_change_invalidates_destructive_plan(self) -> None:
        self.retarget_lan()
        path = self.record(ENDED, LAN_ALIAS, event="end")
        before = self.state_bytes()
        real = providers_cmd.served_preflight

        def preflight_then_hook(runtime, verb, **kwargs):
            planned = real(runtime, verb, **kwargs)
            self.assertTrue(planned.plan.destructive)
            self.assertEqual(planned.plan.impacts, ())  # the holder is ended
            # A SessionStart resumes the ended session (same references).
            state.atomic_write(path, strict_json.canonical_file_bytes(
                lenient_record(ENDED, LAN_ALIAS, event="resume")))
            return planned

        with mock.patch.object(providers_cmd, "served_preflight", side_effect=preflight_then_hook):
            code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn(served_plan.CHANGED_REFUSAL, err)
        after = self.state_bytes()
        self.assertEqual({k: v for k, v in after.items() if k != str(path)},
                         {k: v for k, v in before.items() if k != str(path)})

    def test_alias_prune_never_publishes_an_unreviewed_retarget(self) -> None:
        self.retarget_lan()  # published /v1, declared /v2 (out of band)
        self.record(LIVE, LAN_ALIAS)
        (self.root / "proc").mkdir(exist_ok=True)
        aliases = sorted(continuity.read(self.runtime.home)["aliases"])
        self.assertTrue(aliases)  # retained continuity aliases nobody references
        before = self.state_bytes()
        code, out = self.run_cli(["doctor", "--prune-aliases", aliases[0]], interactive=False)
        self.assertEqual(code, 1, out)
        self.assertIn(served_plan.HEADER, out)  # the shared preview, before any lock
        self.assertIn(f"  {LAN_ALIAS}: lan http://box.lan:8000/v1 · lan-model → lan http://box.lan:8000/v2", out)
        self.assertIn(f"{LIVE[:8]} lead: {LAN_ALIAS} retargets", out)
        self.assertIn("not part of this prune", out)
        self.assertEqual(self.state_bytes(), before)
        self.assertIn('"http://box.lan:8000/v1"', self.config().read_text())
        # Published through its own preview first, the prune then goes ahead
        # and shows only its own removal.
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        code, out = self.run_cli(["doctor", "--prune-aliases", aliases[0]], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertIn(served_plan.HEADER, out)
        self.assertIn(f"pruned: {aliases[0]} (", out)
        self.assertNotIn("Retargeted", out)
        self.assertNotIn(aliases[0], continuity.read(self.runtime.home)["aliases"])

    def test_alias_prune_revalidates_its_preview_inside_the_phase(self) -> None:
        self.published()
        (self.root / "proc").mkdir(exist_ok=True)
        aliases = sorted(continuity.read(self.runtime.home)["aliases"])
        real = proxy.prune_preview

        def preview_then_edit(*args, **kwargs):
            shown = real(*args, **kwargs)
            # An out-of-band declaration (a hand or configuration-manager edit) lands
            # after the preview was shown: the commit would publish it.
            directory = state.ensure_private_dir(claude_multi.operator.providers_dir(self.env))
            state.atomic_write(directory / "lan.json", claude_multi.operator.document_bytes({
                "version": 1,
                "provider": {"display": "LAN", "kind": "openai-compatible-lan",
                             "base_url": "http://box.lan:8000/v1", "auth": {"kind": "none"},
                             "independence_family": "local"},
                "lines": {LAN_ALIAS: {"wire_model": "lan-model", "display": "LAN model", "efforts": ["high"],
                                      "default_effort": "high",
                                      "context": {"declared_tokens": 32768, "source": "operator"}}},
            }))
            return shown

        before_aliases = continuity.read(self.runtime.home)["aliases"]
        with mock.patch.object(proxy, "prune_preview", side_effect=preview_then_edit), \
                contextlib.redirect_stderr(io.StringIO()):
            code, out = self.run_cli(["doctor", "--prune-aliases", aliases[0]], interactive=False)
        self.assertEqual(code, 1, out)
        self.assertIn(served_plan.CHANGED_REFUSAL, out)
        self.assertEqual(continuity.read(self.runtime.home)["aliases"], before_aliases)
        self.assertNotIn(LAN_ALIAS, self.config().read_text())

    def test_prune_unknown_record_scan_refuses(self) -> None:
        self.retarget_lan()
        sessions_dir = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        state.atomic_write(sessions_dir / f"{LIVE}.json", b"{broken")
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn(f"Live session impact: unknown — unreadable records {LIVE[:8]}.\n"
                      "This operation cannot safely remove or retarget selectors.", err)
        self.assertEqual(self.state_bytes(), before)
        # The alias prune refuses the same unknown coverage.
        (self.root / "proc").mkdir(exist_ok=True)
        code, out = self.run_cli(["doctor", "--prune-aliases"], interactive=False)
        self.assertEqual(code, 1, out)
        self.assertIn(f"cannot prove liveness: unreadable records {LIVE[:8]}", out)


# ------------------------------------------------------------ root adoption
class AdoptRootTests(test_cli.CLITestCase):
    def test_adopt_root_serializes_reference_creators(self) -> None:
        home = self.runtime.home
        with contextlib.redirect_stderr(io.StringIO()):
            code, _out = self.run_cli(["providers", "apply"], interactive=False)
        managed = continuity.read(home)["state_root"]
        self.assertEqual(managed, str(self.runtime.session_store.root))
        new_root = self.root / "adopted-state" / "claude-multi"
        guard = mock.Mock()
        # A reference creator (a launch in its commit phase) holds the barrier:
        # the adoption waits, then refuses, writing nothing.
        before = continuity.path(home).read_bytes()
        with foreign_barrier(home), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(state.BarrierBusyError):
                proxy.adopt_root(home, new_root, environ=self.runtime.environ, input_stream=io.StringIO("y\n"),
                                 guard=guard, barrier_timeout=0.2)
        self.assertEqual(continuity.path(home).read_bytes(), before)
        guard.assert_called_once()
        # Declined: nothing written (the preview and the ask hold no lock).
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(proxy.adopt_root(home, new_root, environ=self.runtime.environ,
                                              input_stream=io.StringIO("n\n"), guard=guard))
        self.assertIn("Root adoption preview — nothing written yet", err.getvalue())
        self.assertEqual(continuity.path(home).read_bytes(), before)
        # Confirmed and uncontended: only the existing field moves.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(proxy.adopt_root(home, new_root, environ=self.runtime.environ,
                                             input_stream=io.StringIO("y\n"), guard=guard))
        after = continuity.read(home)
        self.assertEqual(after["state_root"], str(new_root))
        self.assertEqual({k: v for k, v in after.items() if k != "state_root"},
                         {k: v for k, v in strict_json.loads(before).items() if k != "state_root"})
        # Reference creators on the old root now refuse.
        self.assertIn("This gateway is managed for state root", self.runtime.root_authority_refusal())
        # The guard is the shared human guard by default.
        with mock.patch.object(consent_mod, "stdio_ttys", return_value=False), \
                self.assertRaises(consent_mod.ConsentRefused):
            proxy.adopt_root(home, self.root / "third", environ=self.runtime.environ)


    def test_adopt_root_refuses_a_session_committed_during_confirmation(self) -> None:
        home = self.runtime.home
        with contextlib.redirect_stderr(io.StringIO()):
            code, _out = self.run_cli(["providers", "apply"], interactive=False)
        old_root = self.runtime.session_store.root
        self.assertEqual(continuity.read(home)["state_root"], str(old_root))
        new_root = self.root / "adopted-state" / "claude-multi"
        before = continuity.path(home).read_bytes()
        launched = old_root / "sessions" / f"{LIVE}.json"

        class LaunchWhileAnswering(io.StringIO):
            def readline(self, *args):
                # A launch under the managed root commits while the operator
                # reads a preview that said zero sessions there.
                directory = state.ensure_private_dir(old_root / "sessions")
                state.atomic_write(directory / f"{LIVE}.json", strict_json.canonical_file_bytes(
                    lenient_record(LIVE, "claude-opus-5[1m]", event="startup")))
                return "y\n"

        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(proxy.ProxyError) as raised:
            proxy.adopt_root(home, new_root, environ=self.runtime.environ,
                             input_stream=LaunchWhileAnswering(), guard=mock.Mock())
        self.assertIn("0 under the managed root", err.getvalue())
        self.assertEqual(str(raised.exception), proxy.ADOPT_ROOT_REFERENCES_CHANGED)
        self.assertTrue(launched.exists())
        self.assertEqual(continuity.path(home).read_bytes(), before)  # authority stays with the old root
        self.assertTrue(barrier_free(home))


# ------------------------------------------------------------ plan and update
class PlanCommandTests(test_cli.CLITestCase):
    def test_plan_via_main_with_foreign_hook_command_leaves_shims_byte_identical(self) -> None:
        env = dict(self.runtime.environ)
        env.update({"CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT),
                    "CLAUDE_MULTI_HOOK_COMMAND": "/nix/store/aaaa-claude-multi-3.0.2/bin/claude-multi"})
        with mock.patch.dict(os.environ, env, clear=True):
            runtime = runtime_mod.Runtime(asset_root=FIXTURE_ROOT, environ=dict(env), cwd=self.root / "project",
                                          **self._runtime_seams())
            self.assertTrue(runtime._shims_refreshed)
        shims = {path: path.read_bytes() for path in (runtime.session_store.root / "bin").iterdir()}
        self.assertTrue(shims)
        env["CLAUDE_MULTI_HOOK_COMMAND"] = "/nix/store/bbbb-claude-multi-3.1.0/bin/claude-multi"
        state_tree = {path: path.read_bytes() for path in sorted(Path(env["XDG_STATE_HOME"]).rglob("*"))
                      if path.is_file()}
        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            code = cli.main(["plan"], output_stream=output, interactive=False)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(served_plan.HEADER, output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in shims}, shims)
        self.assertEqual({path: path.read_bytes() for path in sorted(Path(env["XDG_STATE_HOME"]).rglob("*"))
                          if path.is_file()}, state_tree)

    def test_plan_assets_preserve_running_identity(self) -> None:
        # A package output: its launcher tree carries the resources.
        candidate = self.root / "candidate" / INSTALLED_RESOURCES_RELATIVE
        shutil.copytree(FIXTURE_ROOT, candidate)
        version = strict_json.loads((candidate / "version.json").read_bytes())
        version["launcher_version"] = "9.9.9"
        (candidate / "version.json").write_text(json.dumps(version))
        before_root, before_env = self.runtime.asset_root, dict(self.runtime.environ)
        code, out = self.run_cli(["plan", "--assets", str(self.root / "candidate"), "--json"], interactive=False)
        self.assertEqual(code, 0, out)
        document = json.loads(out)
        self.assertEqual(document["candidate"]["launcher_version"], "9.9.9")
        self.assertEqual(document["candidate"]["asset_root"], str(candidate))
        self.assertEqual(document["running_launcher"], claude_multi.__version__)
        self.assertEqual(self.runtime.asset_root, before_root)
        self.assertEqual(self.runtime.environ, before_env)
        self.assertNotIn("CLAUDE_MULTI_ASSETS", os.environ)
        code, text = self.run_cli(["plan", "--assets", str(self.root / "candidate")], interactive=False)
        self.assertIn(f"Running launcher {claude_multi.__version__} (unchanged by --assets)", text)
        self.assertIn("(launcher 9.9.9,", text)
        digest = document["digest"]
        self.assertEqual(json.loads(self.run_cli(["plan", "--assets", str(self.root / "candidate"), "--json"],
                                                 interactive=False)[1])["digest"], digest)
        self.assertNotEqual(json.loads(self.run_cli(["plan", "--json"], interactive=False)[1])["digest"], digest)
        # The installer seam refuses a plan that moved.
        plan_cmd.plan_application(self.runtime, digest, self.root / "candidate")
        with self.assertRaises(plan_cmd.PlanError):
            plan_cmd.plan_application(self.runtime, "0" * 64, self.root / "candidate")
        code, out = self.run_cli_err(["plan", "--assets", str(self.root / "nothing")], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("not a claude-multi package or asset root", out)


class PlanApplicationBoundaryTests(test_cli.CLITestCase):
    """The installer-facing application boundary refuses a
    plan it may not apply (another root holds the gateway's authority, or a
    removal has unknown live impact) and one that moved since it was shown."""

    def setUp(self) -> None:
        super().setUp()
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["providers", "apply"], interactive=False)  # published (no gateway to reload)
        self.assertEqual(continuity.read(self.runtime.home)["state_root"], str(self.runtime.session_store.root))
        self.assets = self.root / "candidate" / "resources"
        shutil.copytree(FIXTURE_ROOT, self.assets)

    def live_record(self, stem: str = LIVE, selector: str = "claude-opus-5[1m]", *, raw: bytes | None = None):
        directory = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        state.atomic_write(directory / f"{stem}.json", raw if raw is not None else
                           strict_json.canonical_file_bytes(lenient_record(stem, selector, event="startup")))

    def test_a_plan_that_moved_is_refused(self) -> None:
        digest = plan_cmd.build(self.runtime, self.assets)[0].digest
        self.assertEqual(plan_cmd.plan_application(self.runtime, digest, self.assets).digest, digest)
        self.live_record()
        with self.assertRaises(plan_cmd.PlanError) as raised:
            plan_cmd.plan_application(self.runtime, digest, self.assets)
        self.assertEqual(str(raised.exception), served_plan.CHANGED_REFUSAL)

    def test_an_inhibition_refuses_the_application_boundary(self) -> None:
        """An inhibition another owner holds refuses the plan application
        boundary; the owner's own run (its token in the environment)
        applies."""

        from claude_multi import gateway_inhibition

        token = gateway_inhibition.begin(
            self.runtime.session_store.root, owner="installer", purpose="update 1.0.0 to 1.0.1",
            phase="replace", remedy="sh install.sh --recover")
        digest = plan_cmd.build(self.runtime, self.assets)[0].digest
        refusal = plan_cmd.application_refusal(self.runtime, plan_cmd.build(self.runtime, self.assets)[0])
        self.assertIn("the gateway is inhibited by installer (update 1.0.0 to 1.0.1; phase replace", refusal)
        self.assertIn("sh install.sh --recover", refusal)
        with self.assertRaises(plan_cmd.PlanError) as raised:
            plan_cmd.plan_application(self.runtime, digest, self.assets)
        self.assertIn("inhibited by installer", str(raised.exception))
        self.runtime.environ[gateway_inhibition.TOKEN_ENV] = token
        self.assertEqual(plan_cmd.plan_application(self.runtime, digest, self.assets).digest, digest)

    def test_application_boundary_refuses_root_mismatch_and_unknown_coverage(self) -> None:
        digest = plan_cmd.build(self.runtime, self.assets)[0].digest
        self.assertEqual(plan_cmd.plan_application(self.runtime, digest, self.assets).digest, digest)
        # A destructive plan (the published render is retargeted by the
        # candidate) with an unreadable record: unknown live impact.
        config = proxy.config_dir(self.runtime.home) / "config.yaml"
        published = config.read_text()
        state.atomic_write(config, published.replace('- name: "k3"', '- name: "k2"').encode())
        self.live_record(raw=b"{broken")
        refused, _identity = plan_cmd.build(self.runtime, self.assets)
        self.assertTrue(refused.destructive)
        self.assertIsNotNone(refused.refusal())
        with self.assertRaises(plan_cmd.PlanError) as raised:
            plan_cmd.plan_application(self.runtime, refused.digest, self.assets)
        self.assertEqual(str(raised.exception), refused.refusal())
        # Another root holds the gateway's authority.
        state.atomic_write(config, published.encode())
        (self.runtime.session_store.root / "sessions" / f"{LIVE}.json").unlink()
        document = continuity.read(self.runtime.home)
        other = str(self.root / "other-state")
        document["state_root"] = other
        continuity.write(self.runtime.home, document)
        moved = plan_cmd.build(self.runtime, self.assets)[0]
        with self.assertRaises(plan_cmd.PlanError) as raised:
            plan_cmd.plan_application(self.runtime, moved.digest, self.assets)
        self.assertEqual(str(raised.exception), self.runtime.root_authority_refusal())


class AbsentAuthorityTests(unittest.TestCase):
    """An authority key nothing records reads as its default: the ledger
    names only a non-default transport, so absence is the account sign-in."""

    def test_transport_before_and_after_values_are_named(self) -> None:
        refs = served_plan.References({}, frozenset())
        switch = served_plan.build_plan({}, {}, references=refs, state_root="/s", gateway="127.0.0.1:1",
                                        authority_before={}, authority_after={"transport anthropic": "api-key"})
        self.assertEqual(switch.authority, (("transport anthropic", "oauth-pool", "api-key"),))
        back = served_plan.build_plan({}, {}, references=refs, state_root="/s", gateway="127.0.0.1:1",
                                      authority_before={"transport anthropic": "api-key"}, authority_after={})
        self.assertEqual(back.authority, (("transport anthropic", "api-key", "oauth-pool"),))
        same = served_plan.build_plan({}, {}, references=refs, state_root="/s", gateway="127.0.0.1:1",
                                      authority_before={}, authority_after={"transport anthropic": "oauth-pool"})
        self.assertEqual(same.authority, ())
        route = served_plan.build_plan({}, {}, references=refs, state_root="/s", gateway="127.0.0.1:1",
                                       authority_before={}, authority_after={"route acme": "approved"})
        self.assertEqual(route.authority, (("route acme", "—", "approved"),))


if __name__ == "__main__":
    unittest.main()
