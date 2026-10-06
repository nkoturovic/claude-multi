"""``claude-multi lineup`` — parser, resolution, classify, live apply.

Every session here is really launched through ``Runtime.prepare`` ->
``perform_launch`` (``_v4.V4Case``: temp HOME/XDG, fake verified binary,
``execve`` seam), so the scopes, ``lead-set.json`` and ``launch_fence`` a
request classifies against are the real compiled ones. Fixture catalog only;
no shipped model id is pinned. Crash injection lives in
``test_lineup_crash.py``, threads in ``test_lineup_races.py`` and
propagation in ``test_propagation.py``.
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import io
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import cli, hooks, lineup, lineup_files, lineup_log, migrate, profile, scope, sessions, state, strict_json, transition
from claude_multi import quota as quota_mod
from claude_multi import catalog
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden
from _v4 import V4Case

FIXED = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
NOW = "2026-09-25T00:00:00Z"
LINEUP_GOLDENS = GOLDENS_ROOT / "v4" / "lineup"


def _fixed_id(value: str = FIXED):
    return mock.patch.object(sessions.SessionStore, "new_id", lambda self: value)


def _fixed_clock():
    return mock.patch.object(sessions, "_now", lambda: NOW)


class QuotaEntryInitializationTests(unittest.TestCase):
    def test_cm_quota_constructs_read_only_runtime_from_empty_home(self) -> None:
        """The real CLI entry must not initialize writable state for /cm quota."""
        import tempfile

        from claude_multi import assets, catalog, scope
        import claude_multi.cli.runtime as runtime_mod

        with tempfile.TemporaryDirectory(prefix="cm-quota-entry-") as temporary:
            home = Path(temporary)
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                   "XDG_STATE_HOME": str(home / "state"), "TERM": "dumb"}
            state_root = home / "state" / "claude-multi"
            state_root.mkdir(parents=True)
            # The real shim locations, so a writable Runtime would rewrite them.
            shims = [scope.hook_shim_path(state_root), scope.hook_shim_v3_path(state_root),
                     scope.gateway_token_shim_path(state_root)]
            for shim in shims:
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_bytes(b"existing shim\n")
            # 0600, so a writable Runtime's legacy-override removal would act on it.
            override = home / "config" / "claude-multi" / "native-contract.json"
            override.parent.mkdir(parents=True)
            override.write_bytes(b'{"effort_vocabulary": [], "version": 1}\n')
            override.chmod(0o600)
            before = {str(p.relative_to(home)): (p.stat().st_mtime_ns,
                      p.read_bytes() if p.is_file() else None) for p in home.rglob("*")}
            constructed = []
            original_runtime = runtime_mod.Runtime

            def factory(**kwargs):
                self.assertFalse(kwargs["allow_state_writes"])
                self.assertFalse(kwargs["refresh_shims"])
                self.assertFalse(kwargs["initialize_session_store"])
                result = original_runtime(**kwargs)
                constructed.append(result)
                return result

            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                    mock.patch.object(runtime_mod, "Runtime", side_effect=factory), \
                    mock.patch.object(original_runtime, "pool_status", return_value=quota_mod.PoolStatus("down")), \
                    mock.patch.object(catalog, "remove_legacy_contract_override",
                                      side_effect=AssertionError("override removed")), \
                    mock.patch.object(scope, "ensure_hook_shim", side_effect=AssertionError("shim refreshed")), \
                    mock.patch.object(scope, "ensure_hook_shim_v3", side_effect=AssertionError("shim3 refreshed")), \
                    mock.patch.object(scope, "ensure_token_helper_command", side_effect=AssertionError("token shim refreshed")), \
                    mock.patch.object(sessions, "SessionStore", side_effect=AssertionError("session store created")):
                output = io.StringIO()
                code = cli.main(["lineup", "--session", FIXED, "quota"],
                                runtime=None, input_stream=io.StringIO(),
                                output_stream=output, interactive=False)
            self.assertEqual(code, 0)
            # Skill mode returns 0 even on a swallowed error: pin the exact output.
            self.assertEqual(output.getvalue(), "quota: the local gateway is down\nQuota condition: unavailable — no reading can be made here now\n")
            self.assertEqual(len(constructed), 1)
            self.assertEqual({str(p.relative_to(home)): (p.stat().st_mtime_ns,
                              p.read_bytes() if p.is_file() else None) for p in home.rglob("*")}, before)
            self.assertTrue(override.exists())
            self.assertFalse((state_root / "sessions").exists())
            self.assertTrue(all(shim.read_bytes() == b"existing shim\n" for shim in shims))


# ------------------------------------------------------------------ parser


class ParseCliTests(unittest.TestCase):
    """Argument rules 1-4 (pure)."""

    ENV = {"CLAUDE_CODE_SESSION_ID": OTHER}

    def parse(self, *argv: str, env=None):
        return lineup.parse_cli(list(argv), {} if env is None else env)

    def refusal(self, *argv: str, env=None) -> str:
        with self.assertRaises(lineup.LineupRefusal) as raised:
            self.parse(*argv, env=env)
        return str(raised.exception)

    def test_the_skill_form_takes_exactly_one_argument(self) -> None:
        args = self.parse("--session", FIXED, "set implementer=sol:high")
        self.assertTrue(args.skill_mode)
        self.assertEqual(args.session, FIXED)
        self.assertEqual(
            args.request, lineup.Request("set", agent="cm-implementer", model="sol", effort="high",
                                         text="set implementer=sol:high"),
        )
        self.assertEqual(self.parse("--session", FIXED, "").request.verb, "show")
        # a balanced apostrophe splits the argument: refused, never guessed
        self.assertEqual(
            self.refusal("--session", FIXED, "set implementer=", "sol high"),
            lineup.R4.format(n=2),
        )
        self.assertEqual(self.refusal("--session", FIXED, "--"), lineup.R4.format(n=0))
        self.assertEqual(self.refusal("--session", FIXED, "a", ""), lineup.R4.format(n=2))

    def test_leading_bang_and_unbalanced_quotes_are_refused(self) -> None:
        self.assertEqual(self.refusal("--session", FIXED, "\\!profile claude"), lineup.R5)
        self.assertEqual(self.refusal("--session", FIXED, "!profile claude"), lineup.R5)
        self.assertIn("/cm could not split 'set \"a'", self.refusal("--session", FIXED, 'set "a'))

    def test_an_empty_session_expansion_falls_back_to_the_environment(self) -> None:
        # rule 2a: `--session ''` alone is `show` on $CLAUDE_CODE_SESSION_ID
        args = self.parse("--session", "", env=self.ENV)
        self.assertEqual((args.session, args.request.verb), (OTHER, "show"))
        self.assertEqual(self.refusal("--session", ""), lineup.R2)
        # rule 2a: the shell dropped the empty id, the request moved into its place
        args = self.parse("--session", "set implementer=sol", env=self.ENV)
        self.assertEqual((args.session, args.request.verb, args.request.model), (OTHER, "set", "sol"))
        # rule 2b: an explicit empty value, then the request
        args = self.parse("--session", "", "set implementer=sol", env=self.ENV)
        self.assertEqual((args.session, args.request.verb), (OTHER, "set"))
        self.assertEqual(
            self.refusal("--session", "not-a-uuid", "set a=b"), lineup.R3.format(value="not-a-uuid")
        )
        # rule 2d: a bare trailing --session
        self.assertEqual(self.parse("--session", env=self.ENV).session, OTHER)

    def test_equals_form_options_and_the_cli_form(self) -> None:
        self.assertEqual(self.parse(f"--session={FIXED}").request.verb, "show")
        self.assertEqual(self.refusal("--session=nope"), lineup.R3.format(value="nope"))
        self.assertEqual(self.refusal("--bogus", "show"), lineup.R1.format(opt="--bogus"))
        args = self.parse("--session", FIXED, "--relaunch", "--its-exited", "profile", "quality")
        self.assertTrue(args.relaunch and args.its_exited)
        self.assertEqual(args.request, lineup.Request("profile", name="quality", text="profile quality"))
        # outside skill mode: the words as given, the session from the environment
        args = self.parse("--", "set", "implementer=sol", env=self.ENV)
        self.assertFalse(args.skill_mode)
        self.assertEqual((args.session, args.request.verb), (OTHER, "set"))
        self.assertEqual(self.refusal("show"), lineup.R2)

    def test_request_grammar(self) -> None:
        parse = lineup.parse_request
        self.assertEqual(parse(["unset", "cm-reviewer"]).agent, "cm-reviewer")
        self.assertEqual(parse(["unset", "analyst-strong"]).agent, "cm-analyst-strong")
        self.assertEqual(parse(["direct"]), lineup.Request("direct", text="direct"))
        self.assertEqual(parse(["direct", "sol:high"]).effort, "high")
        self.assertEqual(parse(["direct", "opus55:ultracode"]).effort, "ultracode")
        self.assertEqual(parse(["pin"]).verb, "pin")
        self.assertEqual(parse(["follow"]).verb, "follow")
        for words in (
            ["set", "implementer=sol:ultracode"], ["set", "nobody=sol"], ["set", "a"],
            ["unset"], ["profile"], ["profile", "Bad Name"], ["pin", "x"], ["frobnicate"],
            ["direct", "sol:turbo"], ["set", "implementer=SOL"],
        ):
            with self.subTest(words=words), self.assertRaises(lineup.LineupRefusal) as raised:
                parse(words)
            self.assertEqual(str(raised.exception), lineup.R7.format(text=" ".join(words)))


# ------------------------------------------------------------ shared case


class LineupCase(V4Case):
    """A session really launched from fixture ``balanced`` (gen 1, following)."""

    launch_profile = "balanced"

    def setUp(self) -> None:
        super().setUp()
        if self.launch_profile is not None:
            with _fixed_id():
                self.record = self.launch_fresh(self.profile_target(self.launch_profile))
            self.mid = self.record["managed_id"]
            self.rid = self.record["runtime_session_id"]

    def lineup(self, *argv: str, skill: bool = True, interactive=None, text: str | None = None):
        """``cli.main(["lineup", …])`` -> (exit, stdout, stderr)."""

        head = ["--session", self.rid] if skill else []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(
                ["lineup", *head, *argv],
                runtime=self.runtime,
                output_stream=out,
                input_stream=None if text is None else io.StringIO(text),
                interactive=interactive,
            )
        return code, out.getvalue(), err.getvalue()

    def request(self, text: str, **kwargs):
        return self.lineup(text, **kwargs)

    def save_profile(self, name: str, mutate) -> None:
        document = self.runtime.profiles.load("balanced")
        document.pop("seed", None)
        document["name"] = name
        mutate(document)
        self.runtime.profiles.save(document, target=name)

    def scope_files(self) -> dict[str, bytes]:
        return self.tree(self.live(self.mid))

    def refence(self) -> None:
        """Rewrite the record's ``launch_fence`` to the current disk digest (a test-only write)."""

        live = self.live(self.mid)
        digest = sessions.launch_digest(
            strict_json.loads((live / "settings.json").read_bytes()),
            (live / "lead-set.json").read_bytes(),
        )
        self.mutate(self.mid, launch_fence=digest)

    def converge_report(self) -> list[str]:
        return transition.converge(
            self.store.root, self.store, self.mid, runtime_parts=self.runtime.converge_parts()
        )


