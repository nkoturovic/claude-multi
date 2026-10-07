"""The v2 launch plan — argv, process env, lead appendix, prompt path.

``compiler.compile_lineup_launch`` over the frozen fixture
(``tests/_catalog.FIXTURE_ROOT``); expected ids come from the loaded catalog
or test-local copies, never a shipped model id. Goldens
(``tests/goldens/v2/{managed,direct}/{argv-fresh,argv-resume,env}.json`` and
``lead-appendix.md``) are written by ``tests/bless.py``.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from claude_multi import (
    catalog,
    cli,
    compiler,
    composition,
    custom,
    lineup_files,
    profile,
    proxy,
    scope,
    settings,
    state,
    strict_json,
)
from claude_multi.compiler import CompilerError
from claude_multi.scope import ScopeError
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden

import bless

FIXED_SESSION = bless.FIXED_SESSION
RUNTIME_ID = "22222222-2222-4222-8222-222222222222"
STATE_ROOT = bless.V2_STATE_ROOT
SCOPE_DIR = bless.SCOPE_DIR
HOOK3 = bless.V2_HOOK_COMMAND
HELPER = bless.V2_TOKEN_HELPER
PROMPT_NAME = re.compile(r"^lead-prompt-[0-9a-f]{16}-" + re.escape(FIXED_SESSION) + r"\.md$")
FAMILY_KEYS = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
)


def _bundle():
    return catalog.load_catalog(FIXTURE_ROOT)


def _lcat(docs: dict[str, Any]) -> profile.LineupCatalog:
    return profile.LineupCatalog.from_docs(docs)


def _eff(lcat: profile.LineupCatalog, doc: dict[str, Any] | None = None, **fields: Any):
    eff = settings.effective(
        doc or {"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines
    )
    return dataclasses.replace(eff, **fields) if fields else eff


def _launch(
    lineup,
    eff,
    *,
    docs=None,
    bundle=None,
    action=None,
    **kw: Any,
) -> compiler.CompileResult:
    bundle = bundle or _bundle()
    arguments: dict[str, Any] = dict(
        docs=docs if docs is not None else bundle.docs,
        prompt_bodies=bundle.prompt_bodies,
        lineup=lineup,
        effective=eff,
        session_action=action or compiler.build_fresh(FIXED_SESSION),
        lineup_generation=1,
        state_root=STATE_ROOT,
        scope_dir=SCOPE_DIR,
        hook_command=HOOK3,
        token_helper_command=HELPER,
        passthrough=["--verbose"],
    )
    arguments.update(kw)
    return compiler.compile_lineup_launch(**arguments)


def _seed(name: str, *, eff=None, bundle=None):
    bundle = bundle or _bundle()
    lcat = _lcat(bundle.docs)
    eff = eff or _eff(lcat)
    return bundle, lcat, eff, profile.resolve(bundle.seed_profiles[name], lcat, effective=eff)


def _seed_launch(name: str = "balanced", **kw: Any):
    bundle, _lcat_, eff, lineup = _seed(name)
    return lineup, _launch(lineup, eff, bundle=bundle, **kw)


def _appendix(result: compiler.CompileResult) -> str:
    return bless.v2_appendix(result)


def _flag_values(argv: list[str]) -> dict[str, str]:
    return {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}


# ------------------------------------------------------------------ goldens


class V2LaunchGoldenTests(unittest.TestCase):
    def test_argv_env_and_appendix_goldens(self) -> None:
        for kind in bless.V2_SEEDS:
            for name, data in bless.v2_launch_files(kind).items():
                with self.subTest(kind=kind, name=name):
                    assertGolden(self, GOLDENS_ROOT / "v2" / kind / name, data)

    def test_golden_highlights(self) -> None:
        env = strict_json.loads((GOLDENS_ROOT / "v2" / "managed" / "env.json").read_bytes())
        self.assertFalse([key for key in env["set"] if key in FAMILY_KEYS])
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env["set"])
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env["unset"])
        for kind in bless.V2_SEEDS:
            data = b"".join(
                (GOLDENS_ROOT / "v2" / kind / name).read_bytes()
                for name in ("argv-fresh.json", "argv-resume.json", "env.json", "lead-appendix.md")
            )
            with self.subTest(kind=kind):
                self.assertNotIn(b"/nix/store", data)
                self.assertNotIn(b"lineup.log", data)


# --------------------------------------------------------------------- argv


class ArgvTests(unittest.TestCase):
    def test_one_argv_contract_for_managed_and_direct(self) -> None:
        flags = {}
        for name in ("balanced", "direct"):
            lineup, result = _seed_launch(name)
            argv = result.argv
            flags[name] = [token for token in argv if token.startswith("--")]
            values = _flag_values(argv)
            with self.subTest(seed=name):
                self.assertEqual(values["--session-id"], FIXED_SESSION)
                self.assertEqual(values["--settings"], str(SCOPE_DIR / "settings.json"))
                self.assertEqual(values["--model"], lineup.lead.binding.selector)
                self.assertEqual(values["--effort"], lineup.lead.session_effort)
                self.assertEqual(values["--add-dir"], str(SCOPE_DIR))
                self.assertEqual(values["--append-system-prompt-file"], str(result.lead_prompt_path))
                self.assertEqual(argv[-1], "--verbose")
                self.assertNotIn("--agents", argv)
                self.assertNotIn("--agent", argv)
                self.assertTrue(result.write_lead_prompt)
        self.assertEqual(flags["balanced"], flags["direct"])
        self.assertEqual(
            flags["balanced"],
            [
                "--session-id",
                "--name",
                "--settings",
                "--model",
                "--effort",
                "--add-dir",
                "--append-system-prompt-file",
                "--verbose",
            ],
        )

    def test_resume_pins_model_and_effort(self) -> None:
        lineup, fresh = _seed_launch()
        _, resume = _seed_launch(action=compiler.build_resume(FIXED_SESSION, RUNTIME_ID))
        self.assertEqual(resume.argv[:2], ["--resume", RUNTIME_ID])
        self.assertNotIn("--session-id", resume.argv)
        values = _flag_values(resume.argv)
        self.assertEqual(values["--model"], lineup.lead.binding.selector)
        self.assertEqual(values["--effort"], lineup.lead.session_effort)
        self.assertEqual(fresh.argv[2:], resume.argv[2:])
        self.assertEqual(fresh.lead_prompt_path, resume.lead_prompt_path)

    def test_session_effort_is_the_lead_session_effort(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        doc = copy.deepcopy(bundle.seed_profiles["balanced"])
        doc["workflows"] = "off"
        lineup = profile.resolve(doc, lcat, effective=eff)
        result = _launch(lineup, eff, bundle=bundle)
        self.assertEqual(_flag_values(result.argv)["--effort"], lineup.lead.session_effort)

    def test_name_forms(self) -> None:
        lineup, result = _seed_launch(session_cwd="/work/proj")
        self.assertEqual(_flag_values(result.argv)["--name"], "cm:balanced@proj")
        self.assertEqual(compiler.lineup_session_name(lineup, None), "cm:balanced")
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        key = next(
            key
            for key, entry in lcat.lines.items()
            if "lead" in entry["capabilities"] and entry["context"]["ordinary_profile"]
        )
        adhoc = profile.resolve(profile.ad_hoc_direct(key), lcat, effective=eff, ad_hoc=True)
        self.assertIsNone(adhoc.name)
        result = _launch(adhoc, eff, bundle=bundle, session_cwd="/work/proj")
        self.assertEqual(_flag_values(result.argv)["--name"], f"cm:direct:{key}@proj")
        self.assertEqual(result.composition_name, "")

    def test_passthrough_validation_is_shared(self) -> None:
        bundle, _l, eff, lineup = _seed("balanced")
        for token in ("--append-system-prompt-file", "--model", "--settings=x", "-r", "--agents"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(CompilerError, "launcher-owned"):
                    _launch(lineup, eff, bundle=bundle, passthrough=[token, "x"])
        result = _launch(
            lineup, eff, bundle=bundle, passthrough=["--add-dir", "/extra/one", "--add-dir=/extra/two"]
        )
        self.assertEqual(result.passthrough_add_dirs, ("/extra/one", "/extra/two"))
        with self.assertRaisesRegex(CompilerError, "requires a value"):
            _launch(lineup, eff, bundle=bundle, passthrough=["--add-dir"])


# ---------------------------------------------------------------------- env


class EnvTests(unittest.TestCase):
    def test_env_unset_and_sets(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        for name in ("balanced", "direct"):
            lineup, result = _seed_launch(name)
            with self.subTest(seed=name):
                secrets = sorted(scope.line_view(lcat, _eff(lcat)).secret_env_names)
                self.assertTrue(secrets)
                expected = [
                    *compiler.V2_ENV_UNSET,
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",  # the fixture large class has no scalar
                    *compiler.POLICY_DEFEATING_ENV_KEYS,
                    *compiler.GATEWAY_SECRET_ENV_KEYS,
                    *secrets,
                ]
                self.assertEqual(list(result.env_unset), expected)
                self.assertEqual(len(result.env_unset), len(set(result.env_unset)))
                self.assertTrue(set(catalog.CREDENTIAL_ENV_KEYS) <= set(result.env_unset))
                for key in (*FAMILY_KEYS, "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"):
                    self.assertIn(key, result.env_unset)
                    self.assertNotIn(key, result.env_set)
                self.assertTrue(set(catalog.CREDENTIAL_ENV_KEYS).isdisjoint(result.env_set))
                self.assertEqual(result.env_set["ANTHROPIC_BASE_URL"], bundle.docs["gateway"]["gateway"]["base_url"])
                self.assertEqual(result.env_set["CLAUDE_MULTI_MANAGED_ID"], FIXED_SESSION)
                self.assertEqual(result.env_set["CLAUDE_MULTI_SESSION_ID"], FIXED_SESSION)
                self.assertEqual(result.env_set["CLAUDE_MULTI_LAUNCH_EPOCH"], "0")
                self.assertEqual(result.env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
                window = int(result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])
                self.assertEqual(
                    window, strict_json.loads(result.scope_plan.other_files["lead-set.json"])["context"]["window"]
                )
                self.assertTrue(set(compiler.POLICY_DEFEATING_ENV_KEYS).isdisjoint(result.env_set))

    def test_scalar_exported_only_below_1m(self) -> None:
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lines = docs["models-v2"]["models"]
        # A test-local sub-1M lead class with an explicit scalar.
        entry = lines["qwen-flash-next"]
        entry["context"]["scalar_tokens"] = 300000
        lcat2 = _lcat(docs)
        eff = _eff(lcat2)
        lineup = profile.resolve(profile.ad_hoc_direct("qwen-flash-next"), lcat2, effective=eff, ad_hoc=True)
        result = _launch(lineup, eff, docs=docs, bundle=bundle)
        self.assertEqual(result.env_set["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "300000")
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", result.env_unset)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=300000", result.lead_prompt)

    def test_compaction_override_70(self) -> None:
        # A test-local profile override of 70.
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        doc = copy.deepcopy(bundle.seed_profiles["balanced"])
        doc["settings_overrides"] = {"compaction_percent": 70}
        lineup = profile.resolve(doc, lcat, effective=eff)
        result = _launch(lineup, eff, bundle=bundle)
        window = int(result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])
        trigger = compiler.reactive_trigger(window, 70)
        self.assertEqual(result.env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70")
        appendix = _appendix(result)
        self.assertIn(f"deterministic reactive trigger {trigger} (70% of the prompt budget)", appendix)
        context = strict_json.loads(result.scope_plan.other_files["lead-set.json"])["context"]
        self.assertEqual((context["percent"], context["trigger"]), (70, trigger))
        # The Settings-level value applies without an override; the
        # override still wins over a different Settings value.
        eff80 = _eff(lcat, compaction_percent=80)
        plain = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff80)
        self.assertEqual(_launch(plain, eff80, bundle=bundle).env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "80")
        self.assertEqual(_launch(lineup, eff80, bundle=bundle).env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70")

    def test_reactive_trigger_matches_the_2x_formula_at_90(self) -> None:
        for window in (200_000, 258_400, 320_032, 500_000, 800_000, 983_616):
            with self.subTest(window=window):
                self.assertEqual(
                    compiler.reactive_trigger(window, 90), composition.auto_compact_trigger(window)
                )

    def test_workflow_default_lives_only_in_flag_settings(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        key, entry = next(
            (key, entry)
            for key, entry in lcat.lines.items()
            if "agents" in entry["capabilities"] and isinstance(entry["efforts"], dict)
        )
        effort = next(iter(entry["efforts"]))
        eff = _eff(lcat, workflow_default_binding={"model": key, "effort": effort})
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)
        result = _launch(lineup, eff, bundle=bundle)
        selector = entry["efforts"][effort]["selector"]
        self.assertEqual(result.scope_plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], selector)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", result.scope_plan.settings["env"])
        for key_ in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
            self.assertIn(key_, result.env_unset)
            self.assertNotIn(key_, result.env_set)
        # The reach pinned by the placement probe (iii);
        # balanced denies general-purpose, so only workflow agents are named.
        self.assertEqual(lineup.native_agents["general_purpose"], "off")
        self.assertIn(
            f"- Workflow agents without a cm-* agentType run on `{selector}` "
            "(Settings workflow_default_binding); native Explore and Plan are not "
            "affected.",
            _appendix(result),
        )
        self.assertIn(
            compiler.workflow_default_line(selector, lineup.native_agents),
            result.scope_plan.other_files["lineup.md"].decode("utf-8"),
        )

    def test_workflow_default_line_names_general_purpose_when_on(self) -> None:
        on = compiler.workflow_default_line(
            "sel-x", {"explore": "native", "general_purpose": "on", "plan": "native"}
        )
        self.assertEqual(
            on,
            "- Workflow agents without a cm-* agentType and native general-purpose "
            "agents run on `sel-x` (Settings workflow_default_binding); native "
            "Explore and Plan are not affected.",
        )
        off = compiler.workflow_default_line(
            "sel-x", {"explore": "replace", "general_purpose": "off", "plan": "native"}
        )
        self.assertNotIn("general-purpose", off)

    def test_reserved_lead_env_key_fails_closed(self) -> None:
        bundle, _l, eff, lineup = _seed("balanced")
        for key in ("CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_API_KEY"):
            bad = dataclasses.replace(
                lineup, lead=dataclasses.replace(lineup.lead, env={key: "x"})
            )
            with self.subTest(key=key):
                with self.assertRaisesRegex(CompilerError, "compiler-owned and reserved"):
                    _launch(bad, eff, bundle=bundle)
        allowed = dataclasses.replace(
            lineup, lead=dataclasses.replace(lineup.lead, env={"SOME_LEAD_TUNING": "1"})
        )
        self.assertEqual(_launch(allowed, eff, bundle=bundle).env_set["SOME_LEAD_TUNING"], "1")

    def test_fast_mode_reserved_env_cannot_be_overridden(self) -> None:
        """Every managed launch env disables Claude
        Code fast mode (no penguin-mode prefetch with the gateway key, no
        cached fast speed); a lead env can never switch it back; the rejected
        alternatives are never compiled. The real-client proof is FM in
        ``test_managed_skill_policy``."""

        key = "CLAUDE_CODE_DISABLE_FAST_MODE"
        self.assertIn(key, catalog.RESERVED_LEAD_ENV_KEYS)
        for seed in ("balanced", "direct"):
            for no_subagents in (False, True):
                _lineup, result = _seed_launch(seed, no_subagents=no_subagents)
                with self.subTest(seed=seed, no_subagents=no_subagents):
                    self.assertEqual(result.env_set[key], "1")
                    self.assertNotIn(key, result.env_unset)
                    # The flag settings state it too (user,
                    # project and local settings env override the process env).
                    self.assertEqual(result.scope_plan.settings["env"][key], "1")
                    for rejected in ("CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK",
                                     "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS",
                                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "ANTHROPIC_AUTH_TOKEN"):
                        self.assertNotIn(rejected, result.env_set)
                        self.assertNotIn(rejected, result.scope_plan.settings["env"])
        bundle, _l, eff, lineup = _seed("balanced")
        for value in ("0", "1", "false"):
            bad = dataclasses.replace(lineup, lead=dataclasses.replace(lineup.lead, env={key: value}))
            with self.subTest(value=value), self.assertRaisesRegex(CompilerError, "compiler-owned and reserved"):
                _launch(bad, eff, bundle=bundle)
        # The catalog refuses it in a model line's lead env as well.
        raw = catalog.load_raw(FIXTURE_ROOT)
        entry = next(line for line in raw["docs"]["models"]["models"].values() if line.get("lead") is not None)
        entry["lead"]["env"][key] = "0"
        errors = catalog.validate_catalog(raw)
        self.assertTrue(any(key in error and "compiler-owned and reserved" in error for error in errors), errors)

    def test_no_subagents_belt(self) -> None:
        lineup, result = _seed_launch(no_subagents=True)
        self.assertEqual(result.env_set["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"], "1")
        self.assertIn("CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS", result.env_unset)
        self.assertEqual(result.scope_plan.settings["permissions"]["deny"][0], "Agent")
        self.assertIs(result.scope_plan.settings["disableWorkflows"], True)
        appendix = _appendix(result)
        self.assertIn("- Workflows: off (the Workflow tool is disabled).", appendix)
        self.assertIn(
            "- Delegation: disabled for this session (`--no-subagents`: the Agent tool is "
            "denied and workflows are off).",
            appendix,
        )
        _, plain = _seed_launch()
        self.assertNotIn("Delegation:", _appendix(plain))

    def test_native_policy_env_applied(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        doc = copy.deepcopy(bundle.seed_profiles["direct"])
        doc["native_agents"] = {"explore": "off", "general_purpose": "on", "plan": "off"}
        lineup = profile.resolve(doc, lcat, effective=eff)
        result = _launch(lineup, eff, bundle=bundle)
        self.assertEqual(result.policy_env, {"CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"})
        self.assertEqual(result.env_set["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"], "1")


class ProviderSecretUnsetTests(unittest.TestCase):
    """Provider secrets never reach a session."""

    def test_v2_env_unset_rules(self) -> None:
        names = compiler.v2_env_unset(
            scalar=None,
            secret_env_names=["ZED_SECRET", "ALPHA_SECRET", "ZED_SECRET", "CLAUDE_CODE_MESSAGING_TOKEN"],
            launch_environ={
                "FOO_API_KEY": "x",
                "ANTHROPIC_API_KEY": "x",
                "EXAMPLE_MCP_API_KEY": "x",
                "CLAUDE_CODE_MESSAGING_TOKEN": "x",
                "CLAUDE_MULTI_SECRET_ENV": "/p",
                "API_KEY_HOLDER": "x",
                "BAR_API_KEY": "x",
                "MANAGEMENT_PASSWORD": "hostile-upper",
                "management_password": "hostile-lower",
                "Management_Password": "hostile-mixed",
            },
        )
        head = len(compiler.V2_ENV_UNSET) + 1 + len(compiler.POLICY_DEFEATING_ENV_KEYS)
        self.assertEqual(names[head:head + 3], (
            "MANAGEMENT_PASSWORD", "Management_Password", "management_password",
        ))
        head += len(compiler.GATEWAY_SECRET_ENV_KEYS) + 2
        self.assertEqual(list(names[head:]),
                         ["ALPHA_SECRET", "ZED_SECRET", "BAR_API_KEY", "EXAMPLE_MCP_API_KEY", "FOO_API_KEY"])
        self.assertEqual(names.count("MANAGEMENT_PASSWORD"), 1)
        self.assertEqual(names.count("ANTHROPIC_API_KEY"), 1)
        for kept in ("CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_MULTI_SECRET_ENV"):
            self.assertNotIn(kept, names)
        # Product names only; an MCP server's key is unset like any
        # other *_API_KEY (no operator allowlist entry).
        self.assertEqual(compiler.ENV_UNSET_KEEP, frozenset({"CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_MULTI_SECRET_ENV"}))
        self.assertNotIn("API_KEY_HOLDER", names)
        scalar = compiler.v2_env_unset(scalar=300000, secret_env_names=())
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", scalar)

    def test_gateway_secrets_removed_for_managed_and_direct_without_scope_changes(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        ordinary = {"PATH": "/bin", "HTTPS_PROXY": "http://proxy.invalid:8080", "NO_PROXY": "example.invalid"}
        secrets = {
            "MANAGEMENT_PASSWORD": "hostile-upper-value",
            "management_password": "hostile-lower-value",
            "Management_Password": "hostile-mixed-value",
        }
        for seed in ("balanced", "direct"):
            with self.subTest(seed=seed):
                lineup = profile.resolve(bundle.seed_profiles[seed], lcat, effective=eff)
                clean = _launch(lineup, eff, bundle=bundle, launch_environ=ordinary)
                hostile = _launch(lineup, eff, bundle=bundle, launch_environ=ordinary | secrets)
                # The strip list alone changes. No settings, argv, prompt,
                # scope hash or NO_PROXY bytes depend on the secret values.
                self.assertEqual(dataclasses.replace(hostile, env_unset=clean.env_unset), clean)
                self.assertEqual(hostile.env_set["NO_PROXY"], "127.0.0.1,localhost,::1,example.invalid")
                self.assertEqual(hostile.env_set["no_proxy"], hostile.env_set["NO_PROXY"])
                child = ordinary | secrets
                for name in hostile.env_unset:
                    child.pop(name, None)
                child.update(hostile.env_set)
                self.assertFalse(set(child) & set(secrets))
                for value in secrets.values():
                    self.assertNotIn(value, repr(hostile))

    def test_launch_env_api_keys_and_merged_custom_secret(self) -> None:
        bundle = _bundle()
        registry = {
            "version": 1,
            "providers": {
                "my-gw": {
                    "base_url": "https://llm.example.invalid/v1",
                    "auth_kind": "bearer",
                    "secret_env": "MY_GATEWAY_TOKEN",
                }
            },
            "models": {
                "my-model": {"provider": "my-gw", "wire_model": "my-wire", "context_tokens": 262144}
            },
        }
        docs = custom.merge_docs(bundle.docs, registry)
        lcat = _lcat(docs)
        eff = _eff(lcat)
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)
        result = _launch(
            lineup,
            eff,
            docs=docs,
            bundle=bundle,
            launch_environ={"OPENAI_API_KEY": "sk-x", "EXAMPLE_MCP_API_KEY": "m", "CLAUDE_MULTI_SECRET_ENV": "/p",
                            "PATH": "/bin"},
        )
        self.assertIn("MY_GATEWAY_TOKEN", result.env_unset)  # free-form custom name
        self.assertIn("OPENAI_API_KEY", result.env_unset)
        self.assertIn("EXAMPLE_MCP_API_KEY", result.env_unset)  # No operator keep entry
        self.assertNotIn("CLAUDE_MULTI_SECRET_ENV", result.env_unset)
        self.assertNotIn("PATH", result.env_unset)
        for provider in lcat.providers.values():
            ref = provider["transport"].get("auth", {}).get("secret_ref", "")
            if ref.startswith("env:"):
                self.assertIn(ref[4:], result.env_unset)

    def test_print_launch_shows_the_unsets(self) -> None:
        _, result = _seed_launch(launch_environ={"FOO_API_KEY": "secret-value-1"})
        # The printer reads the planned v4 record's profile/follow/
        # lineup_generation.
        record = {
            "version": 4,
            "profile": "balanced",
            "follow": True,
            "lineup_generation": 1,
            "managed_id": FIXED_SESSION,
            "runtime_session_id": FIXED_SESSION,
        }
        buffer = io.StringIO()
        cli._print_launch_plan(SimpleNamespace(result=result, record=record), buffer)
        text = buffer.getvalue()
        for key in result.env_unset:
            self.assertIn(f"  unset {key}\n", text)
        self.assertIn("  unset FOO_API_KEY\n", text)
        self.assertIn("  unset KIMI_CLAUDE_API_KEY\n", text)
        self.assertNotIn("secret-value-1", text)

    def test_gateway_render_is_unaffected_by_the_unsets(self) -> None:
        # The render resolves secret_ref from the secret file
        # (proxy.resolve_secret), never the process env, so a managed
        # session's env (secrets unset) renders byte-identically.
        root = Path(tempfile.mkdtemp(prefix="claude-multi-v2-unset-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        os.chmod(root, 0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        secrets = root / "secrets"
        state.ensure_private_dir(secrets)
        secret_file = secrets / "claude.env"
        state.atomic_write(secret_file, b"KIMI_CLAUDE_API_KEY=test-dummy-value-123\n")
        binary = root / "bin" / "cli-proxy-api"
        binary.parent.mkdir()
        binary.write_bytes(b"#!/bin/fake\n")
        binary.chmod(0o755)
        outer = {
            "HOME": str(home),
            "CLAUDE_MULTI_SECRET_ENV": str(secret_file),
            "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT),
            "CLAUDE_MULTI_PROXY_BIN": str(binary),
            "KIMI_CLAUDE_API_KEY": "env-value-never-used",
            "OPENAI_API_KEY": "sk-env",
        }
        _, result = _seed_launch(launch_environ=outer)
        self.assertNotIn("CLAUDE_MULTI_SECRET_ENV", result.env_unset)
        inner = {key: value for key, value in outer.items() if key not in result.env_unset}
        inner.update(result.env_set)
        self.assertNotIn("KIMI_CLAUDE_API_KEY", inner)
        self.assertNotIn("OPENAI_API_KEY", inner)
        config = proxy.config_dir(home) / "config.yaml"

        def models_get(_base, _token):
            yaml = config.read_text()
            return 200, set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))

        rendered = []
        for environ in (outer, inner):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(proxy.cmd_init([], environ=environ, models_get=models_get), 0)
            rendered.append(config.read_bytes())
        self.assertEqual(rendered[0], rendered[1])
        self.assertIn(b"test-dummy-value-123", rendered[1])
        self.assertNotIn(b"env-value-never-used", rendered[1])


# ------------------------------------------------------------ context guard


class AgentContextGuardTests(unittest.TestCase):
    def _docs(self):
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lines = docs["models-v2"]["models"]
        # A test-local agents-only line whose provider bound undercuts both
        # its client window and the large class's process window.
        key = next(
            key for key, entry in lines.items()
            if entry["capabilities"] == ["agents"] and isinstance(entry["efforts"], dict)
        )
        lines[key]["context"]["client_tokens"] = 1_000_000
        lines[key]["context"]["provider_tokens"] = 300_000
        lines[key]["context"]["scalar_tokens"] = None
        return bundle, docs, key

    def test_bound_capacity_risk_warns_with_the_actual_suffixless_class(self) -> None:
        bundle, docs, key = self._docs()
        lcat = _lcat(docs)
        eff = _eff(lcat)
        entry = lcat.lines[key]
        effort = next(iter(entry["efforts"]))
        rid = next(
            rid for rid in catalog.AGENT_ROLE_IDS
            if not lcat.roles[rid]["requires"]
            and (entry["roles"] == "all" or rid in entry["roles"])
        )
        base = copy.deepcopy(bundle.seed_profiles["balanced"])
        unbound = profile.resolve(base, lcat, effective=eff)
        self.assertEqual(compiler.agent_context_gaps(unbound, 800_000), ())
        _launch(unbound, eff, docs=docs, bundle=bundle)  # unbound line: never checked
        doc = copy.deepcopy(base)
        doc["agents"][rid] = {"model": key, "effort": effort}
        lineup = profile.resolve(doc, lcat, effective=eff)
        self.assertEqual(lineup.agents[rid].binding.client_context_tokens, 200_000)
        self.assertEqual(compiler.agent_context_gaps(lineup, 800_000), ())
        _launch(lineup, eff, docs=docs, bundle=bundle)
        # A genuinely smaller provider bound warns, rather than inventing
        # a per-agent 128K window or refusing short requests that can work.
        docs["models-v2"]["models"][key]["context"]["provider_tokens"] = 128_000
        lcat = _lcat(docs)
        lineup = profile.resolve(doc, lcat, effective=eff)
        self.assertEqual(compiler.agent_context_gaps(lineup, 800_000), (rid,))
        warning = next(f.message for f in lineup.warnings if f.code == "context-risk" and f.slot == rid)
        for number in ("200000", "800000", "128000"):
            self.assertIn(number, warning)
        result = _launch(lineup, eff, docs=docs, bundle=bundle)
        self.assertIn(lineup.agents[rid].binding.selector, result.scope_plan.settings["availableModels"])

    def test_fixture_catalog_never_trips_the_guard(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        for key, entry in lcat.lines.items():
            ctx = entry["context"]
            with self.subTest(key=key):
                self.assertFalse(
                    "agents" in entry["capabilities"]
                    and ctx["provider_tokens"] < ctx["client_tokens"]
                    and ctx["provider_tokens"] < 800_000
                    and ctx["client_tokens"] >= 1_000_000
                )


# ------------------------------------------------------------------ appendix


class AppendixTests(unittest.TestCase):
    def test_sections_and_sentinel(self) -> None:
        lineup, result = _seed_launch()
        appendix = _appendix(result)
        headers = [line for line in appendix.splitlines() if line.startswith("## ")]
        self.assertEqual(
            headers,
            [
                "## Session lineup (generated)",
                "## Native-agent policy (generated)",
                "## Context policy (generated)",
                "## Standing rules (generated)",
                "## Session sentinel (generated)",
            ],
        )
        self.assertTrue(appendix.endswith("`.\n"))
        self.assertFalse(appendix.endswith("\n\n"))
        self.assertIn(f"`{SCOPE_DIR / lineup_files.LINEUP_MD}`", appendix)
        self.assertIn("- Explore: replaced by `cm-explorer` (native Explore denied).", appendix)
        self.assertIn(f"- Managed session: {FIXED_SESSION}.", appendix)
        self.assertIn(f"`claude-multi -r {FIXED_SESSION}`", appendix)
        self.assertIn("press `s` (this session only)", appendix)
        self.assertTrue(result.lead_prompt.startswith(
            _bundle().prompt_bodies["cm-lead"].decode("utf-8") + "\n"
        ))

    def test_no_binding_family_inventory_or_profile_name(self) -> None:
        lineup, result = _seed_launch()
        appendix = _appendix(result)
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        for key, entry in lcat.lines.items():
            for _effort, selector, _contract in catalog.line_selectors(entry):
                self.assertNotIn(selector, appendix)
            self.assertNotIn(entry["display"], appendix)
        for provider in lcat.providers.values():
            family = provider["independence_family"]
            if family in catalog.IDENTITY_TOKEN_EXCLUSIONS:
                continue
            self.assertNotRegex(appendix.lower(), rf"\b{re.escape(family)}\b")
        self.assertNotIn(lineup.name, appendix)
        self.assertNotIn("composition", appendix)
        self.assertNotIn("Enabled provider families", appendix)
        self.assertNotIn("preferred", appendix)

    def test_explore_lines(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        doc = copy.deepcopy(bundle.seed_profiles["direct"])
        cases = {
            ("native", True): "- Explore: native, inheriting your model.",
            ("native", False): "- Explore: native, inheriting your model; under a Fable lead the "
            "client caps it to the Opus family default.",
            ("off", True): "- Explore: disabled without replacement.",
        }
        for (explore, cap_disabled), line in cases.items():
            eff = _eff(lcat, explore_inherit_cap_disabled=cap_disabled)
            doc["native_agents"] = {"explore": explore, "general_purpose": "on", "plan": "native"}
            lineup = profile.resolve(doc, lcat, effective=eff)
            with self.subTest(explore=explore, cap_disabled=cap_disabled):
                appendix = _appendix(_launch(lineup, eff, bundle=bundle))
                self.assertIn(line, appendix)
                # 2.1.286 caps only under a Fable lead.
                self.assertNotIn("unless your model id contains", appendix)

    def test_quiet_sonnet_agent_note(self) -> None:
        """The delivered lead prompt says a quiet Sonnet agent is not
        necessarily hung and asks for actual failure evidence before recovery
        or rerouting; it qualifies, never replaces, the silence clause."""

        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        for name in ("balanced", "direct"):
            lineup = profile.resolve(copy.deepcopy(bundle.seed_profiles[name]), lcat, effective=eff)
            appendix = _appendix(_launch(lineup, eff, bundle=bundle))
            with self.subTest(profile=name):
                self.assertEqual(appendix.count("- A quiet Sonnet agent is not necessarily hung"), 1)
                for needle in ("thinking whose summary is omitted",
                               "silence alone is neither a death nor a rate-limited provider",
                               "this qualifies the silence clause under Failure handling",
                               "only on actual failure evidence (an API error, a 429, a crash or a timeout)"):
                    self.assertIn(needle, appendix)

    def test_workflows_line(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        doc = copy.deepcopy(bundle.seed_profiles["balanced"])
        self.assertIn("- Workflows: native.", _appendix(_launch(profile.resolve(doc, lcat, effective=eff), eff, bundle=bundle)))
        doc["workflows"] = "off"
        appendix = _appendix(_launch(profile.resolve(doc, lcat, effective=eff), eff, bundle=bundle))
        self.assertIn("- Workflows: off (the Workflow tool is disabled).", appendix)
        self.assertNotIn("Delegation:", appendix)

    def test_context_lines(self) -> None:
        lineup, result = _seed_launch()
        appendix = _appendix(result)
        context = compiler.lead_set_context(result.fence, 90)
        self.assertIn(
            f"- Lead class `{lineup.lead_class}`: process compaction window {context.window} "
            f"tokens; deterministic reactive trigger {context.trigger} (90% of the prompt budget).",
            appendix,
        )
        self.assertEqual(
            "- Operating ceiling" in appendix, context.window < context.min_provider_tokens
        )
        self.assertEqual("Context qualification" in appendix, not context.validated)
        self.assertNotIn("Process scalar", appendix)
        # Lower-context class with a native agent on: the inheritance line.
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        low = profile.resolve(
            profile.ad_hoc_direct("qwen-flash-next"), lcat, effective=eff, ad_hoc=True
        )
        low_appendix = _appendix(_launch(low, eff, bundle=bundle))
        self.assertIn("- Native agents inherit this lower-context lead", low_appendix)
        self.assertNotIn("- Native agents inherit", appendix)
        quiet = _appendix(_launch(low, eff, bundle=bundle, no_subagents=True))
        self.assertNotIn("- Native agents inherit", quiet)

    def test_lineup_independent_prompt_and_path(self) -> None:
        # Other bindings (or none) with the same relaunch fields keep
        # the appendix, the prompt bytes and the prompt path.
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        base = copy.deepcopy(bundle.seed_profiles["balanced"])
        first = _launch(profile.resolve(base, lcat, effective=eff), eff, bundle=bundle)
        other = copy.deepcopy(base)
        other["name"] = "renamed"
        other["agents"] = {"cm-explorer": base["agents"]["cm-explorer"]}
        second = _launch(profile.resolve(other, lcat, effective=eff), eff, bundle=bundle)
        self.assertEqual(first.lead_prompt, second.lead_prompt)
        self.assertEqual(first.lead_prompt_path, second.lead_prompt_path)
        self.assertNotEqual(first.scope_plan.agent_files, second.scope_plan.agent_files)
        # A relaunch-only input moves it.
        changed = copy.deepcopy(base)
        changed["workflows"] = "off"
        third = _launch(profile.resolve(changed, lcat, effective=eff), eff, bundle=bundle)
        self.assertNotEqual(first.lead_prompt_path, third.lead_prompt_path)

    def test_prompt_path_is_content_addressed_and_prunable(self) -> None:
        for name in ("balanced", "direct"):
            _, result = _seed_launch(name)
            with self.subTest(seed=name):
                path = result.lead_prompt_path
                self.assertEqual(path.parent, STATE_ROOT)
                self.assertRegex(path.name, PROMPT_NAME)
                digest = strict_json.sha256_hex(result.lead_prompt.encode("utf-8"))[:16]
                self.assertEqual(path.name, f"lead-prompt-{digest}-{FIXED_SESSION}.md")
                self.assertTrue(result.write_lead_prompt)


# ------------------------------------------------------------------ contract


class ContractTests(unittest.TestCase):
    def test_result_fields(self) -> None:
        lineup, result = _seed_launch(lineup_generation=7, launch_epoch=3)
        self.assertEqual(result.lineup_generation, 7)
        self.assertIsInstance(result.fence, scope.Fence)
        self.assertEqual(
            result.fence.available_models, tuple(result.scope_plan.settings["availableModels"])
        )
        self.assertEqual(
            result.snapshot, {"applied": lineup.applied_bindings(), "lineup_generation": 7}
        )
        self.assertEqual(result.composition_name, "balanced")
        self.assertTrue(result.durable)
        self.assertEqual(result.agents_json, "")
        self.assertEqual(result.scope_dir, SCOPE_DIR)
        self.assertEqual(result.env_set["CLAUDE_MULTI_LAUNCH_EPOCH"], "3")
        self.assertTrue(result.scope_plan.other_files["lineup.gen"].startswith(b"7 "))
        # The 2.x path's results keep their defaults.
        self.assertIsNone(compiler.CompileResult.__dataclass_fields__["fence"].default)
        self.assertIsNone(compiler.CompileResult.__dataclass_fields__["lineup_generation"].default)

    def test_token_helper_and_hook_command_required(self) -> None:
        bundle, _l, eff, lineup = _seed("balanced")
        for missing in ("", None):
            with self.subTest(token_helper=missing):
                with self.assertRaisesRegex(CompilerError, "scope apiKeyHelper"):
                    _launch(lineup, eff, bundle=bundle, token_helper_command=missing)
        with self.assertRaisesRegex(CompilerError, "hook command"):
            _launch(lineup, eff, bundle=bundle, hook_command="")
        with self.assertRaisesRegex(ScopeError, "claude-multi-hook-3"):
            _launch(lineup, eff, bundle=bundle, hook_command="/state/bin/claude-multi-hook")
        with self.assertRaisesRegex(ScopeError, "lineup_generation"):
            _launch(lineup, eff, bundle=bundle, lineup_generation=0)
        with self.assertRaisesRegex(CompilerError, "unknown session action"):
            _launch(
                lineup,
                eff,
                bundle=bundle,
                action=compiler.SessionAction("fork", FIXED_SESSION, FIXED_SESSION),
            )

    def test_scope_helper_must_be_retained(self) -> None:
        bundle, _l, eff, lineup = _seed("balanced")
        real = scope.compile_lineup_scope

        def stripped(*args, **kw):
            plan = real(*args, **kw)
            settings_doc = dict(plan.settings)
            settings_doc.pop("apiKeyHelper")
            return dataclasses.replace(plan, settings=settings_doc)

        with mock.patch.object(scope, "compile_lineup_scope", stripped):
            with self.assertRaisesRegex(CompilerError, "scope apiKeyHelper"):
                _launch(lineup, eff, bundle=bundle)

    def test_scope_plan_is_the_scope_compile(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        result = _launch(lineup, eff, bundle=bundle)
        plan = scope.compile_lineup_scope(
            lineup,
            lcat,
            eff,
            bundle.prompt_bodies,
            scope.catalog_meta_v2(bundle.docs),
            lineup_generation=1,
            managed_id=FIXED_SESSION,
            hook_command=HOOK3,
            launch_epoch=0,
            token_helper_command=HELPER,
        )
        self.assertEqual(scope.plan_hash(result.scope_plan), scope.plan_hash(plan))

    def test_invalid_override_is_a_compiler_error(self) -> None:
        bundle, _l, eff, lineup = _seed("balanced")
        bad = dataclasses.replace(lineup, settings_overrides={"compaction_percent": 50})
        with self.assertRaisesRegex(CompilerError, "compaction_percent"):
            _launch(bad, eff, bundle=bundle)


class CustomLeadLaunchTests(unittest.TestCase):
    """Ad-hoc direct with a custom lead over merged docs."""

    def test_custom_context_is_user_attested_even_below_the_validation_cap(self) -> None:
        from tests.test_custom_registry import registry_fixture

        bundle = _bundle()
        for bound in (128_000, 1_000_000):
            with self.subTest(bound=bound):
                registry = registry_fixture()
                registry["providers"]["acme"]["header"] = "X-API-Key"
                registry["models"]["acme-one"]["context_tokens"] = bound
                docs = custom.merge_docs(bundle.docs, registry)
                lcat = _lcat(docs)
                eff = _eff(lcat, {"version": 1, "providers": {
                    pid: {"enabled": False} for pid in lcat.providers if pid != "acme"
                }})
                lineup = profile.resolve(
                    profile.ad_hoc_direct("acme-one", "high"), lcat, effective=eff, ad_hoc=True
                )
                result = _launch(lineup, eff, docs=docs, bundle=bundle)
                context = compiler.lead_set_context(result.fence, 90)
                self.assertTrue(context.custom_bound)
                self.assertFalse(context.validated)
                self.assertEqual(context.min_provider_tokens, bound)
                self.assertNotIn("custom_bound", context.as_document())
                self.assertEqual(set(context.as_document()), {"percent", "scalar", "trigger", "window"})
                appendix = _appendix(result)
                self.assertIn(
                    "- Context qualification: the lead set includes an operator-declared custom "
                    "model (custom.json); its provider bound is user-attested, not benchmark-verified.",
                    appendix,
                )
                self.assertNotIn("at least one lead-set provider bound", appendix)

    def test_catalog_only_context_retains_the_generic_qualification(self) -> None:
        _lineup, result = _seed_launch()
        context = compiler.lead_set_context(result.fence, 90)
        self.assertFalse(context.custom_bound)
        self.assertNotIn("operator-declared custom model", _appendix(result))
        self.assertEqual("Context qualification" in _appendix(result), not context.validated)

    def test_custom_lead_argv_and_env(self) -> None:
        bundle = _bundle()
        registry = {
            "version": 1,
            "providers": {
                "my-gw": {
                    "base_url": "https://llm.example.invalid/v1",
                    "auth_kind": "bearer",
                    "secret_env": "MY_GW_API_KEY",
                }
            },
            "models": {
                "my-model": {"provider": "my-gw", "wire_model": "my-wire", "context_tokens": 262144}
            },
        }
        docs = custom.merge_docs(bundle.docs, registry)
        lcat = _lcat(docs)
        others = [pid for pid in lcat.providers if pid != "my-gw"]
        eff = _eff(lcat, {"version": 1, "providers": {pid: {"enabled": False} for pid in others}})
        lineup = profile.resolve(
            profile.ad_hoc_direct("my-model", "high"), lcat, effective=eff, ad_hoc=True
        )
        result = _launch(lineup, eff, docs=docs, bundle=bundle, session_cwd="/work/proj")
        values = _flag_values(result.argv)
        selector = lcat.lines["my-model"]["selector"]
        self.assertEqual(values["--model"], selector)
        self.assertEqual(values["--effort"], "high")
        self.assertEqual(values["--name"], "cm:direct:my-model@proj")
        self.assertEqual(result.fence.lead_set[0].family, "custom")
        self.assertEqual(result.fence.fallback_only, ())
        self.assertEqual(result.scope_plan.settings["availableModels"], [selector])
        self.assertFalse([key for key in result.scope_plan.settings["env"] if key in FAMILY_KEYS])
        self.assertIn("MY_GW_API_KEY", result.env_unset)
        self.assertIn("- Explore:", _appendix(result))


# ------------------------------------------------------- worktree seam


class WorktreeSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-v2-git-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.repo = self.root / "repo"
        self.plain = self.root / "plain"
        self.repo.mkdir()
        self.plain.mkdir()
        self.env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _git_init(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def test_git_work_tree(self) -> None:
        self._git_init()
        self.assertTrue(compiler.git_work_tree(self.repo))
        (self.repo / "sub").mkdir()
        self.assertTrue(compiler.git_work_tree(self.repo / "sub"))
        self.assertFalse(compiler.git_work_tree(self.plain))
        self.assertFalse(compiler.git_work_tree(self.root / "missing"))
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertFalse(compiler.git_work_tree(self.repo))
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)
        ):
            self.assertFalse(compiler.git_work_tree(self.repo))

    def test_launch_marks_writer_grades_outside_a_work_tree(self) -> None:
        self._git_init()
        note = scope.WRITER_UNAVAILABLE_NOTE
        _, in_repo = _seed_launch(session_cwd=self.repo)
        _, outside = _seed_launch(session_cwd=self.plain)
        _, forced = _seed_launch(session_cwd=self.plain, worktree_available=True)
        _, no_cwd = _seed_launch()
        md = lambda result: result.scope_plan.other_files["lineup.md"].decode("utf-8")  # noqa: E731
        self.assertNotIn(note, md(in_repo))
        self.assertIn(note, md(outside))
        self.assertNotIn(note, md(forced))
        self.assertNotIn(note, md(no_cwd))
        # The appendix is lineup-independent: the seam never moves it.
        self.assertEqual(in_repo.lead_prompt_path, outside.lead_prompt_path)

    def test_explicit_value_skips_the_probe(self) -> None:
        with mock.patch.object(compiler, "git_work_tree") as probe:
            _seed_launch(session_cwd=self.plain, worktree_available=False)
            _seed_launch()
        probe.assert_not_called()
        with mock.patch.object(compiler, "git_work_tree", return_value=True) as probe:
            _seed_launch(session_cwd=self.plain)
        probe.assert_called_once_with(self.plain)


class LoopbackProxyCompileTests(unittest.TestCase):
    def test_no_proxy_bytes_match_the_existing_golden(self):
        bundle, lcat, eff, lineup = _seed("balanced")
        plan = _launch(lineup, eff, bundle=bundle, launch_environ={}).scope_plan
        assertGolden(self, GOLDENS_ROOT / "v2/managed/scope/settings.json",
                     strict_json.canonical_file_bytes(plan.settings))
        self.assertNotIn("NO_PROXY", plan.settings["env"])
        self.assertEqual(compiler.loopback_no_proxy({"NO_PROXY": "unrelated"}), {})

    def test_any_case_proxy_merges_both_spellings_and_layers_in_order(self):
        for name in (*compiler.PROXY_VARS, "hTtPs_PrOxY"):
            with self.subTest(name=name):
                env = compiler.loopback_no_proxy(
                    {name: "never-copy-this-value", "NO_PROXY": "shell,localhost", "no_proxy": "lower,shell"},
                    {"NO_PROXY": "user,lower"}, {"no_proxy": "project"},
                    previous={"NO_PROXY": "previous,::1"},
                )
                value = "127.0.0.1,localhost,::1,shell,lower,user,project,previous"
                self.assertEqual(env, {"NO_PROXY": value, "no_proxy": value})
                self.assertNotIn("never-copy-this-value", repr(env))

    def test_shell_proxy_is_in_process_and_scope_and_previous_is_sticky(self):
        bundle, lcat, eff, lineup = _seed("balanced")
        first = _launch(lineup, eff, bundle=bundle,
                        launch_environ={"https_proxy": "fixture", "NO_PROXY": "internal"})
        value = "127.0.0.1,localhost,::1,internal"
        for name in ("NO_PROXY", "no_proxy"):
            self.assertEqual(first.env_set[name], value)
            self.assertEqual(first.scope_plan.settings["env"][name], value)
        again = _launch(lineup, eff, bundle=bundle, launch_environ={},
                        proxy_env=first.scope_plan.settings["env"])
        self.assertEqual(first.scope_plan.settings, again.scope_plan.settings)

    def test_deny_overrides_use_documented_absolute_syntax(self):
        env = {"HOME": "/fixture/home", "ANTHROPIC_CONFIG_DIR": "/fixture/home/auth",
               "XDG_CONFIG_HOME": "/elsewhere/config"}
        self.assertEqual(scope.secret_path_denies(env)[-2:],
                         ("Read(~/auth/**)", "Read(//elsewhere/config/anthropic/**)"))
        self.assertEqual(scope.secret_path_denies({"HOME": "/fixture/home",
                                                  "ANTHROPIC_CONFIG_DIR": "/fixture/home/.config/anthropic"}),
                         scope.SECRET_PATH_DENIES)
        self.assertEqual(scope.secret_path_denies({"ANTHROPIC_CONFIG_DIR": "relative"}), scope.SECRET_PATH_DENIES)
        env["ANTHROPIC_CONFIG_DIR"] = "/elsewhere/config/anthropic"
        self.assertEqual(scope.secret_path_denies(env).count("Read(//elsewhere/config/anthropic/**)"), 1)


class SessionEnvKeepTests(unittest.TestCase):
    """The user's ``session_env_keep`` names leave only the generic
    ``*_API_KEY`` rule; provider, gateway and reserved names stay removed."""

    ENVIRON = {"DOCS_TOOL_API_KEY": "kept-value-1", "OTHER_TOOL_API_KEY": "other-value-2",
               "KIMI_CLAUDE_API_KEY": "provider-value-3", "PATH": "/bin"}

    def _child(self, result: compiler.CompileResult) -> dict[str, str]:
        child = dict(self.ENVIRON)
        for name in result.env_unset:
            child.pop(name, None)
        child.update(result.env_set)
        return child

    def test_a_kept_name_reaches_the_session_and_every_other_api_key_is_removed(self) -> None:
        for action in (compiler.build_fresh(FIXED_SESSION), compiler.build_resume(FIXED_SESSION, RUNTIME_ID)):
            with self.subTest(action=action.kind):
                _, result = _seed_launch(launch_environ=self.ENVIRON, env_keep=("DOCS_TOOL_API_KEY",), action=action)
                child = self._child(result)
                self.assertEqual(child["DOCS_TOOL_API_KEY"], "kept-value-1")
                self.assertNotIn("OTHER_TOOL_API_KEY", child)
                self.assertNotIn("KIMI_CLAUDE_API_KEY", child)
                self.assertEqual(result.env_keep, compiler.EnvKeep(
                    kept=("DOCS_TOOL_API_KEY",), stripped=("KIMI_CLAUDE_API_KEY", "OTHER_TOOL_API_KEY")))
                # Names only: no value enters the plan, the scope or the prompt.
                for value in ("kept-value-1", "other-value-2", "provider-value-3"):
                    self.assertNotIn(value, repr(result))

    def test_without_a_keep_list_every_api_key_is_removed(self) -> None:
        _, result = _seed_launch(launch_environ=self.ENVIRON)
        self.assertNotIn("DOCS_TOOL_API_KEY", self._child(result))
        self.assertEqual(result.env_keep.kept, ())

    def test_a_kept_name_never_keeps_a_provider_or_reserved_name(self) -> None:
        bundle = _bundle()
        provider_names = sorted({ref[4:] for provider in bundle.docs["providers"]["providers"].values()
                                 if (ref := provider["transport"].get("auth", {}).get("secret_ref", "")).startswith("env:")})
        self.assertTrue(provider_names)
        keep = (*provider_names, "ANTHROPIC_EXTRA_API_KEY", "PLATFORM_OPENAI_API_KEY", "DOCS_TOOL_API_KEY", "PATH")
        environ = {name: "v" for name in keep}
        _, result = _seed_launch(launch_environ=environ, env_keep=keep)
        for name in (*provider_names, "ANTHROPIC_EXTRA_API_KEY", "PLATFORM_OPENAI_API_KEY"):
            with self.subTest(name=name):
                self.assertIn(name, result.env_unset)
                self.assertIn(name, dict(result.env_keep.refused))
        self.assertNotIn("DOCS_TOOL_API_KEY", result.env_unset)
        self.assertIn("PATH", dict(result.env_keep.refused))  # never stripped: no exception needed
        self.assertNotIn("PATH", result.env_unset)

    def test_a_provider_declared_later_turns_the_exception_off(self) -> None:
        bundle = _bundle()
        registry = {
            "version": 1,
            "providers": {"tool-gw": {"base_url": "https://llm.example.invalid/v1", "auth_kind": "bearer",
                                      "secret_env": "DOCS_TOOL_API_KEY"}},
            "models": {"tool-model": {"provider": "tool-gw", "wire_model": "w", "context_tokens": 262144}},
        }
        docs = custom.merge_docs(bundle.docs, registry)
        lcat = _lcat(docs)
        eff = _eff(lcat)
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)
        result = _launch(lineup, eff, docs=docs, bundle=bundle, launch_environ=self.ENVIRON,
                         env_keep=("DOCS_TOOL_API_KEY",))
        self.assertIn("DOCS_TOOL_API_KEY", result.env_unset)
        self.assertEqual([name for name, _why in result.env_keep.refused], ["DOCS_TOOL_API_KEY"])
        self.assertIn("provider", dict(result.env_keep.refused)["DOCS_TOOL_API_KEY"])

    def test_the_keep_list_changes_no_scope_byte(self) -> None:
        _, clean = _seed_launch(launch_environ=self.ENVIRON)
        _, kept = _seed_launch(launch_environ=self.ENVIRON, env_keep=("DOCS_TOOL_API_KEY",))
        self.assertEqual(kept.scope_plan, clean.scope_plan)
        self.assertEqual(kept.lead_prompt, clean.lead_prompt)
        self.assertEqual(kept.argv, clean.argv)

    def test_v2_env_unset_keep_leaves_only_the_generic_scan(self) -> None:
        names = compiler.v2_env_unset(
            scalar=None, secret_env_names=["TOOL_API_KEY"],
            launch_environ={"TOOL_API_KEY": "x", "DOCS_TOOL_API_KEY": "x", "MANAGEMENT_PASSWORD": "x"},
            env_keep=("TOOL_API_KEY", "DOCS_TOOL_API_KEY", "MANAGEMENT_PASSWORD"),
        )
        self.assertIn("TOOL_API_KEY", names)  # a provider reference wins
        self.assertIn("MANAGEMENT_PASSWORD", names)
        self.assertNotIn("DOCS_TOOL_API_KEY", names)


class WslSecretDenyTests(unittest.TestCase):
    """Under WSL the Windows side's Claude configuration and secrets are
    denied; every other compiled scope is unchanged."""

    def test_the_fixed_wsl_rules(self) -> None:
        env = {"HOME": "/fixture/home"}
        self.assertEqual(scope.secret_path_denies(env, wsl=False), scope.SECRET_PATH_DENIES)
        under = scope.secret_path_denies(env, wsl=True)
        self.assertEqual(under, (*scope.SECRET_PATH_DENIES, *scope.WSL_SECRET_PATH_DENIES))
        for folder in ("//mnt/c/Users/*/.claude/**", "//mnt/c/Users/*/AppData/Roaming/Anthropic/**"):
            for verb in ("Read", "Edit"):
                self.assertIn(f"{verb}({folder})", under)
        self.assertEqual([rule for rule, _cause in scope.secret_deny_causes(env, wsl=True)],
                         list(scope.WSL_SECRET_PATH_DENIES))
        self.assertEqual(scope.secret_deny_causes(env, wsl=False), ())

    def test_wsl_is_detected_from_the_launch_environment(self) -> None:
        import sys

        if not sys.platform.startswith("linux"):
            self.skipTest("BOUNDARY: WSL detection is Linux-only")
        _, plain = _seed_launch(launch_environ={"HOME": "/fixture/home"})
        _, wsl = _seed_launch(launch_environ={"HOME": "/fixture/home", "WSL_DISTRO_NAME": "Ubuntu"})
        plain_denies = plain.scope_plan.settings["permissions"]["deny"]
        wsl_denies = wsl.scope_plan.settings["permissions"]["deny"]
        self.assertFalse(set(scope.WSL_SECRET_PATH_DENIES) & set(plain_denies))
        self.assertEqual([rule for rule in wsl_denies if rule not in plain_denies],
                         list(scope.WSL_SECRET_PATH_DENIES))

    def test_environment_dependent_denies_name_their_cause(self) -> None:
        env = {"HOME": "/fixture/home", "ANTHROPIC_CONFIG_DIR": "/elsewhere/auth",
               "CLAUDE_MULTI_SECRET_ENV": "/elsewhere/keys.env"}
        causes = dict(scope.secret_deny_causes(env, wsl=False))
        self.assertIn("ANTHROPIC_CONFIG_DIR", causes["Read(//elsewhere/auth/**)"])
        self.assertIn("CLAUDE_MULTI_SECRET_ENV", causes["Read(//elsewhere/keys.env)"])
        self.assertIn("CLAUDE_MULTI_SECRET_ENV", causes["Edit(//elsewhere/keys.env)"])
        self.assertEqual(tuple(rule for rule in scope.secret_path_denies(env, wsl=False)
                               if rule not in scope.SECRET_PATH_DENIES), tuple(causes))


if __name__ == "__main__":
    unittest.main()
