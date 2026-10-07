"""Provider-route journeys through the real full-screen UI in a
pseudo-terminal, at 80x24 and on a wide terminal: a reviewed preset alone
to a usable starter profile, the OpenAI API key alone to a launch, and
OpenAI's switch from its account to its API key and back on the Providers
screen.

The children are the onboarding journeys' (``test_journeys_onboarding``): a
fixture runtime on a temporary home with nothing connected, a verified
Claude Code copy, the reload answered by the runtime's seam and a fake
launch; nothing leaves the machine. The OpenAI journeys run on a copy of
the fixture assets with one OpenAI line reviewed for the API-key route;
the preset journey reads the shipped presets. The keys sent are derived
from a parent-side runtime of the same shape, never fixture ids written
into the test.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import test_journeys_onboarding as journeys
from test_journeys_onboarding import DOWN, ENTER, ESC, HOME, MODELS_CURSOR, RIGHT, JourneyCase

WIDE = {"rows": 40, "columns": 120}
SIZES = {"80x24": {}, "wide": WIDE}

# The child's assets: a copy of the fixture's with one OpenAI line reviewed
# for the API-key route (the setup code of a child; ``runtime`` is in scope).
REVIEWED = """
import test_openai_key_route as key_route
runtime.asset_root, reviewed = key_route.reviewed_asset_root(Path(runtime.environ["HOME"]).parent)
runtime.reload_catalog()
journeys.say("REVIEWED", reviewed)
"""
# The Providers screen's row of OpenAI (printed for the parent).
OPENAI_ROW = """
import claude_multi.cli.screens.providers as providers_screen
from claude_multi import tui
rows = [row.id for row in providers_screen._ProvidersScreen(runtime, palette=tui.MONO_PALETTE,
                                                             journal=lambda: None).rows]