# ------------------------------------------------------------ goldens


def _lead_profile(case: "LineupCase", name: str, lead: dict, *, agents: dict | None = None) -> None:
    """A user profile copied from ``balanced`` with another lead (lineup goldens)."""

    def mutate(document: dict) -> None:
        document["lead"] = lead
        document["description"] = f"Golden profile {name}"
        if agents is not None:
            document["agents"].update(agents)

    case.save_profile(name, mutate)


def _user_copy(case: "LineupCase", name: str, **changes) -> None:
    """A user file overriding seed ``name`` with ``changes`` (the seed marker dropped)."""

    document = case.runtime.profiles.load(name)
    document.pop("seed", None)
    document.update(changes)
    case.runtime.profiles.save(document, target=name)


def lineup_golden_files() -> dict[str, bytes]:
    """Exact stdout of ``cli.main(["lineup", "--session", …])`` (fixed id and clock).

    It covers ``/cm profiles``, ``/cm profile X`` for agents only,
    a lead inside the lead set (alone, and with agent changes) and a lead
    outside it, ``/cm review`` and ``/cm fallback``. A setup callable
    prepares fixture-local profiles first.
    """

    requests = {
        "show.txt": ("", None),
        "set-live.txt": ("set implementer=opus55:xhigh", None),
        "unset-live.txt": ("unset implementer-strong", None),
        "profile-live.txt": ("profile quality", None),
        "relaunch-pending.txt": ("profile direct", None),
        "direct-relaunch.txt": ("direct sol", None),
        "profiles.txt": ("profiles", None),
        "profile-agents-only.txt": ("profile quality", None),
        "profile-lead-in-set.txt": (
            "profile lead-in",
            lambda case: _lead_profile(case, "lead-in", {"model": "opus5", "effort": "xhigh"}),
        ),
        "profile-lead-in-set-agents.txt": (
            "profile lead-in-agents",
            lambda case: _lead_profile(case, "lead-in-agents", {"model": "opus5", "effort": "xhigh"},
                                       agents={"cm-implementer": {"model": "opus55", "effort": "xhigh"}}),
        ),
        "profile-lead-outside-set.txt": (
            "profile lead-out",
            lambda case: _lead_profile(case, "lead-out", {"model": "grok46", "effort": "high"}),
        ),
        "review-normal.txt": ("review", None),
        "review-high-stakes.txt": ("review high-stakes HEAD~3..HEAD", None),
        "fallback-live.txt": ("fallback anthropic", None),
        "fallback-lead-switch.txt": ("fallback openai", None),
        "fallback-pending.txt": (
            "fallback openai",
            lambda case: _user_copy(case, "openai", lead_providers=["openai"]),
        ),
        "fallback-none.txt": ("fallback kimi", None),
        # The quota command's formatter on the session surface
        # (an understood, empty passive read: deterministic, no clock or TZ).
        "quota.txt": ("quota", lambda case: setattr(case.runtime, "pool_status",
                                                    lambda: quota_mod.PoolStatus("ok"))),
    }
    files: dict[str, bytes] = {}
    for name, (text, setup) in requests.items():
        case = _GoldenCase()
        case.setUp()
        try:
            if setup is not None:
                setup(case)
            with _fixed_clock():
                code, out, err = case.request(text)
            assert (code, err) == (0, ""), (name, out, err)
            files[name] = out.encode("utf-8")
        finally:
            case.doCleanups()
    case = _GoldenCase()
    case.setUp()
    try:
        case.runtime.environ["CLAUDE_CODE_SESSION_ID"] = case.rid
        with _fixed_clock():
            code, out, err = case.lineup("--preview", "fallback", "anthropic", skill=False,
                                         interactive=False)
        assert (code, err) == (0, ""), (out, err)
        files["fallback-preview.txt"] = out.encode("utf-8")
    finally:
        case.doCleanups()
    case = _GoldenCase()
    case.setUp()
    try:
        code, out, err = case.lineup("set implementer=", "opus55 high")
        assert (code, err) == (0, ""), (out, err)
        files["refused-two-args.txt"] = out.encode("utf-8")
    finally:
        case.doCleanups()
    return files


class _GoldenCase(LineupCase):
    def runTest(self) -> None:  # pragma: no cover - helper only
        pass


class LineupGoldenTests(unittest.TestCase):
    def test_outputs_match_the_goldens(self) -> None:
        for name, data in lineup_golden_files().items():
            with self.subTest(golden=name):
                assertGolden(self, LINEUP_GOLDENS / name, data)
        live = (LINEUP_GOLDENS / "set-live.txt").read_text()
        self.assertIn(lineup.RELOAD_LINES[0], live)


# -------------------------------------------------------- main intercept


class InterceptTests(LineupCase):
    def test_raw_argv_reaches_parse_cli_with_the_separator(self) -> None:
        seen: list[list[str]] = []
        real = lineup.parse_cli

        def spy(argv, environ):
            seen.append(list(argv))
            return real(argv, environ)

        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch.object(lineup, "parse_cli", spy):
            code, out, err = self.lineup("--", "set implementer=sol:xhigh")
            self.assertNotIn("passthrough arguments are accepted only for launch", out + err)
            code, out, err = self.lineup("--", "set", "implementer=sol:xhigh", skill=False)
            self.assertEqual(code, 0, err)
            self.assertNotIn("passthrough", out + err)
        self.assertEqual(seen[0], ["--session", self.rid, "--", "set implementer=sol:xhigh"])
        self.assertEqual(seen[1], ["--", "set", "implementer=sol:xhigh"])

    def test_skill_mode_refusals_go_to_stdout_with_exit_0_and_no_tty(self) -> None:
        with mock.patch('claude_multi.cli.streams._open_tty_streams', side_effect=AssertionError("tty opened")):
            code, out, err = self.lineup("set implementer=", "sol")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"claude-multi: {lineup.R4.format(n=2)}\n")

    def test_outside_skill_mode_refusals_go_to_stderr_with_exit_2(self) -> None:
        code, out, err = self.lineup("frobnicate", skill=False)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, f"claude-multi: {lineup.R2}\n")
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        code, out, err = self.lineup("frobnicate", skill=False)
        self.assertEqual(code, 2)
        self.assertIn(lineup.R7.format(text="frobnicate"), err)

    def test_migration_busy_is_r19_and_writes_nothing(self) -> None:
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        before = self.record_bytes(self.mid)
        for text in ("set implementer=opus55:xhigh", "pin", "profile quality", "direct"):
            with self.subTest(request=text):
                code, out, _ = self.request(text)
                self.assertEqual((code, out), (0, f"claude-multi: {lineup.R19}\n"))
        self.assertEqual(self.record_bytes(self.mid), before)
        code, out, _ = self.request("")  # show takes no lock
        self.assertTrue(out.startswith("claude-multi · profile balanced (follows)"))


# ------------------------------------------------------------ resolution


class ResolutionTests(LineupCase):
    def test_a_managed_id_that_is_not_the_runtime_id_is_refused_with_the_runtime_id(self) -> None:
        self.mutate(self.mid, runtime_session_id=OTHER)  # a runtime id that never was the managed id
        with mock.patch.object(self, "rid", self.mid):
            code, out, _ = self.request("")
        self.assertIn(f"lineup --session takes the runtime id ({OTHER})", out)
        with mock.patch.object(self, "rid", OTHER):
            code, out, _ = self.request("")
        self.assertTrue(out.startswith("claude-multi · profile balanced"), out)

    def test_a_pending_fork_is_r8(self) -> None:
        record = self.store.load(self.mid)
        self.mutate(
            self.mid,
            pending_forks=[{"session_id": OTHER, "observed_at": NOW}],
            identity_state=sessions.IDENTITY_PENDING_FORK,
        )
        del record
        with mock.patch.object(self, "rid", OTHER):
            code, out, _ = self.request("pin")
        self.assertTrue(out.startswith(f"claude-multi: {OTHER} is an unadopted native fork: "), out)
        self.assertIn("claude-multi sessions link", out)


class V3RecordTests(LineupCase):
    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        from test_launch_v4 import Gen0AndMigrationTests

        Gen0AndMigrationTests._v3_record(self)
        self.mid = self.rid = FIXED

    def test_only_profile_and_direct_model_are_accepted(self) -> None:
        before = self.record_bytes(FIXED)
        for text in ("set implementer=sol", "unset reviewer", "pin", "follow", "direct"):
            with self.subTest(request=text):
                code, out, _ = self.request(text)
                self.assertIn(f"session {FIXED[:8]} is a legacy record (version 3)", out)
        code, out, _ = self.request("")
        self.assertIn("legacy record (version 3)", out)
        self.assertIn(lineup.SHOW_2X_LINE.format(mid=FIXED), out)
        self.assertEqual(self.record_bytes(FIXED), before)

    def test_inside_or_non_interactive_is_r25_and_the_record_stays_v3(self) -> None:
        before = self.record_bytes(FIXED)
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED
        code, out, _ = self.request("profile quality")
        self.assertIn(
            f"session {FIXED[:8]} is a legacy record (version 3) and cannot hold a pending change",
            out,
        )
        del self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"]
        code, out, _ = self.lineup("--relaunch", "profile", "quality")
        self.assertIn("cannot hold a pending change", out)
        self.assertEqual(self.record_bytes(FIXED), before)
        self.assertEqual(self.execs, [])

    def test_relaunch_its_exited_migrates_in_prepare_and_execs(self) -> None:
        with mock.patch.object(lineup, "_evaluate", side_effect=AssertionError("evaluated a v3 view")):
            code, out, err = self.lineup("--relaunch", "--its-exited", "profile", "quality")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.execs), 1)
        record = self.store.load(FIXED)
        self.assertEqual((record["version"], record["profile"], record["follow"]), (4, "quality", True))
        self.assertTrue(self.store.backup_path(FIXED).is_file())


