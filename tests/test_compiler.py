"""Tests for the deterministic compiler: agents, lead content, env, argv, goldens."""

from __future__ import annotations

import unittest
from pathlib import Path

from claude_multi import catalog, compiler, strict_json
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden


CATALOG_ROOT = FIXTURE_ROOT
GOLDENS = GOLDENS_ROOT / "default"
FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
SETTINGS_PATH = Path("/trusted/settings.json")
SCOPE_DIR = Path("/state") / "scopes" / FIXED_SESSION


class LeadPromptPathTests(unittest.TestCase):
    def test_session_scoped_and_deterministic(self) -> None:
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        path = compiler.lead_prompt_path(Path("/state"), digest_a, FIXED_SESSION)
        # resume of the same UUID is stable
        self.assertEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_a, FIXED_SESSION)
        )
        self.assertIn(FIXED_SESSION, path.name)
        self.assertIn(digest_a.removeprefix("sha256:")[:16], path.name)
        # fresh launches with different UUIDs never collide
        self.assertNotEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_a, OTHER_SESSION)
        )
        # composition transition for the same UUID lands on the new digest
        self.assertNotEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_b, FIXED_SESSION)
        )


class PassthroughFlagTests(unittest.TestCase):
    def test_disable_slash_commands_is_blocked(self) -> None:
        for token in ("--disable-slash-commands", "--disable-slash-commands=x"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(
                    compiler.CompilerError, "agent-directory watching"
                ):
                    compiler.validate_passthrough([token])


class EnvironmentTests(unittest.TestCase):


    def test_family_default_env_rules(self) -> None:
        import copy

        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.docs["models"]["models"])
        providers = bundle.docs["providers"]["providers"]
        # A missing line omits its variable; a sub-1M line has no [1m].
        del models["fable"]
        models["opus"]["context"]["client_tokens"] = 200_000
        self.assertEqual(
            compiler.family_default_env(models, providers),
            {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8"},
        )
        # A custom synthetic entry is never a family default.
        models["sonnet"] = {**copy.deepcopy(models["opus"]), "family": "custom",
                            "provider": "kimi", "wire_model": "k3"}
        self.assertNotIn(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", compiler.family_default_env(models, providers)
        )
        self.assertEqual(
            [variable for _key, variable in compiler.FAMILY_DEFAULT_LINES],
            ["ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
             "ANTHROPIC_DEFAULT_SONNET_MODEL"],
        )


if __name__ == "__main__":
    unittest.main()


class SessionDisplayNameTests(unittest.TestCase):
    """--name gains the project basename; old form without cwd."""

    def test_no_cwd_keeps_plain_prefix(self) -> None:
        self.assertEqual(compiler.session_display_name("cm:default", None), "cm:default")

    def test_root_cwd_keeps_plain_prefix(self) -> None:
        self.assertEqual(compiler.session_display_name("cg:sol", "/"), "cg:sol")

    def test_basename_appended(self) -> None:
        self.assertEqual(
            compiler.session_display_name("cm:kimi-sol", "/home/user/projects/example-app"),
            "cm:kimi-sol@example-app",
        )

    def test_long_basename_capped(self) -> None:
        name = compiler.session_display_name(
            "cg:glm52", "/x/" + "a" * 40
        )
        self.assertEqual(name, "cg:glm52@" + "a" * 24)

    def test_control_characters_stripped(self) -> None:
        name = compiler.session_display_name("cm:default", "/x/evil\x1b[2k\x07dir\n")
        self.assertNotIn("\x1b", name)
        self.assertNotIn("\x07", name)
        self.assertNotIn("\n", name)
        self.assertTrue(name.startswith("cm:default@evil"))


class GrokProfileFenceTests(unittest.TestCase):
    """The grok 500K selector (a fence is the lead set)."""

    def test_grok_selector_carries_no_1m_suffix(self) -> None:
        # 500K is not 1M-class: the selector must not self-classify as
        # extended-context; the window comes from the profile env.
        # On the v2 line's selectors (the v1 view is gone).
        bundle = catalog.load_catalog(CATALOG_ROOT)
        selectors = catalog.line_selectors(bundle.lines["grok46"])
        self.assertTrue(selectors)
        for _effort, selector, _contract in selectors:
            self.assertNotIn("[1m]", selector)