journeys.say("ROW", rows.index("openai"))
"""
TRANSPORT_AFTER = """
from claude_multi import secret_store
from claude_multi.setup import providers as layer
print("TRANSPORT=" + layer.current_transport(runtime, "openai"), flush=True)
name = "PLATFORM_OPENAI_API_KEY"
print("KEY_KEPT=" + str(secret_store.default_store(runtime.gateway_environ()).get(name) is not None), flush=True)
"""


class ProviderRouteJourneys(JourneyCase):
    # The model and admission steps the onboarding journeys share.
    declare_by_hand = journeys.FirstRunJourneys.declare_by_hand
    admit_on_models = journeys.FirstRunJourneys.admit_on_models
    starter_from_step_six = journeys.FirstRunJourneys.starter_from_step_six

    def reviewed_picker_index(self, entry_id: str) -> int:
        """The picker position of ``entry_id`` on the reviewed assets."""

        import test_openai_key_route as key_route
        from claude_multi.setup import providers as layer

        with self.shape() as runtime:
            runtime.asset_root, _key = key_route.reviewed_asset_root(Path(runtime.environ["HOME"]).parent)
            runtime.reload_catalog()
            return [entry.id for entry in layer.picker_entries(runtime)].index(entry_id)

    # -- a reviewed preset alone ---------------------------------------------------

    def preset_alone(self, size: dict[str, Any]) -> None:
        import test_presets
        from claude_multi import views
        from claude_multi.cli import text as cli_text
        from claude_multi.setup import texts

        name = test_presets.first("anthropic-compatible")
        info = test_presets.shipped()[name]
        key = f"custom-{name}-chat"
        options = {"presets": True, "serve_render": True}
        child = self.child(setup=MODELS_CURSOR.format(key=key), options=options, **size)
        child.expect("claude-multi — Get started")
        self.to_picker_entry(child, f"preset:{name}", options)
        child.expect(cli_text.PRESET_FORM_TITLE.format(display=info.display))
        child.keys(ENTER)  # the preset's own name
        child.expect(views.PRESET_ADDRESS_LABEL)
        child.expect(info.base_url)
        child.keys(ENTER)  # the preset's own address
        child.expect(cli_text.ENDPOINT_PREVIEW_TITLE)
        child.expect(texts.PRESET_PREVIEW_HEAD.format(display=info.display)[:30])
        child.keys(ENTER)  # declare
        child.expect(cli_text.APPROVE_TITLE.format(id=name))
        child.keys(RIGHT, ENTER)  # Approve
        child.expect(cli_text.KEY_MODAL_TITLE.format(display=info.display))
        child.keys("preset-journey-key", ENTER, ENTER)
        child.expect(cli_text.ADD_MODELS_TITLE.format(id=name))
        self.declare_by_hand(child, f"{name}-chat-1", key)
        child.expect(cli_text.ADMIT_NOW_TITLE.format(key=key))
        child.keys(ENTER)  # Default Skip; Models records an optional local badge below.
        child.expect(f"Added {name}")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("Status  ")
        self.admit_on_models(child)
        output = self.starter_from_step_six(child)
        self.assertIn("DEFAULT=starter", output)
        self.assertNotIn("preset-journey-key", output)

    def test_a_preset_alone_reaches_a_usable_profile_80x24(self) -> None:
        self.preset_alone(SIZES["80x24"])

    def test_a_preset_alone_reaches_a_usable_profile_wide(self) -> None:
        self.preset_alone(SIZES["wide"])

    # -- the OpenAI API key alone -----------------------------------------------------

    def openai_key_alone(self, size: dict[str, Any]) -> None:
        from claude_multi.cli import text as cli_text

        index = self.reviewed_picker_index("openai:api-key")
        child = self.child(setup=REVIEWED, after=TRANSPORT_AFTER, **size)
        reviewed = self.fact(child, "REVIEWED")
        child.expect("claude-multi — Get started")
        child.keys("a")
        child.expect("connect a provider — more can follow")
        child.keys(HOME, *([DOWN] * index), ENTER)
        child.expect(cli_text.TRANSPORT_TO_KEY_TITLE.format(display="OpenAI", models="OpenAI models"))
        child.expect(reviewed)  # the consent names the models reviewed for the key
        child.keys(RIGHT, ENTER)  # Switch
        child.expect(cli_text.KEY_MODAL_TITLE.format(display="OpenAI"))
        child.keys("sk-openai-journey-key", ENTER, ENTER)
        child.expect("OpenAI models now use your OpenAI API key")
        child.keys(ESC)
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("profile: openai")
        child.keys(ENTER)
        child.expect_out("FAKE_LAUNCH=openai")
        output = self.finish(child)
        self.assertIn("TRANSPORT=api-key", output)
        self.assertIn("KEY_KEPT=True", output)
        self.assertNotIn("sk-openai-journey-key", output)

    def test_the_openai_key_alone_launches_80x24(self) -> None:
        self.openai_key_alone(SIZES["80x24"])

    def test_the_openai_key_alone_launches_wide(self) -> None:
        self.openai_key_alone(SIZES["wide"])

    # -- OpenAI's account or its API key on the Providers screen ------------------------

    def platform_switch(self, size: dict[str, Any]) -> None:
        from claude_multi.cli import text as cli_text

        child = self.child(setup=(REVIEWED, OPENAI_ROW), after=TRANSPORT_AFTER, **size)
        self.fact(child, "REVIEWED")
        row = int(self.fact(child, "ROW"))
        child.expect("claude-multi — Get started")
        child.keys(ESC)
        child.expect("Status  ")
        child.keys("g")
        child.expect("providers — local status")
        # Enter on OpenAI: the chooser, then its API key, Switch, the key.
        child.keys(HOME, *([DOWN] * row), ENTER)
        child.expect(cli_text.TRANSPORT_CHOOSER_TITLE.format(display="OpenAI"))
        child.keys(DOWN, ENTER)
        child.expect(cli_text.TRANSPORT_TO_KEY_TITLE.format(display="OpenAI", models="OpenAI models"))
        child.keys(RIGHT, ENTER)
        child.expect(cli_text.KEY_MODAL_TITLE.format(display="OpenAI"))
        child.keys("sk-openai-switch-key", ENTER, ENTER)
        child.expect("OpenAI models now use your OpenAI API key")
        # And back to the account: Switch, keep the key, sign in later.
        child.keys(ENTER)
        child.expect(cli_text.TRANSPORT_CHOOSER_TITLE.format(display="OpenAI"))
        child.keys(ENTER)  # the account is the chooser's first item
        child.expect(cli_text.TRANSPORT_TO_ACCOUNT_TITLE.format(account="ChatGPT account", models="OpenAI models"))
        child.keys(RIGHT, ENTER)
        child.expect(cli_text.KEEP_KEY_TITLE.format(display="OpenAI"))
        child.keys(ENTER)  # Keep it
        child.expect(cli_text.SIGN_IN_NOW_TITLE.format(account="ChatGPT account"))
        child.keys(ENTER)  # Later
        child.expect("OpenAI models use your ChatGPT account sign-in again")
        child.keys(ESC)
        child.expect("Status  ")
        child.keys(ESC)
        output = self.finish(child)
        self.assertIn("Nothing was launched.", output)
        self.assertIn("TRANSPORT=oauth-pool", output)
        self.assertIn("KEY_KEPT=True", output)
        self.assertNotIn("sk-openai-switch-key", output)

    def test_the_platform_transport_switch_80x24(self) -> None:
        self.platform_switch(SIZES["80x24"])

    def test_the_platform_transport_switch_wide(self) -> None:
        self.platform_switch(SIZES["wide"])


if __name__ == "__main__":
    unittest.main()