class Gen0Tests(LineupCase):
    """A migrated (gen 0) record: never live, remedies reach the exec seam."""

    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        from test_launch_v4 import Gen0AndMigrationTests

        Gen0AndMigrationTests._v3_record(self)
        migrate.migrate_one(
            self.store, self.runtime.lineup_catalog(), FIXED,
            hook_command=self.runtime.resolved_hook_command,
            hook_shim_path=scope.hook_shim_path(self.store.root),
        )
        self.mid = self.rid = FIXED
        record = self.store.load(FIXED)
        self.assertEqual(record["lineup_generation"], 0)

    def needs_choice(self) -> None:
        record = self.store.load(FIXED)
        applied = copy.deepcopy(record["applied"])
        applied["lead"].update({"key": "muse-spark", "selector": None, "generation": None})
        self.mutate(FIXED, applied=applied)
        self.assertTrue(sessions.lead_needs_choice(self.store.load(FIXED), self.runtime.lineup_catalog()))

    def test_n1_remedy_reaches_the_exec_seam(self) -> None:
        self.needs_choice()
        with mock.patch.object(lineup, "_read_scope_launch_files",
                               side_effect=AssertionError("read a scope file")):
            code, out, err = self.lineup("--relaunch", "--its-exited", "profile", "quality")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.execs), 1)
        record = self.store.load(FIXED)
        self.assertEqual((record["profile"], record["lineup_generation"]), ("quality", 1))

    def test_without_its_exited_or_from_inside_it_is_pending(self) -> None:
        self.needs_choice()
        code, out, _ = self.lineup("--relaunch", "profile", "quality")
        self.assertIn(f"relaunch needed: {lineup.REASON_GEN0}", out)
        self.assertEqual(self.store.load(FIXED)["pending"]["profile"], "quality")
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED
        code, out, _ = self.request("profile economy")
        self.assertIn("Recorded as a pending change", out)
        self.assertEqual(self.store.load(FIXED)["pending"]["profile"], "economy")
        self.assertEqual(self.execs, [])
        # The resume-override remedy (no --relaunch) from outside: pending too
        del self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"]
        code, out, _ = self.lineup("profile", "quality")
        self.assertEqual(self.store.load(FIXED)["pending"]["profile"], "quality")

    def test_pin_and_a_matching_follow_are_record_only_at_gen_0(self) -> None:
        with mock.patch.object(lineup, "_read_scope_launch_files",
                               side_effect=AssertionError("read a scope file")):
            code, out, _ = self.request("pin")
            self.assertIn("unchanged", out)
            self.mutate(FIXED, profile="balanced")
            record = self.store.load(FIXED)
            document = sessions.applied_document(record)
            document.update({"name": "mirror"})
            self.runtime.profiles.save(document, target="mirror")
            self.mutate(FIXED, profile="mirror")
            code, out, _ = self.request("follow")
        self.assertIn(f"session {FIXED[:8]} now follows profile mirror; its lineup already matches (gen 0)", out)
        self.assertTrue(self.store.load(FIXED)["follow"])

    def test_a_scope_less_gen_1_record_is_relaunch(self) -> None:
        with _fixed_id(OTHER):
            record = self.launch_fresh(self.profile_target("balanced"))
        scope._remove_tree(self.live(OTHER))
        with mock.patch.object(self, "rid", record["runtime_session_id"]):
            code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn(f"relaunch needed: {lineup.REASON_NO_SCOPE}", out)

    def test_show_on_gen_0_names_the_2x_scope(self) -> None:
        self.needs_choice()
        code, out, _ = self.request("")
        self.assertIn("needs a lead choice:", out)
        self.assertIn(lineup.SHOW_2X_LINE.format(mid=FIXED), out)


# --------------------------------------------------------- classification


class ClassifyTests(LineupCase):
    def test_reason_1_lead_outside_the_lead_set(self) -> None:
        live = self.live(self.mid)
        lead_set = strict_json.loads((live / "lead-set.json").read_bytes())
        lead_set["rows"] = [r for r in lead_set["rows"] if r["key"] != "fable"]
        state.atomic_write(live / "lead-set.json", strict_json.canonical_file_bytes(lead_set))
        self.refence()
        code, out, _ = self.request("profile max")
        self.assertIn("relaunch needed: lead Fable 5 · 1M selector (claude-fable-5[1m]) is outside "
                      "this session's lead set (lead class large); a lead outside the set needs a "
                      "relaunch", out)
        self.assertIn(lineup.LEAD_SWITCH_HINT, out)
        self.assertEqual(self.store.load(self.mid)["pending"]["profile"], "max")

    def test_a_lead_in_the_lead_set_applies_agents_live_and_asks_for_model(self) -> None:
        before = self.store.load(self.mid)
        code, out, _ = self.request("profile max")
        self.assertIn(
            "switch the lead with /model → Fable 5 · 1M selector · ultracode "
            "(claude-fable-5[1m]); press s (this session only)",
            out,
        )
        self.assertIn(lineup.RELOAD_LINES[0], out)
        after = self.store.load(self.mid)
        self.assertEqual(after["applied"]["lead"], before["applied"]["lead"])
        self.assertEqual(after["lead_target"], {"key": "fable", "effort": "ultracode",
                                                "selector": "claude-fable-5[1m]"})
        self.assertEqual(after["scope_lead"], sessions.lead_ref(before["applied"]["lead"]))
        self.assertEqual((after["profile"], after["follow"]), ("max", True))
        self.assertIn("matches", self.converge_report()[-1])

    def test_an_effort_only_lead_change_applies_at_the_next_resume(self) -> None:
        # The same selector at another effort cannot be /model-ed.
        self.save_profile("balanced-x", lambda d: d["lead"].update({"effort": "xhigh"}))
        code, out, _ = self.request("profile balanced-x")
        self.assertIn(
            f"lead effort ultracode → xhigh applies at the next resume (claude-multi -r {self.mid})",
            out,
        )
        self.assertNotIn("switch the lead with /model", out)
        self.assertEqual(self.store.load(self.mid)["lead_target"]["effort"], "xhigh")

    def test_reasons_2_to_6_each_isolated(self) -> None:
        cases = {
            "grok": (lambda d: d["lead"].update({"model": "grok46", "effort": "xhigh"}),
                     "lead class large → grok"),
            "nat": (lambda d: d["native_agents"].update({"plan": "off"}),
                    "native agents: plan native → off"),
            "wf": (lambda d: d.update({"workflows": "off"}), "workflows native → off"),
            "prov": (lambda d: d.update({"lead_providers": ["anthropic"]}),
                     "lead providers any → anthropic"),
            "pct": (lambda d: d.update({"settings_overrides": {"compaction_percent": 80}}),
                    "compaction override none → 80"),
        }
        for name, (mutate, reason) in cases.items():
            with self.subTest(profile=name):
                self.save_profile(name, mutate)
                code, out, _ = self.request(f"profile {name}")
                self.assertIn(reason, out)
                self.assertIn("Recorded as a pending change", out)

    def test_reason_7_launch_files_changed_after_launch(self) -> None:
        live = self.live(self.mid)
        settings_doc = strict_json.loads((live / "settings.json").read_bytes())
        settings_doc["env"]["CLAUDE_CODE_RETRY_WATCHDOG"] = "0"
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(settings_doc))
        code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn(lineup.REASON_FENCE_DIGEST, out)
        # a request that changes no agent is not blocked by it
        code, out, _ = self.request("pin")
        self.assertIn("is now pinned", out)

    def test_reason_7_a_selector_outside_the_launch_time_fence(self) -> None:
        live = self.live(self.mid)
        settings_doc = strict_json.loads((live / "settings.json").read_bytes())
        selector = "claude-multi-qwen38-max[1m]"
        settings_doc["availableModels"] = [s for s in settings_doc["availableModels"] if s != selector]
        state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(settings_doc))
        self.refence()
        code, out, _ = self.request("set reviewer=qwen38")
        self.assertIn(lineup.REASON_FENCE_GAP.format(label="reviewer", selector=selector), out)

    def test_reason_8_context_guard(self) -> None:
        live = self.live(self.mid)
        lead_set = strict_json.loads((live / "lead-set.json").read_bytes())
        lead_set["context"]["window"] = 999_999_999
        state.atomic_write(live / "lead-set.json", strict_json.canonical_file_bytes(lead_set))
        self.refence()
        code, out, _ = self.request("set reviewer=qwen38")
        self.assertNotIn("context guard", out)
        self.assertIn("/reload-plugins", out)

    def test_reason_9_relaunch_forces_a_pending_change_from_inside(self) -> None:
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = self.mid
        code, out, _ = self.lineup("--relaunch", "set implementer=opus55:xhigh")
        self.assertIn(f"relaunch needed: {lineup.REASON_FORCED}", out)
        self.assertEqual(self.execs[1:], [])

    def test_unreadable_lead_set_is_a_single_reason(self) -> None:
        (self.live(self.mid) / "lead-set.json").write_bytes(b"{")
        code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn(lineup.REASON_LEAD_SET_UNREADABLE.format(mid=self.mid), out)


class Cp04Tests(LineupCase):
    """Policies equal -> LIVE, else relaunch."""

    launch_profile = None

    def test_direct_session_to_a_policy_equal_balanced_is_live(self) -> None:
        native = {"explore": "native", "plan": "native", "general_purpose": "off"}
        balanced = self.runtime.profiles.load("balanced")
        direct_eq = {"version": 2, "name": "direct-eq", "description": "",
                     "lead": copy.deepcopy(balanced["lead"]), "agents": {},
                     "native_agents": dict(native), "workflows": balanced["workflows"]}
        self.runtime.profiles.save(direct_eq)
        self.runtime.profiles.update("balanced", lambda d: d["native_agents"].update(native))
        with _fixed_id():
            record = self.launch_fresh(self.profile_target("direct-eq"))
        self.mid, self.rid = FIXED, record["runtime_session_id"]
        self.assertEqual(self.scope_files().get(".claude/agents/cm-implementer.md"), None)
        code, out, _ = self.request("profile balanced")
        self.assertIn(lineup.RELOAD_LINES[0], out)
        self.assertIn("session now follows profile balanced", out)
        self.assertIn(".claude/agents/cm-implementer.md", self.scope_files())
        self.assertTrue(self.store.load(FIXED)["follow"])

    def test_the_direct_seed_to_unedited_balanced_is_relaunch(self) -> None:
        with _fixed_id():
            record = self.launch_fresh(self.profile_target("direct"))
        self.mid, self.rid = FIXED, record["runtime_session_id"]
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED
        code, out, _ = self.request("profile balanced")
        self.assertIn("native agents: explore native → replace", out)
        self.assertIn("Recorded as a pending change", out)
        self.assertEqual(self.store.load(FIXED)["pending"]["kind"], "profile")


# ---------------------------------------------------------------- live apply


class LiveApplyTests(LineupCase):
    def test_write_order_record_last_and_launch_files_untouched(self) -> None:
        self.save_profile("mix", lambda d: (
            d["agents"].pop("cm-analyst-strong"),
            d["agents"].update({"cm-designer": {"model": "sol", "effort": "high"},
                                "cm-implementer": {"model": "opus55", "effort": "xhigh"}}),
        ))
        live = self.live(self.mid)
        keep = {name: (live / name).read_bytes() for name in ("settings.json", "lead-set.json")}
        prompts = sorted(p.name for p in self.store.root.glob("lead-prompt-*"))
        before = self.store.load(self.mid)
        order: list[str] = []
        real_write, real_remove, real_save = state.atomic_write, state.remove_private, self.store.save

        def write(path, data):
            order.append(f"write {Path(path).relative_to(live) if str(path).startswith(str(live)) else Path(path).name}")
            return real_write(path, data)

        def remove(path):
            order.append(f"remove {Path(path).relative_to(live)}")
            return real_remove(path)

        def save(record):
            order.append("record")
            return real_save(record)

        with mock.patch.object(state, "atomic_write", write), \
                mock.patch.object(state, "remove_private", remove), \
                mock.patch.object(self.store, "save", save):
            code, out, _ = self.request("profile mix")
        self.assertEqual(code, 0)
        scoped = [item for item in order if item.startswith(("write .claude", "write lineup", "remove", "record"))]
        self.assertEqual(scoped, [
            "write .claude/agents/cm-designer.md",
            "write .claude/agents/cm-implementer.md",
            "remove .claude/agents/cm-analyst-strong.md",
            "write lineup.md",
            "write lineup.gen",
            "record",
        ])
        after = self.store.load(self.mid)
        self.assertEqual(after["lineup_generation"], 2)
        self.assertEqual(after["launch_epoch"], before["launch_epoch"])
        self.assertEqual(after["launch_fence"], before["launch_fence"])
        self.assertNotEqual(after["mutation_token"], before["mutation_token"])
        self.assertNotEqual(after["applied_hash"], before["applied_hash"])
        for name, data in keep.items():
            self.assertEqual((live / name).read_bytes(), data)
        self.assertEqual(sorted(p.name for p in self.store.root.glob("lead-prompt-*")), prompts)
        self.assertFalse(self.prev(self.mid).exists())
        self.assertTrue((live / "lineup.gen").read_text().startswith("2 "))
        log = lineup_log.lineup_log_path(self.store.root, self.mid).read_text().splitlines()
        entry = strict_json.loads(log[-1])
        self.assertEqual(entry["event"], "apply")
        self.assertEqual(entry["lineup_gen"], (live / "lineup.gen").read_text().strip())
        self.assertEqual(entry["label"], lineup_log.binding_label(2))
        self.assertEqual(sorted(entry["changes"]), ["cm-analyst-strong", "cm-designer", "cm-implementer"])
        self.assertIsNone(entry["changes"]["cm-designer"]["from"])
        self.assertIsNone(entry["changes"]["cm-analyst-strong"]["to"])
        self.assertEqual(self.converge_report()[-1], "live scope matches the record-authoritative compile")

    def test_set_pins_and_unset_all_then_direct_keeps_the_scope_consistent(self) -> None:
        code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn("session is now pinned (follow off)", out)
        self.assertFalse(self.store.load(self.mid)["follow"])
        self.assertIn("matches", self.converge_report()[-1])
        code, out, _ = self.request("pin")
        self.assertEqual(
            out,
            f"session {self.mid[:8]} is already pinned (follow off); lineup gen 2 unchanged\n"
            f"{lineup.NEXT_NONE}\n",
        )
        code, out, _ = self.request("follow")
        self.assertIn("session now follows profile balanced", out)
        self.assertIn("matches", self.converge_report()[-1])
        code, out, _ = self.request("follow")
        self.assertIn("its lineup already matches (gen 3)", out)

    def test_unknown_profile_and_invalid_requests_write_nothing(self) -> None:
        before = self.record_bytes(self.mid)
        files = self.scope_files()
        code, out, _ = self.request("profile nosuch")
        self.assertEqual(out, f"claude-multi: {lineup.R11.format(name='nosuch')}\n")
        code, out, _ = self.request("set analyst=sol:low")  # no gateway selector mapping for low
        self.assertTrue(out.startswith(f"claude-multi: {lineup.R14}\n- "), out)
        code, out, _ = self.request("set implementer=nosuch-model")
        self.assertIn(lineup.R14, out)
        self.assertEqual(self.record_bytes(self.mid), before)
        self.assertEqual(self.scope_files(), files)

    def test_follow_without_a_profile_or_on_a_removed_one(self) -> None:
        self.mutate(self.mid, profile=None, follow=False)
        code, out, _ = self.request("follow")
        self.assertIn(lineup.R12.format(m8=self.mid[:8]), out)
        self.mutate(self.mid, profile="gone")
        code, out, _ = self.request("follow")
        self.assertIn(lineup.R13.format(m8=self.mid[:8], p="gone"), out)

    def test_credentials_of_newly_bound_agents_only(self) -> None:
        secrets = Path(self.env["CLAUDE_MULTI_SECRET_ENV"])
        code, out, _ = self.request("set reviewer=kimi-k3")
        self.assertIn(lineup.RELOAD_LINES[0], out)
        state.atomic_write(secrets, b"QWEN_CLAUDE_API_KEY=v4-test-dummy\n")
        before = self.record_bytes(self.mid)
        code, out, _ = self.request("set implementer=kimi-k3")
        self.assertTrue(out.startswith("claude-multi: implementer → kimi-k3: provider kimi has no credential ("), out)
        self.assertIn("connect it first — nothing changed", out)
        self.assertEqual(self.record_bytes(self.mid), before)
        # the reviewer stays bound to kimi-k3: an unchanged binding never refuses
        code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn(lineup.RELOAD_LINES[0], out)

    def test_project_agents_shadowing_a_newly_bound_id_are_r16(self) -> None:
        self.request("unset designer")
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "cm-designer.md").write_text("---\nname: cm-designer\n---\n")
        code, out, _ = self.request("set designer=sol")
        self.assertIn("project agent(s) would shadow newly bound ids:", out)
        self.assertIn("(cm-designer); rename them or keep cm-designer unbound", out)

    def test_s1_s2_hermetic_durability_of_record_authority(self) -> None:
        self.request("set implementer=opus55:xhigh")
        compiled = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "S1"),
            action="resume", passthrough=[], session_id=self.mid,
        )
        disk = scope.read_disk_plan(self.live(self.mid), compiled.result.scope_plan)
        self.assertIsNotNone(disk)
        agent = ".claude/agents/cm-implementer.md"
        self.assertIn(b"model: claude-multi-opus-5-5[1m]", disk.agent_files[agent])
        self.assertIn(b"model: claude-multi-opus-5-5[1m]", compiled.result.scope_plan.agent_files[agent])


class PendingTests(LineupCase):
    def test_inside_pending_then_a_live_apply_drops_it_then_resume_consumes(self) -> None:
        before = self.store.load(self.mid)
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = self.mid
        code, out, _ = self.request("direct")
        pending = self.store.load(self.mid)
        self.assertEqual(pending["applied_hash"], before["applied_hash"])
        self.assertNotEqual(pending["mutation_token"], before["mutation_token"])
        self.assertEqual(pending["pending"]["kind"], "lineup")
        self.assertEqual(pending["pending"]["document"]["agents"], {})
        code, out, _ = self.request("set implementer=opus55:xhigh")
        self.assertIn("dropped the pending relaunch change (native agents: explore replace → "
                      "native; native agents: general_purpose off → on)", out)
        self.assertNotIn("pending", self.store.load(self.mid))
        code, out, _ = self.request("direct")
        resumed = self.resume(self.mid)
        self.assertNotIn("pending", resumed)
        self.assertEqual(resumed["applied"]["agents"], {})
        self.assertIsNone(resumed["profile"])

    def test_relaunch_its_exited_from_outside_execs_the_target(self) -> None:
        self.write_transcript(self.rid)
        code, out, err = self.lineup("--relaunch", "--its-exited", "profile", "quality")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.execs), 2)
        record = self.store.load(self.mid)
        self.assertEqual((record["profile"], record["launch_epoch"]), ("quality", 2))

    def test_non_interactive_relaunch_without_its_exited_is_pending(self) -> None:
        code, out, _ = self.lineup("--relaunch", "profile", "quality")
        self.assertIn("Recorded as a pending change", out)
        self.assertEqual(len(self.execs), 1)

    def test_the_confirm_runs_without_the_lifecycle_lock(self) -> None:
        free: list[bool] = []

        def confirm(*_args, **_kwargs):
            lock = self.store.lifecycle_lock(self.mid)
            free.append(lock.acquire(blocking=False))
            if free[-1]:
                lock.release()
            migration = sessions.migration_lock(self.store.root)
            free.append(migration.acquire(blocking=False))
            if free[-1]:
                migration.release()
            return False

        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch('claude_multi.cli.screens.transition._transition_confirm', side_effect=confirm):
            code, out, err = self.lineup("--relaunch", "profile", "quality", skill=False,
                                         interactive=True, text="")
        self.assertEqual(code, 0, err)
        self.assertEqual(free, [True, True])
        self.assertIn("Nothing was launched.", out)

    def test_a_change_between_the_sample_and_prepare_refuses(self) -> None:
        def confirm(*_args, **_kwargs):
            record = self.store.load(self.mid)
            self.store.save({**record, "mutation_token": sessions.new_mutation_token()})
            return True

        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch('claude_multi.cli.screens.transition._transition_confirm', side_effect=confirm):
            code, out, err = self.lineup("--relaunch", "profile", "quality", skill=False,
                                         interactive=True, text="")
        self.assertEqual(code, 1)  # a refused request; 2 is kept for an invalid one
        self.assertIn("changed while the relaunch was being confirmed", err)
        self.assertEqual(len(self.execs), 1)

    def test_the_relaunch_with_real_locks_does_not_deadlock(self) -> None:
        self.write_transcript(self.rid)
        done = threading.Event()
        result: list = []

        def run() -> None:
            try:
                result.append(self.lineup("--relaunch", "--its-exited", "profile", "quality"))
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(done.wait(10), "lineup --relaunch deadlocked on its own lifecycle lock")
        self.assertEqual(result[0][0], 0, result)


class ShowTests(LineupCase):
    def test_show_names_pending_lead_target_needs_choice_and_drift(self) -> None:
        self.request("profile max")
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = self.mid
        self.request("direct")
        code, out, _ = self.request("")
        self.assertIn("pending relaunch change: native agents:", out)
        self.assertIn("requested lead: Fable 5 · 1M selector · ultracode (claude-fable-5[1m])", out)

    def test_sessions_show_lists_needs_choice_pending_and_lead_target(self) -> None:
        self.request("profile max")
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = self.mid
        self.request("direct")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["sessions", "show", self.mid], runtime=self.runtime,
                            output_stream=out, interactive=False)
        self.assertEqual(code, 0)
        text = err.getvalue()
        self.assertIn("pending relaunch change: native agents:", text)
        self.assertIn(f"applies at the next resume (claude-multi -r {self.mid})", text)
        self.assertIn("requested lead: Fable 5 · 1M selector · ultracode (claude-fable-5[1m])", text)
        record = self.store.load(self.mid)
        self.assertEqual(strict_json.loads(out.getvalue()), record)
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "gone-lead"
        self.mutate(self.mid, applied=applied)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["sessions", "show", self.mid], runtime=self.runtime, output_stream=out,
                            interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual(strict_json.loads(out.getvalue()), self.store.load(self.mid))
        self.assertIn(
            f"needs a lead choice: lead gone-lead: unknown to catalog", err.getvalue()
        )
        self.assertIn(f"claude-multi lineup --session {self.rid} --relaunch --its-exited "
                      "profile <name>", err.getvalue())

    def test_resolve_fork_names_the_v4_adopt_command(self) -> None:
        self.mutate(
            self.mid,
            pending_forks=[{"session_id": OTHER, "observed_at": NOW}],
            identity_state=sessions.IDENTITY_PENDING_FORK,
        )
        out = io.StringIO()
        code = cli.main(["sessions", "resolve-fork", self.mid, OTHER, "--yes"], runtime=self.runtime,
                        output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(f"`claude-multi sessions link {OTHER} --profile balanced`", out.getvalue())

    def test_show_never_raises_on_an_unknown_lead(self) -> None:
        record = self.store.load(self.mid)
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "gone-lead"
        self.mutate(self.mid, applied=applied)
        code, out, _ = self.request("")
        self.assertEqual(code, 0)
        self.assertIn("needs a lead choice: lead gone-lead: unknown to catalog", out)


class ShowProviderErrorsTests(LineupCase):
    """``/cm show``: the failed requests of the last day per provider the
    session binds, from the bounded gateway log doctor reads, with the
    observation's scope and coverage and the fitting remedy."""

    def setUp(self) -> None:
        super().setUp()
        from datetime import timedelta
        from tests.test_observations import NOW as LOG_NOW

        import claude_multi.cli.gateway_facts as gateway_facts

        self.gateway_facts = gateway_facts
        self.log_now = LOG_NOW
        clock = mock.patch.object(gateway_facts, "_doctor_now", return_value=LOG_NOW + timedelta(hours=1))
        clock.start()
        self.addCleanup(clock.stop)
        record = self.store.load(self.mid)
        self.lcat = self.runtime.lineup_catalog()
        self.agent = "cm-explorer"
        binding = record["applied"]["agents"][self.agent]
        self.agent_selector = binding["selector"].removesuffix("[1m]")
        self.agent_provider = self.lcat.lines[binding["key"]]["provider"]
        lead = record["applied"]["lead"]
        self.lead_selector = lead["selector"].removesuffix("[1m]")
        self.lead_provider = self.lcat.lines[lead["key"]]["provider"]
        self.assertNotEqual(self.agent_provider, self.lead_provider, "the fixture lineup spans two providers")

    def requests(self, *rows: tuple[str, int, str]):
        """One gateway-log request per row: (request id, final status, selector)."""

        from tests.test_observations import final, log

        records = []
        for rid, status, selector in rows:
            records += [log(f"model={selector} provider=fixture", rid=rid, when=self.log_now),
                        final(status, rid=rid, when=self.log_now)]
        return records

    def show(self, window) -> list[str]:
        with mock.patch.object(self.gateway_facts, "_read_gateway_journal", return_value=window):
            code, out, err = self.request("")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("gateway: "), lines[:3])
        return lines

    def test_errors_are_counted_per_bound_provider_with_their_remedy(self) -> None:
        from tests.test_observations import source

        window = source(*self.requests(
            ("r1", 429, self.agent_selector), ("r2", 429, self.agent_selector), ("r3", 503, self.agent_selector),
            ("r4", 429, self.agent_selector), ("r5", 200, self.agent_selector), ("r6", 200, self.lead_selector),
            ("r7", 500, "not-a-bound-selector")), coverage="truncated")
        lines = self.show(window)
        self.assertEqual(lines[2], "provider errors, last 24 h (gateway-wide, not only this session; "
                                   f"log read truncated, back to {self.log_now.astimezone():%Y-%m-%d %H:%M}):")
        line = lines[3]
        agents = self.store.load(self.mid)["applied"]["agents"]
        slots = [profile.label(rid) for rid in catalog.AGENT_ROLE_IDS
                 if rid in agents and self.lcat.lines[agents[rid]["key"]]["provider"] == self.agent_provider]
        self.assertTrue(line.startswith(f"  {self.agent_provider} ({', '.join(slots)}): 4 failed — "
                                        "HTTP 429 ×3, HTTP 503 ×1; last "), line)
        others = [name for name in lineup.fallback_providers(self.runtime) if name != self.agent_provider]
        self.assertTrue(others, "a fixture profile is a fallback for another provider")
        self.assertTrue(line.endswith(f" — {lineup.PROVIDER_ERRORS_FALLBACK} or /cm set <agent>=<model>"), line)
        self.assertEqual(lines[4], f"  none observed for {self.lead_provider}")
        self.assertNotIn("not-a-bound-selector", "\n".join(lines))

    def test_the_fallback_remedy_has_one_size_whatever_the_profiles(self) -> None:
        # Five hundred profiles, each a fallback for a provider of its own,
        # and one whose provider id is 20,012 characters: the remedy stays
        # the constant /cm fallback <provider> with /cm profiles, and no
        # identifier is cut into a command.
        from tests.test_observations import source

        long_provider = "p" + "x" * 20011
        for index in range(500):
            self.save_profile(f"fallback-{index:03d}",
                              lambda document, index=index: document.update(primary_provider=f"vendor-{index:03d}"))
        self.save_profile("fallback-long", lambda document: document.update(primary_provider=long_provider))
        self.assertGreaterEqual(len(lineup.fallback_providers(self.runtime)), 501)
        lines = self.show(source(*self.requests(("r1", 429, self.agent_selector))))
        line = next(line for line in lines if line.startswith(f"  {self.agent_provider} ("))
        self.assertLess(len(line), 240, line[:300])
        self.assertTrue(line.endswith(f" — {lineup.PROVIDER_ERRORS_FALLBACK} or /cm set <agent>=<model>"), line)
        self.assertIn("/cm fallback <provider>", lineup.PROVIDER_ERRORS_FALLBACK)
        self.assertIn("/cm profiles", lineup.PROVIDER_ERRORS_FALLBACK)
        shown = "\n".join(lines[:5])
        self.assertNotIn("vendor-", shown)
        self.assertNotIn("pxxxx", shown)

    def test_without_errors_it_says_none_were_observed_and_where_it_looked(self) -> None:
        from tests.test_observations import source

        lines = self.show(source(*self.requests(("r1", 200, self.agent_selector), ("r2", 200, self.lead_selector))))
        providers = ", ".join(sorted({self.agent_provider, self.lead_provider}))
        self.assertEqual(lines[2], f"provider errors, last 24 h: none observed for {providers} "
                                   "(gateway-wide, not only this session; log read bounded)")

    def test_an_unreadable_log_is_unavailable_never_zero(self) -> None:
        from claude_multi.platform.observation import LogWindow

        for window in (None, LogWindow()):
            with self.subTest(window=window):
                lines = self.show(window)
                self.assertEqual(lines[2], lineup.PROVIDER_ERRORS_UNAVAILABLE)
                self.assertNotIn("none observed", "\n".join(lines))
        with mock.patch.object(self.gateway_facts, "report_events", side_effect=OSError("unreadable")):
            code, out, _err = self.request("")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[2], lineup.PROVIDER_ERRORS_UNAVAILABLE)


class CmVerbTableTests(unittest.TestCase):
    """One /cm verb table: the parser, the help line, the refusal and the
    generated skill grammar all read it; each verb has fixtures."""

    VALID = {
        "show": [[], ["show"]],
        "profiles": [["profiles"]],
        "profile": [["profile", "balanced"]],
        "set": [["set", "implementer=sol"], ["set", "cm-analyst=opus:high"]],
        "unset": [["unset", "implementer"], ["unset", "cm-reviewer"]],
        "direct": [["direct"], ["direct", "sol"], ["direct", "sol:ultracode"]],
        "pin": [["pin"]],
        "follow": [["follow"]],
        "fallback": [["fallback", "openai"]],
        "review": [["review"], ["review", "high-stakes"], ["review", "HEAD~1..HEAD"],
                   ["review", "high-stakes", "main..HEAD"]],
        "quota": [["quota"]],
    }
    INVALID = {
        "show": [["show", "x"]],
        "profiles": [["profiles", "x"]],
        "profile": [["profile"], ["profile", "Bad Name"], ["profile", "a", "b"]],
        "set": [["set"], ["set", "nobody=sol"], ["set", "implementer=sol:loud"]],
        "unset": [["unset"], ["unset", "nobody"]],
        "direct": [["direct", "sol", "x"], ["direct", "sol:loud"]],
        "pin": [["pin", "now"]],
        "follow": [["follow", "now"]],
        "fallback": [["fallback"], ["fallback", "Open AI"], ["fallback", "a", "b"]],
        "review": [["review", "--force"], ["review", "a", "b", "c"], ["review", "high-stakes", "high-stakes"]],
        "quota": [["quota", "all"]],
    }

    def test_every_verb_has_a_parser_and_fixtures(self) -> None:
        self.assertEqual(set(self.VALID), set(lineup_files.CM_VERB_NAMES))
        self.assertEqual(set(self.INVALID), set(lineup_files.CM_VERB_NAMES))
        for verb in lineup_files.CM_VERBS:
            for words in self.VALID[verb.name]:
                with self.subTest(words=words):
                    request = lineup.parse_request(words)
                    self.assertEqual(request.verb, verb.name)
                    self.assertEqual(request.writes, verb.writes)
            for words in self.INVALID[verb.name]:
                with self.subTest(words=words), self.assertRaises(lineup.LineupRefusal):
                    lineup.parse_request(words)
        with self.assertRaises(lineup.LineupRefusal):
            lineup.parse_request(["unknown-verb"])

    def test_help_refusal_and_skill_list_the_same_verbs(self) -> None:
        for verb in lineup_files.CM_VERBS:
            with self.subTest(verb=verb.name):
                self.assertIn(f"/cm {verb.spelled()}", lineup.HELP_LINE)
                self.assertIn(verb.spelled(), lineup.R7)
                skill = scope.SKILL_MD_BYTES.decode()
                self.assertIn("(/cm," if verb.name == "show" else f"/cm {verb.spelled(placeholders='upper')}", skill)
        self.assertEqual(lineup.HELP_LINE.count("/cm "), len(lineup_files.CM_VERBS))
        self.assertEqual(lineup._READ_VERBS, frozenset(v.name for v in lineup_files.CM_VERBS if not v.writes))
        self.assertEqual(scope.SKILL_MD_BYTES, scope.skill_md_bytes())
        self.assertNotIn(scope.SKILL_MD_BYTES, scope.PREVIOUS_SKILL_MD_BYTES)

    def test_a_verb_missing_from_the_table_has_no_parser(self) -> None:
        # The import check pairs the table with the parsers: a parser for a
        # verb the table does not list (or the reverse) fails at import.
        self.assertEqual(tuple(lineup._VERB_PARSERS), lineup_files.CM_VERB_NAMES)


class PreviewApplyTests(LineupCase):
    """A read-only preview of a lineup change, then a locked apply that
    refuses with a refreshed preview when the session moved meanwhile."""

    def _pending(self) -> None:
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = self.mid
        self.request("direct")
        self.runtime.environ.pop("CLAUDE_MULTI_MANAGED_ID")
        self.assertIn("pending", self.store.load(self.mid))

    def test_keep_current_lineup_previews_the_discard_and_follow_off(self) -> None:
        self._pending()
        record = self.store.load(self.mid)
        before, files = self.record_bytes(self.mid), self.scope_files()
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["pin"]))
        self.assertIn(lineup.DISCARD_PENDING, shown.text)
        self.assertIn("follow: on (profile balanced) → off", shown.text)
        self.assertIn("pending change (", shown.text)
        self.assertIn("): dropped", shown.text)
        self.assertIn(lineup.EFFECT_RECORD, shown.text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), (before, files))
        result = lineup.apply_preview(self.runtime, shown)
        self.assertEqual(result.exit_code, 0)
        after = self.store.load(self.mid)
        self.assertNotIn("pending", after)
        self.assertFalse(after["follow"])
        self.assertEqual((after["applied_hash"], after["lineup_generation"]),
                         (record["applied_hash"], record["lineup_generation"]))
        self.assertEqual(self.scope_files(), files)

    def test_a_set_preview_shows_the_change_and_its_effect_without_writing(self) -> None:
        record = self.store.load(self.mid)
        before, files = self.record_bytes(self.mid), self.scope_files()
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["set", "implementer=opus55:xhigh"]))
        self.assertIn("cm-implementer:", shown.text)
        self.assertIn("follow: on (profile balanced) → off", shown.text)
        self.assertTrue(lineup.EFFECT_LIVE in shown.text or lineup.EFFECT_RELAUNCH in shown.text, shown.text)
        self.assertIn(lineup.PREVIEW_ONLY, shown.text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), (before, files))

    def test_follow_previews_its_consequence(self) -> None:
        self.request("pin")
        self.save_profile("balanced-edit", lambda doc: None)
        record = self.store.load(self.mid)
        before = self.record_bytes(self.mid)
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["follow"]))
        self.assertIn("follow: off → on (profile balanced)", shown.text)
        self.assertEqual(self.record_bytes(self.mid), before)
        self.assertEqual(lineup.apply_preview(self.runtime, shown).exit_code, 0)
        self.assertTrue(self.store.load(self.mid)["follow"])

    def test_a_moved_session_gets_a_refreshed_preview_not_an_apply(self) -> None:
        record = self.store.load(self.mid)
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["pin"]))
        self.request("set implementer=opus55:xhigh")  # the session moved after the preview
        moved = self.record_bytes(self.mid)
        result = lineup.apply_preview(self.runtime, shown)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.text.startswith(lineup.PREVIEW_STALE), result.text)
        self.assertEqual(self.record_bytes(self.mid), moved)

    def test_a_profile_edited_during_the_confirmation_is_refused_with_the_new_preview(self) -> None:
        self.request("pin")
        record = self.store.load(self.mid)
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["follow"]))
        self.assertIn("follow: off → on (profile balanced)", shown.text)
        # Another window edits the profile while the preview waits for its yes.
        _user_copy(self, "balanced", agents={**self.runtime.profiles.load("balanced")["agents"],
                                             "cm-analyst": {"model": "opus55", "effort": "xhigh"}})
        before, files = self.record_bytes(self.mid), self.scope_files()
        with mock.patch.object(self.store, "save", wraps=self.store.save) as saved:
            result = lineup.apply_preview(self.runtime, shown)
        self.assertEqual(result.exit_code, 1, result.text)
        self.assertTrue(result.text.startswith(lineup.PREVIEW_STALE), result.text)
        self.assertIn("cm-analyst", result.text)
        saved.assert_not_called()
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), (before, files))
        self.assertFalse(self.store.load(self.mid)["follow"])
        # The refreshed preview, confirmed, applies what it shows.
        again = lineup.preview(self.runtime, self.store.load(self.mid), lineup.parse_request(["follow"]))
        self.assertEqual(lineup.apply_preview(self.runtime, again).exit_code, 0)
        self.assertTrue(self.store.load(self.mid)["follow"])

    def test_a_fallback_recipe_edited_during_the_confirmation_is_refused(self) -> None:
        record = self.store.load(self.mid)
        shown = lineup.preview(self.runtime, record, lineup.parse_request(["fallback", "openai"]))
        recipe = self.runtime.profiles.load("openai")
        _user_copy(self, "openai", agents={**recipe["agents"], "cm-explorer": {"model": "opus55", "effort": "xhigh"}})
        before = self.record_bytes(self.mid)
        result = lineup.apply_preview(self.runtime, shown)
        self.assertEqual(result.exit_code, 1, result.text)
        self.assertTrue(result.text.startswith(lineup.PREVIEW_STALE), result.text)
        self.assertEqual(self.record_bytes(self.mid), before)

    def test_a_read_verb_has_nothing_to_preview(self) -> None:
        with self.assertRaises(lineup.LineupRefusal):
            lineup.preview(self.runtime, self.store.load(self.mid), lineup.parse_request(["show"]))


if __name__ == "__main__":
    unittest.main()


class SkillExpansionTests(LineupCase):
    """The /cm skill body through ``sh -c`` with a real launcher wrapper.

    The client substitutes ``${CLAUDE_SESSION_ID}`` and ``$ARGUMENTS`` raw
    (an observed client fact), then the shell runs the line; the wrapper on PATH is the
    real ``cli.main`` over the fixture catalog and this case's temp state.
    """

    def setUp(self) -> None:
        super().setUp()
        import shutil as _shutil
        import sys as _sys

        if _shutil.which("sh") is None:  # pragma: no cover - every supported host has sh
            self.skipTest("needs /bin/sh")
        from _catalog import FIXTURE_ROOT
        from _layout import REPO_ROOT

        bindir = self.root / "wrapper-bin"
        bindir.mkdir()
        wrapper = bindir / "claude-multi"
        wrapper.write_text(
            f"#!{_sys.executable}\n"
            "import os, sys\n"
            "from claude_multi.cli.entry import main\n"
            "from claude_multi.cli.runtime import Runtime\n"
            f"runtime = Runtime(asset_root={str(FIXTURE_ROOT)!r}, environ=dict(os.environ), "
            f"cwd={str(self.project)!r})\n"
            "raise SystemExit(main(sys.argv[1:], runtime=runtime))\n"
        )
        wrapper.chmod(0o700)
        existing = [
            str(Path(p).resolve()) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p
        ]
        self.child_env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": self.env["HOME"],
            "XDG_STATE_HOME": self.env["XDG_STATE_HOME"],
            "XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"],
            "XDG_DATA_HOME": str(self.root / "data"),
            "PYTHONPATH": os.pathsep.join(
                [*existing, str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]
            ),
        }
        line = scope.SKILL_MD_BYTES.decode().splitlines()[-1]
        self.template = line.removeprefix("!`").removesuffix("`")

    def expand(self, arguments: str) -> tuple[int, str, str]:
        import subprocess

        command = self.template.replace("${CLAUDE_SESSION_ID}", self.rid).replace(
            "$ARGUMENTS", arguments
        )
        work = self.root / "cwd"
        work.mkdir(exist_ok=True)
        result = subprocess.run(
            ["sh", "-c", command], cwd=work, env=self.child_env,
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode, result.stdout, result.stderr

    def test_the_skill_cases_end_in_the_launchers_refusal_or_apply(self) -> None:
        cases = {
            "set cm-explorer=opus[1m]": lineup.R7.format(text="set cm-explorer=opus[1m]"),
            "a;b": lineup.R7.format(text="a;b"),
            "$(touch pwned)": lineup.R7.format(text="$(touch pwned)"),
            "\\!profile claude": lineup.R5,
            "set a' 'b": lineup.R4.format(n=2),
        }
        for raw, refusal in cases.items():
            with self.subTest(raw=raw):
                code, out, err = self.expand(raw)
                self.assertEqual((code, out), (0, f"claude-multi: {refusal}\n"), err)
        self.assertEqual(sorted(p.name for p in (self.root / "cwd").iterdir()), [])
        code, out, err = self.expand("set implementer=opus55:xhigh")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("lineup gen 2: implementer "), out)
        self.assertIn(lineup.RELOAD_LINES[0], out)
        code, out, _ = self.expand("")
        self.assertTrue(out.startswith("claude-multi · profile balanced (pinned) · lineup gen 2"), out)


# ------------------------------------------------------------------ agent class moves


class AgentClassMoveTests(V4Case):
    """A session recorded with 200K-class agent selectors on a line the
    installed catalog and the session window now give the 1M class (its
    provider bound is at least the window) keeps its recorded selectors until
    resume: record-authority compiles see no moved selector, doctor reports
    the pending move as Attention, an unrelated live change neither reports
    a fence gap nor rewrites the unchanged agents, and the resume moves them
    to ``[1m]`` with an explicit "agent class 200K → 1M" row. The line is
    chosen by shape (``test_agent_context.codex_line``), outside the lead set."""

    def setUp(self) -> None:
        super().setUp()
        import test_agent_context as ac

        self.before_root, self.key = ac.write_bounded_assets(self.root / "assets-before", ac.codex_line, ac.BELOW)
        self.runtime = self.make_runtime(asset_root=self.before_root)
        document = ac.narrowed_balanced(self.runtime.catalog)
        self.runtime.profiles.new(document)
        with _fixed_id():
            self.record = self.launch_fresh(cli.LaunchTarget("profile", document, document["name"], False, "Narrow"))
        self.mid, self.rid = self.record["managed_id"], self.record["runtime_session_id"]
        self.slots = sorted(rid for rid, b in self.record["applied"]["agents"].items() if b["key"] == self.key)
        self.assertTrue(self.slots and all(
            not self.record["applied"]["agents"][rid]["selector"].endswith("[1m]") for rid in self.slots))
        self.assertEqual(sessions.recorded_window(self.record), 800_000)
        self.runtime = self.make_runtime(asset_root=FIXTURE_ROOT)  # the line's bound is now above the window

    def request(self, text: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["lineup", "--session", self.rid, text], runtime=self.runtime, output_stream=out)
        return code, out.getvalue(), err.getvalue()

    def test_record_authority_keeps_the_recorded_class_and_notices_the_move(self) -> None:
        from claude_multi.cli import doctor

        parts = self.runtime.converge_parts()
        ep = transition.expected_plan(
            self.store.load(self.mid), docs=parts.docs, prompt_bodies=parts.prompt_bodies,
            state_root=self.store.root, hook_command=parts.hook_command,
            token_helper_command=parts.token_helper_command, live=self.live(self.mid),
            environ=parts.environ, managed_root=parts.managed_root)
        self.assertIsNotNone(ep.plan, ep.reason)
        self.assertEqual(sorted(row[0] for row in ep.agent_class_kept), sorted(profile.label(r) for r in self.slots))
        for _label, kept, installed in ep.agent_class_kept:
            self.assertEqual(kept + "[1m]", installed)
        problems, attention, _info = doctor._check_scope_integrity(self.runtime, self.store.load(self.mid))
        self.assertEqual(problems, [])
        notice = next(line for line in attention if "agent context class changes at the next resume" in line)
        self.assertIn(f"{profile.label(self.slots[0])} 200K → 1M", notice)
        self.assertIn(f"claude-multi -r {self.mid} after it exits", notice)
        self.assertIn("matches", self.converge_report()[-1])

    def converge_report(self):
        return transition.converge(self.store.root, self.store, self.mid, runtime_parts=self.runtime.converge_parts())

    def test_an_unrelated_live_change_never_rewrites_or_gaps_unchanged_agents(self) -> None:
        live = self.live(self.mid)
        kept = {rid: (live / ".claude" / "agents" / f"{rid}.md").read_bytes() for rid in self.slots}
        other = next(rid for rid in self.record["applied"]["agents"] if rid not in self.slots)
        code, out, err = self.request(f"unset {other}")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("relaunch needed", out)
        self.assertNotIn("fence", out)
        after = self.store.load(self.mid)
        self.assertEqual(after["lineup_generation"], 2, out)
        for rid in self.slots:
            self.assertEqual((live / ".claude" / "agents" / f"{rid}.md").read_bytes(), kept[rid])
            self.assertEqual(after["applied"]["agents"][rid]["selector"],
                             self.record["applied"]["agents"][rid]["selector"])
        self.assertNotIn(other, after["applied"]["agents"])
        self.assertIn("matches", self.converge_report()[-1])

    def test_a_new_slot_on_the_line_takes_the_1m_class_live(self) -> None:
        free = next(rid for rid in lineup.catalog_mod.AGENT_ROLE_IDS if rid not in self.record["applied"]["agents"])
        effort = self.runtime.lineup_catalog().lines[self.key]["default_effort"]
        code, out, err = self.request(f"set {free}={self.key}:{effort}")
        self.assertEqual(code, 0, out + err)
        # The launch fence admits the line's [1m] selector, and its bound is
        # at least the session window: a live change, never a gap.
        self.assertNotIn("relaunch needed", out)
        selector = self.runtime.lineup_catalog().lines[self.key]["efforts"][effort]["selector"]
        self.assertTrue(selector.endswith("[1m]"))
        after = self.store.load(self.mid)
        self.assertEqual(after["applied"]["agents"][free]["selector"], selector)
        text = (self.live(self.mid) / ".claude" / "agents" / f"{free}.md").read_text()
        self.assertIn(f"model: {selector}\n", text)
        for rid in self.slots:
            self.assertEqual(after["applied"]["agents"][rid]["selector"],
                             self.record["applied"]["agents"][rid]["selector"])

    def test_resume_shows_the_agent_class_move_and_applies_it(self) -> None:
        from claude_multi.cli import doctor

        prepared = self.prepare_resume(self.mid)
        rows = [line for line in prepared.diff if "agent class" in line]
        self.assertEqual(len(rows), len(self.slots), prepared.diff)
        self.assertTrue(all(line.rstrip().endswith("200K → 1M") for line in rows))
        self.runtime.perform(prepared)
        after = self.store.load(self.mid)
        for rid in self.slots:
            self.assertEqual(after["applied"]["agents"][rid]["selector"],
                             self.record["applied"]["agents"][rid]["selector"] + "[1m]")
            text = (self.live(self.mid) / ".claude" / "agents" / f"{rid}.md").read_text()
            self.assertIn(f"model: {after['applied']['agents'][rid]['selector']}\n", text)
        problems, attention, _info = doctor._check_scope_integrity(self.runtime, after)
        self.assertEqual(problems, [])
        self.assertFalse([line for line in attention if "agent context class" in line], attention)


# ------------------------------------------------------------------ /cm profiles, review, fallback


def _golden(case: unittest.TestCase, name: str, out: str) -> None:
    assertGolden(case, LINEUP_GOLDENS / name, out.encode("utf-8"))


def _last(out: str) -> str:
    return out.rstrip("\n").splitlines()[-1]


class ProfilesAndNextStepTests(LineupCase):
    """The profiles block, /cm profiles and exactly one next step."""

    def test_cm_bare_profiles_current_marker_golden(self) -> None:
        with _fixed_clock():
            code, out, err = self.request("")
        self.assertEqual((code, err), (0, ""))
        _golden(self, "show.txt", out)
        lines = out.splitlines()
        block = lines[lines.index(lineup.PROFILES_HEAD):lines.index(lineup.PROFILES_SWITCH) + 1]
        self.assertEqual([line for line in block if line.startswith("*")], [block[1]])
        self.assertTrue(block[1].startswith("* balanced "))
        # one line per available profile, the switch line, then the help
        self.assertEqual(len(block), len(self.runtime.profiles.names()) + 2)
        self.assertEqual(lines[lines.index(lineup.PROFILES_SWITCH) + 1], lineup.HELP_LINE)

    def test_cm_profiles_golden(self) -> None:
        before = self.record_bytes(self.mid)
        files = self.scope_files()
        code, out, err = self.request("profiles")
        self.assertEqual((code, err), (0, ""))
        _golden(self, "profiles.txt", out)
        self.assertEqual(out.splitlines()[0], lineup.PROFILES_HEAD)
        self.assertEqual(_last(out), lineup.PROFILES_SWITCH)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), (before, files))
        self.assertFalse(lineup.parse_request(["profiles"]).writes)

    def test_cm_profile_agents_only_next_step(self) -> None:
        with _fixed_clock():
            code, out, err = self.request("profile quality")
        _golden(self, "profile-agents-only.txt", out)
        self.assertEqual(_last(out), lineup.NEXT_RELOAD)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith(("next:", "Next:", "lead:"))), 1)

    def test_cm_profile_lead_inside_set_next_step(self) -> None:
        _lead_profile(self, "lead-in", {"model": "opus5", "effort": "xhigh"})
        with _fixed_clock():
            code, out, err = self.request("profile lead-in")
        _golden(self, "profile-lead-in-set.txt", out)
        self.assertTrue(_last(out).startswith("lead: Opus 5"), out)
        self.assertIn("now with /model", _last(out))
        self.assertEqual(self.store.load(self.mid)["lead_target"]["key"], "opus5")
        # Agents change AND the lead must switch → one combined step.
        _lead_profile(self, "lead-in-agents", {"model": "opus5", "effort": "xhigh"},
                      agents={"cm-implementer": {"model": "opus55", "effort": "xhigh"}})
        code, out, err = self.request("profile lead-in-agents")
        self.assertEqual(_last(out), lineup.NEXT_RELOAD_AND_MODEL)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith(("next:", "Next:", "lead:"))), 1)

    def test_cm_profile_lead_outside_set_next_step(self) -> None:
        _lead_profile(self, "lead-out", {"model": "grok46", "effort": "high"})
        with _fixed_clock():
            code, out, err = self.request("profile lead-out")
        _golden(self, "profile-lead-outside-set.txt", out)
        self.assertEqual(
            _last(out), lineup.NEXT_LEAD_RESUME.format(label="Grok 4.6 · high", mid=self.mid)
        )
        self.assertIn("pending", self.store.load(self.mid))

    def test_pin_follow_and_unchanged_next_steps(self) -> None:
        code, out, _ = self.request("pin")
        self.assertEqual(_last(out), lineup.NEXT_RECORDED)
        code, out, _ = self.request("pin")
        self.assertEqual(_last(out), lineup.NEXT_NONE)
        code, out, _ = self.request("follow")
        self.assertEqual(_last(out), lineup.NEXT_RECORDED)  # never "no change" after an authority change


class ReviewTests(LineupCase):
    """/cm review is lineup-routed and report-only."""

    def test_cm_review_skill_routing_table_golden(self) -> None:
        code, out, err = self.request("review")
        self.assertEqual((code, err), (0, ""))
        _golden(self, "review-normal.txt", out)
        # The printed rows are exactly the live lineup.md's routing table.
        table = [line[2:] for line in out.splitlines() if line.startswith("  |")]
        lineup_md = (self.live(self.mid) / "lineup.md").read_text()
        section = lineup_md.split("## Review routing\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(table, section)
        self.assertIn("/cm review", scope.SKILL_MD_BYTES.decode())
        self.assertIn("spawn cm-reviewer", out)

    def test_cm_review_high_stakes_is_report_only_no_fix(self) -> None:
        before = self.record_bytes(self.mid)
        files = self.scope_files()
        code, out, err = self.request("review high-stakes HEAD~3..HEAD")
        self.assertEqual((code, err), (0, ""))
        _golden(self, "review-high-stakes.txt", out)
        self.assertIn("spawn cm-reviewer and cm-reviewer-strong in parallel (both reviewer grades)", out)
        self.assertIn("arbitrate", out)
        self.assertIn("report-only: no edits, no fix pass, no commits", out)
        self.assertIn("git diff HEAD~3..HEAD", out)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), (before, files))
        self.assertFalse(lineup.parse_request(["review", "high-stakes"]).writes)
        for words in (["review", "-p"], ["review", "a;b"], ["review", "x", "y"], ["review", "high-stakes", "--x"]):
            with self.subTest(words=words), self.assertRaises(lineup.LineupRefusal):
                lineup.parse_request(words)
        # No reviewer bound: said so, never a generic agent.
        self.request("unset reviewer-strong")
        code, out, _ = self.request("unset reviewer")
        self.assertNotIn("claude-multi:", out)
        code, out, _ = self.request("review")
        self.assertIn("No cm reviewer is bound", out)


class FallbackTests(LineupCase):
    """/cm fallback PROVIDER."""

    def _state(self):
        return self.record_bytes(self.mid), self.scope_files()

    def test_fallback_uses_explicit_provider_profile(self) -> None:
        seed_before = copy.deepcopy(self.runtime.profiles.load("claude"))
        with _fixed_clock():
            code, out, err = self.request("fallback anthropic")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Fallback target: anthropic · profile claude")
        self.assertEqual(self.runtime.profiles.load("claude"), seed_before)  # the recipe is never modified
        record = self.store.load(self.mid)
        self.assertEqual(record["applied"]["primary_provider"], "anthropic")
        self.assertEqual(record["profile"], "balanced")
        # None: the exact remedy; several: named, never chosen silently.
        before = self._state()
        code, out, _ = self.request("fallback kimi")
        _golden(self, "fallback-none.txt", out)
        self.assertEqual(out, "claude-multi: " + lineup.FALLBACK_NONE.format(provider="kimi") + "\n")
        _user_copy(self, "balanced", primary_provider="kimi")
        _lead_profile(self, "kimi-two", {"model": "opus55", "effort": "ultracode"})
        _user_copy(self, "kimi-two", primary_provider="kimi")
        code, out, _ = self.request("fallback kimi")
        self.assertIn('several profiles declare primary_provider "kimi": balanced, kimi-two', out)
        self.assertEqual(self._state()[1], before[1])

    def test_fallback_preserves_unbound_slots_and_grades(self) -> None:
        self.request("unset implementer-strong")
        with _fixed_clock():
            code, out, err = self.request("fallback anthropic")
        record = self.store.load(self.mid)
        agents = record["applied"]["agents"]
        recipe = self.runtime.profiles.load("claude")["agents"]
        self.assertNotIn("cm-implementer-strong", agents)  # unbound stays unbound
        self.assertIn("cm-implementer-strong", recipe)
        # exact role ids, grade included (strong never becomes normal)
        for rid in ("cm-reviewer", "cm-reviewer-strong", "cm-analyst", "cm-analyst-strong"):
            self.assertEqual((agents[rid]["key"], agents[rid]["effort"]),
                             (recipe[rid]["model"], recipe[rid]["effort"]), rid)
        self.assertNotIn("cm-designer", agents)

    def test_missing_fallback_slot_refuses_without_write(self) -> None:
        self.request("set designer=sol")
        before = self._state()
        code, out, _ = self.request("fallback anthropic")
        self.assertEqual(
            out,
            "claude-multi: " + lineup.FALLBACK_MISSING.format(
                provider="anthropic", name="claude", roles="cm-designer") + "\n",
        )
        self.assertEqual(self._state(), before)

    def test_t2_fallback_is_relaunch_class(self) -> None:
        import test_operator_agents as agents_fixtures  # lazy: it imports this module

        class Case(agents_fixtures.OperatorState, LineupCase):
            launch_profile = None

            def runTest(self) -> None:  # pragma: no cover - helper only
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state.atomic_write(case.root / "secrets" / "claude.env", b"ACME_API_KEY=acme-dummy\n")
        patcher = mock.patch.object(case.runtime, "contract_identity", return_value=agents_fixtures.CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)
        case.install(case.runtime, case.acme_files(), keys=(agents_fixtures.AGENT_KEY,))
        layer = case.runtime.operator_snapshot().layer
        agents_fixtures.record_passes(case.runtime.gateway_environ(), agents_fixtures.AGENT_KEY,
                                      layer.lines[agents_fixtures.AGENT_KEY].definition_digest)
        with _fixed_id():
            record = case.launch_fresh(case.profile_target("balanced"))
        case.mid, case.rid = record["managed_id"], record["runtime_session_id"]
        recipe = case.runtime.profiles.load("claude")
        recipe.pop("seed", None)
        recipe["agents"]["cm-reviewer"] = {"model": agents_fixtures.AGENT_KEY, "effort": "high"}
        case.runtime.profiles.save(recipe, target="claude")
        code, out, err = case.request("fallback anthropic")
        self.assertIn("Effect: RELAUNCH", out)
        self.assertIn("an operator agent binding is relaunch-class", out)
        self.assertIn(lineup.FALLBACK_PENDING[0], out)
        pending = case.store.load(case.mid)["pending"]
        self.assertEqual(pending["document"]["agents"]["cm-reviewer"]["model"], agents_fixtures.AGENT_KEY)
        self.assertNotEqual(case.store.load(case.mid)["applied"]["agents"]["cm-reviewer"]["key"],
                            agents_fixtures.AGENT_KEY)

    def test_existing_agents_are_not_rebound(self) -> None:
        live = self.live(self.mid)
        launch_files = {name: (live / name).read_bytes() for name in ("settings.json", "lead-set.json")}
        before = self.store.load(self.mid)
        code, out, _ = self.request("fallback anthropic")
        self.assertIn(lineup.FALLBACK_EXISTING, out)
        after = self.store.load(self.mid)
        # Running agents are never touched: the launch-time fence files stay,
        # the running lead stays, and only the fresh-spawn definitions move.
        self.assertEqual({name: (live / name).read_bytes() for name in launch_files}, launch_files)
        self.assertEqual(after["applied"]["lead"], before["applied"]["lead"])
        self.assertEqual(after["lineup_generation"], before["lineup_generation"] + 1)
        log = lineup_log.lineup_log_path(self.store.root, self.mid).read_text().splitlines()
        self.assertEqual([json_line["event"] for json_line in map(strict_json.loads, log)][-1], "apply")

    def test_fallback_updates_lead_providers_preserves_policy_and_pins(self) -> None:
        _user_copy(self, "claude", lead_providers=["anthropic"], workflows="off",
                   native_agents={"explore": "native", "plan": "off", "general_purpose": "on"})
        before = self.store.load(self.mid)
        with _fixed_clock():
            code, out, _ = self.request("fallback anthropic")
        self.assertIn("Effect: RELAUNCH", out)
        self.assertIn("  lead providers: any → anthropic", out)
        record = self.store.load(self.mid)
        document = record["pending"]["document"]
        self.assertEqual(document["lead_providers"], ["anthropic"])
        self.assertEqual(document["primary_provider"], "anthropic")
        # native agents, workflows and settings overrides are the session's own
        self.assertEqual(document["native_agents"], dict(before["applied"]["native_agents"]))
        self.assertEqual(document["workflows"], before["applied"]["workflows"])
        self.assertNotIn("settings_overrides", document)
        # pinned: follow off at the next resume, the profile label kept
        self.assertFalse(record["pending"]["follow"])
        self.assertEqual(record["pending"]["profile"], "balanced")
        self.assertIn("pinned when the change applies", out)

    def test_fallback_preview_is_read_only(self) -> None:
        before = self._state()
        log = lineup_log.lineup_log_path(self.store.root, self.mid)
        log_before = log.read_bytes() if log.exists() else None
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch.object(sessions.SessionStore, "lifecycle_lock",
                               side_effect=AssertionError("a preview takes no lock")), \
                mock.patch.object(sessions.SessionStore, "save",
                                  side_effect=AssertionError("a preview writes nothing")), \
                _fixed_clock():
            code, out, err = self.lineup("--preview", "fallback", "anthropic", skill=False, interactive=False)
        self.assertEqual((code, err), (0, ""))
        _golden(self, "fallback-preview.txt", out)
        self.assertEqual(_last(out), lineup.FALLBACK_PREVIEW_ONLY.format(provider="anthropic"))
        self.assertEqual(self._state(), before)
        self.assertEqual(log.read_bytes() if log.exists() else None, log_before)
        code, out, err = self.lineup("--preview", "profile", "quality", skill=False, interactive=False)
        self.assertEqual(code, 2)
        self.assertIn(lineup.R26, err)

    def test_preview_forms_including_the_r26_remedy_parse(self) -> None:
        """The form R26 recommends (the trailing
        ``--preview``) parses to the same read-only preview as the leading one."""

        env = {"CLAUDE_CODE_SESSION_ID": self.rid}
        for argv in (["--session", self.rid, "--preview", "fallback", "openai"],
                     ["--preview", "--session", self.rid, "fallback", "openai"],
                     ["--session", self.rid, "fallback", "openai", "--preview"],
                     ["--session", self.rid, "fallback openai --preview"],
                     ["fallback", "openai", "--preview"]):
            with self.subTest(argv=argv):
                args = lineup.parse_cli(argv, env)
                self.assertEqual((args.request.verb, args.request.provider, args.preview, args.writes),
                                 ("fallback", "openai", True, False))
        self.assertIn("lineup fallback <provider> --preview", lineup.R26)
        with self.assertRaises(lineup.LineupRefusal):
            lineup.parse_cli(["--session", self.rid, "profile", "quality", "--preview"], env)

    def test_cm_quota_is_a_read_only_wrapper_of_the_quota_formatter(self) -> None:
        """``/cm quota`` uses the Runtime collector (one cached
        read) and ``quota.command_lines`` on the session surface; no lock,
        no write, skill mode exits 0."""

        from datetime import datetime, timezone

        from claude_multi import quota as quota_mod

        self.assertEqual(lineup.parse_request(["quota"]).verb, "quota")
        self.assertFalse(lineup.parse_request(["quota"]).writes)
        with self.assertRaises(lineup.LineupRefusal):
            lineup.parse_request(["quota", "now"])
        self.assertIn("/cm quota", lineup.HELP_LINE)
        self.assertIn("quota", lineup.R7)
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        pool = quota_mod.PoolStatus("ok", read_at=now, credentials=())
        before = self._state()
        calls = []

        def pool_status():
            calls.append(1)
            return pool

        expected = quota_mod.command_lines(
            pool, provider_by_pool=self.runtime.pool_providers(), login_commands={"claude": "claude-multi providers sign-in anthropic",
                                                                                 "codex": "claude-multi providers sign-in openai"},
            restart_hint="x", now=now, surface="session")[1]
        with mock.patch.object(sessions.SessionStore, "lifecycle_lock",
                               side_effect=AssertionError("quota takes no lock")), \
                mock.patch.object(sessions.SessionStore, "save",
                                  side_effect=AssertionError("quota writes nothing")), \
                mock.patch.object(self.runtime, "pool_status", pool_status), \
                mock.patch.object(quota_mod, "_now", return_value=now):
            code, out, err = self.request("quota")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(calls, [1])
        self.assertEqual(out, "".join(line + "\n" for line in expected))
        self.assertEqual(self._state(), before)
        # Outside skill mode a non-ok pool is the quota command's exit 1.
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = self.rid
        with mock.patch.object(self.runtime, "pool_status", return_value=quota_mod.PoolStatus("down")):
            code, out, err = self.lineup("quota", skill=False, interactive=False)
        self.assertEqual((code, out), (1, "quota: the local gateway is down\nQuota condition: unavailable — no reading can be made here now\n"))

    def test_fallback_three_outcomes_and_follow_remedy(self) -> None:
        with _fixed_clock():
            code, live_out, _ = self.request("fallback anthropic")
        _golden(self, "fallback-live.txt", live_out)
        self.assertIn(lineup.FALLBACK_LIVE[0].format(generation=2), live_out)
        self.assertIn(lineup.FALLBACK_LIVE[1], live_out)
        self.assertIn("/cm follow returns to it", live_out)
        # Again: nothing moves; the session is pinned already (no follow line).
        code, again, _ = self.request("fallback anthropic")
        self.assertIn(lineup.FALLBACK_UNCHANGED, again)
        self.assertNotIn("/cm follow returns", again)
        self.assertEqual(_last(again), lineup.NEXT_NONE)
        # RELAUNCH: a fresh session and a recipe that changes the lead providers.
        with _fixed_clock():
            code, out, _ = self.request("follow")
        _user_copy(self, "openai", lead_providers=["openai"])
        with _fixed_clock():
            code, pending_out, _ = self.request("fallback openai")
        for line in lineup.FALLBACK_PENDING:
            self.assertIn(line, pending_out)
        self.assertIn("pinned when the change applies", pending_out)
        self.assertTrue(_last(pending_out).startswith("lead: GPT-5.6 Sol"), pending_out)


class FallbackGoldenCaseTests(unittest.TestCase):
    def test_fallback_goldens_cover_the_three_outcomes(self) -> None:
        texts = {name: (LINEUP_GOLDENS / name).read_text() for name in
                 ("fallback-live.txt", "fallback-lead-switch.txt", "fallback-pending.txt")}
        self.assertIn("Effect: LIVE", texts["fallback-live.txt"])
        self.assertIn("Effect: LIVE", texts["fallback-lead-switch.txt"])
        self.assertEqual(_last(texts["fallback-lead-switch.txt"]), lineup.NEXT_RELOAD_AND_MODEL)
        self.assertIn("Effect: RELAUNCH", texts["fallback-pending.txt"])
