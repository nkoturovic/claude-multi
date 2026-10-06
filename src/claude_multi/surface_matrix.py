"""Every semantic operation of claude-multi and where a person reaches it.

One row per operation, not per parser leaf: its object, who it is for, the
command-line spellings that perform it (option-derived spellings such as
``direct --no-subagents`` included), the ``claude-multi-proxy`` commands, the
``/cm`` verbs, the TUI actions (an action id of ``cli.screens.actions``, the
path a person takes, when it is offered and the test that drives it), and an
explicit reason for every surface it does not have. Destructive operations
name their confirmation; a change that one command undoes is reversible, not
destructive, and says why. Command-line spellings name the stream their
result goes to and the exit contract they follow.

:data:`REQUIRED` is the inventory of operations the product must offer,
kept apart from the rows, so an operation without any implementation is
still a row the census looks for. :func:`census` checks the rows against
the surfaces the code actually has (the argument parser, the gateway tool's
command table, the ``/cm`` verb table and the TUI action table), which the
caller discovers and passes in: nothing here builds a Runtime, reads a home
or starts anything. :func:`document` is the same data for the documentation
(the task tables' TUI path column).

Pure data and pure checks; standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# The objects of the inventory, in the documentation's order.
OBJECTS = (
    "launch", "setup", "install", "gateway", "gateway-service", "providers", "credentials", "models",
    "discovery", "profiles", "sessions", "lineup", "diagnostics", "settings", "bindings", "portability",
    "maintenance", "compatibility", "review", "interface",
)
AUDIENCES = ("user", "scripting", "maintenance", "service", "compatibility", "internal")
# Where a command-line spelling writes its result (``cli/streams.py``):
# stdout (a pipe or a redirect gets it; questions go to stderr) or the
# terminal (a full-screen view, an editor or a launch).
STREAMS = ("stdout", "terminal")
# The exit contract of a surface: the ordinary user-command table (0 ok, 1
# failed, refused or unavailable, 2 usage, 3 declined, 130 interrupted), the
# gateway tool's documented statuses, the hooks' fail-open/fail-closed
# protocol, or the /cm skill transport (status 0 with the text).
EXITS = ("user", "proxy", "hook", "skill")
# How a destructive operation is confirmed.
CONFIRMS = (
    "y/N, default No; --yes off a terminal",
    "y/N, default No",
    "dialog with Cancel focused first",
    "typed confirmation",
    "typed session id when liveness is unknown",
    "consent naming every request",
)
SURFACES = ("cli", "proxy", "cm", "tui")
# Words an absence reason must not lean on: a reason names the product's
# reason, never a plan, an owner or a later pass.
UNREASONS = ("todo", "tbd", "later", "follow-up", "followup", "not yet", "owner", "wip")


@dataclass(frozen=True)
class Tui:
    """One TUI mapping: the action (``cli.screens.actions`` id), the path a
    person takes, when it is offered, and the test that drives it
    (``tests.<module>.<Class>.<method>``): a dispatch fixture that drives
    the action's own screen, feeds the action's key to the input driver and
    asserts the call it makes or what it shows after that press."""

    action: str
    path: str
    available: str
    fixture: str


@dataclass(frozen=True)
class Op:
    """One semantic operation (see the module docstring)."""

    id: str
    object: str
    does: str
    audience: str = "user"
    cli: tuple[str, ...] = ()
    proxy: tuple[str, ...] = ()
    cm: tuple[str, ...] = ()
    tui: tuple[Tui, ...] = ()
    absent: Mapping[str, str] = field(default_factory=dict)
    destructive: bool = False
    confirm: str | None = None
    stream: str | None = None
    exit: str | None = None
    # Why a change that replaces a value is not destructive: the way back,
    # which its result shows (never set on a destructive row).
    reversible: str | None = None

    def surfaces(self) -> tuple[str, ...]:
        return tuple(name for name in SURFACES if getattr(self, name))


def _t(action: str, path: str, available: str, fixture: str) -> Tui:
    return Tui(action, path, available, fixture)


ALWAYS = "always"
SETTINGS_CLI = "an interactive, validated editor of local choices; no general settings command is promised"
BINDINGS_CLI = "named bindings are edited in their validated TUI editor; there is no binding command"
PORTABLE_TUI = "portable-file input and output: the file is written for, or read from, another computer"
MACHINE_TUI = "a machine-readable report for scripts and issue reports; the screens show the same facts"
INSTALL_TUI = ("it replaces or removes the running installation, so it runs from a terminal where the "
               "launcher starts again cleanly")
MAINTENANCE_TUI = "guarded maintenance of generated state, run deliberately from a terminal"
COMPAT_TUI = "an earlier spelling or a one-time move from the earlier format; accepted, not offered"
IN_SESSION = "it runs inside a managed session, where the lead asks for it"
SERVICE_TUI = "the supervised unit and the launcher run it; a person never needs it"

# The gateway operations the TUI leaves to the CLI (``gateway_actions.CLI_ONLY``
# gives the same reasons on the screen).
GATEWAY_STOP_TUI = ("a launch never needs it and it is refused while the persistence hold is active; "
                    "run claude-multi gateway stop in a terminal")
GATEWAY_CLEAR_HOLD_TUI = ("it needs a typed confirmation in a terminal outside Claude Code: "
                          "claude-multi gateway clear-hold")
GATEWAY_SERVICE_UNINSTALL_TUI = ("it reverses a deliberate choice and changes the service manager; run "
                                 "claude-multi gateway service uninstall in a terminal")

T_CARD = "tests.test_screens_card"
T_CARD_ONB = "tests.test_screens_card_onboarding"
T_CATALOG = "tests.test_screens_catalog"
T_DIRECT = "tests.test_screens_direct"
T_EDITOR = "tests.test_screens_editor"
T_GS = "tests.test_screens_get_started"
T_MODELS = "tests.test_screens_models_lifecycle"
T_PROFILES = "tests.test_screens_profiles"
T_PROVIDERS = "tests.test_screens_providers_lifecycle"
T_RECOVERY = "tests.test_screens_recovery"
T_SESSIONS = "tests.test_screens_sessions"
T_DISPATCH = "tests.test_tui_dispatch"
T_WINDOW = "tests.test_agent_context"

OPERATIONS: tuple[Op, ...] = (
    # ------------------------------------------------------------ launch
    Op("launch.fresh", "launch", "launch the profile shown on the card", cli=("",),
       tui=(_t("card.launch", "card → Enter", "the card's profile can launch",
               f"{T_CARD}.CardKeyTests.test_enter_on_ready_returns_the_prepared_perform_intent"),),
       stream="terminal", exit="user"),
    Op("launch.choose-profile", "launch", "choose the profile to launch", cli=("--profile",),
       tui=(_t("card.next-profile", "card → Tab", "more than one profile",
               f"{T_CARD}.CardProfileCycleTests.test_tab_cycles_profiles_in_mru_order"),
            _t("profiles.use", "card → P → Enter", "a profile that can be loaded",
               f"{T_PROFILES}.UseTests.test_enter_uses_the_selected_profile")),
       stream="terminal", exit="user"),
    Op("launch.direct", "launch", "launch one model as the lead (choose the model and its effort)",
       cli=("direct",),
       tui=(_t("card.direct", "card → D", ALWAYS,
               f"{T_CARD}.CardKeyTests.test_d_direct_returns_a_perform_intent_with_an_ad_hoc_record"),
            _t("direct.launch", "card → D → Enter", "a lead-capable model is offered",
               f"{T_DIRECT}.DirectLaunchTests.test_passthrough_threads_into_the_prepared_argv"),
            _t("direct.effort", "card → D → ← →", "a model whose gateway serves several efforts",
               f"{T_DIRECT}.DirectRowTests.test_gateway_rows_cycle_their_effort_and_client_rows_do_not")),
       stream="terminal", exit="user"),
    Op("launch.continue", "launch", "continue the last session in this directory",
       cli=("-c", "direct -c"),
       tui=(_t("sessions.resume", "card → S → Enter on this directory's newest session", "a session here",
               f"{T_SESSIONS}.ResumeGateModalTests.test_enter_continues_this_directorys_newest_session"),),
       stream="terminal", exit="user"),
    Op("launch.resume", "launch", "select a managed session and resume it", cli=("-r", "direct -r"),
       tui=(_t("sessions.resume", "card → S → Enter", "a managed session that is not running",
               f"{T_SESSIONS}.ResumeGateModalTests.test_resume_without_a_diff_returns_the_perform_intent_directly"),
            _t("resume-card.resume", "claude-multi -r → Enter", "the resume card",
               f"{T_CARD}.CardResumeKeyTests.test_enter_resumes_with_the_callers_decision")),
       stream="terminal", exit="user"),
    Op("launch.force-resume", "launch", "resume despite a background-liveness marker",
       cli=("--force", "direct --force"),
       tui=(_t("sessions.resume", "card → S → Enter → Resume anyway", "the background-liveness gate",
               f"{T_SESSIONS}.ResumeGateModalTests.test_daemon_owned_gate_modal_resume_anyway_threads_force"),),
       stream="terminal", exit="user"),
    Op("launch.profile-file", "launch", "launch an unsaved profile file (or stdin)", audience="scripting",
       cli=("--profile-file",),
       absent={"tui": "an unsaved profile document is a file or stdin input for scripts; the TUI launches "
                      "and edits saved profiles"},
       stream="terminal", exit="user"),
    Op("launch.print-launch", "launch", "print what a launch would run and whether it would succeed",
       audience="scripting", cli=("--print-launch",),
       absent={"tui": "a dry run for scripts and issue reports; the card shows the plan before Enter"},
       stream="terminal", exit="user"),
    Op("launch.print-launch-direct", "launch", "print what a direct launch would run", audience="scripting",
       cli=("direct --print-launch",),
       absent={"tui": "a dry run for scripts and issue reports; Direct shows the model before Enter"},
       stream="stdout", exit="user"),
    Op("launch.passthrough", "launch", "pass arguments to Claude Code unchanged", audience="scripting",
       cli=("--", "direct --"),
       absent={"tui": "arguments for Claude Code are a command-line input; the card launches without any"},
       stream="terminal", exit="user"),
    Op("launch.no-subagents", "launch", "hard-deny subagent delegation for a direct session",
       cli=("direct --no-subagents",),
       absent={"tui": "the deny is fixed at launch and recorded for every resume, so it is chosen where the "
                      "launch is spelled out; Direct launches with delegation allowed, and a lead-only "
                      "profile is the TUI's session without agents"},
       stream="terminal", exit="user"),
    Op("launch.save-direct", "launch", "save a direct choice as a lead-only profile", cli=("profile new",),
       tui=(_t("direct.save", "card → D → Tab", "a catalog model (not one you declared)",
               f"{T_DIRECT}.DirectLaunchTests.test_tab_saves_a_lead_only_profile_without_a_seed_marker"),),
       stream="terminal", exit="user"),
    # ------------------------------------------------------------ setup
    Op("setup.status", "setup", "show the setup steps and what is done", cli=("setup --status",),
       tui=(_t("card.get-started", "card → W", ALWAYS,
               f"{T_CARD_ONB}.GetStartedKeyTests.test_w_opens_get_started_and_the_card_follows_the_default"),
            _t("profiles.get-started", "card → P → W", ALWAYS, f"{T_PROFILES}.UseTests.test_w_opens_get_started")),
       stream="stdout", exit="user"),
    Op("setup.run", "setup", "run the steps not done yet, in order", cli=("setup",),
       tui=(_t("get-started.open", "card → W → Enter on the first open step", "a step not done",
               f"{T_GS}.StepViewTests.test_profile_step_builds_the_starter_and_makes_it_the_default"),),
       stream="stdout", exit="user"),
    Op("setup.redo", "setup", "run one step again, done or not", cli=("setup --step", "setup --redo"),
       tui=(_t("get-started.open", "card → W → Enter on any step", ALWAYS,
               f"{T_GS}.StepViewTests.test_the_gateway_step_reports_and_offers_the_log"),),
       stream="stdout", exit="user"),
    Op("setup.preflight", "setup", "check this computer before anything is installed",
       cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → this computer", ALWAYS,
               f"{T_GS}.StepListTests.test_enter_on_this_computer_opens_the_preflight"),),
       stream="stdout", exit="user"),
    Op("setup.claude", "setup", "acquire and verify claude-multi's own copy of the pinned Claude Code",
       cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → Claude Code", ALWAYS,
               f"{T_GS}.ClaudeStepTests.test_a_no_downloads_nothing_and_a_yes_installs_the_verified_copy"),),
       stream="stdout", exit="user"),
    Op("setup.claude-from", "setup", "install the pinned Claude Code from a local file",
       cli=("setup --claude-from",),
       absent={"tui": "a local copy of the pinned build is a file input for offline machines; the Claude "
                      "Code step downloads and verifies the same copy"},
       stream="stdout", exit="user"),
    Op("setup.gateway", "setup", "set up and start the local gateway", cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → local gateway", ALWAYS,
               f"{T_GS}.StepViewTests.test_the_gateway_step_reports_and_offers_the_log"),),
       stream="stdout", exit="user"),
    Op("setup.proxy", "setup", "set or clear the gateway's outbound proxy",
       cli=("setup --proxy", "setup --no-proxy"),
       tui=(_t("get-started.open", "card → W → local gateway → outbound proxy", ALWAYS,
               f"{T_GS}.ProxyTests.test_enter_on_the_gateway_step_opens_the_outbound_proxy_and_saves_it"),),
       stream="stdout", exit="user"),
    Op("setup.providers", "setup", "connect providers (a key, a sign-in or your own endpoint)",
       cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → providers", ALWAYS,
               f"{T_GS}.ProvidersStepTests.test_enter_on_the_providers_step_connects_a_key_in_the_picker"),
            _t("get-started.add-provider", "card → W → A", ALWAYS,
               f"{T_GS}.ProvidersStepTests.test_a_adds_your_own_endpoint_from_any_step")),
       stream="stdout", exit="user"),
    Op("setup.test", "setup", "test the connected providers (one consented request each)",
       cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → test", "a provider is connected",
               f"{T_GS}.StepViewTests.test_the_test_step_asks_once_and_a_no_sends_nothing"),),
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("setup.profile", "setup", "choose or build the profile to use (a starter from what is connected)",
       cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → profile", "a provider is connected",
               f"{T_GS}.StepViewTests.test_profile_step_builds_the_starter_and_makes_it_the_default"),),
       stream="stdout", exit="user"),
    Op("setup.check", "setup", "the final check, then the launch card", cli=("setup --step",),
       tui=(_t("get-started.open", "card → W → check", ALWAYS,
               f"{T_GS}.StepViewTests.test_the_check_step_opens_the_launch_card"),),
       stream="stdout", exit="user"),
    Op("setup.answers", "setup", "apply a prepared setup (answers file)", audience="scripting",
       cli=("setup --answers",),
       absent={"tui": "a prepared answers file is a scripting input; Get started asks each step"},
       confirm="y/N, default No", stream="stdout", exit="user"),
    Op("setup.keys-file", "setup", "use an existing private key file for API keys", cli=("setup --keys-file",),
       absent={"tui": "the key file is chosen once per computer by its path, checked before it is used; "
                      "every TUI key prompt then writes to the file chosen"},
       stream="stdout", exit="user"),
    # ------------------------------------------------------------ install
    Op("install.identity", "install", "show the release identity (launcher, catalog, gateway, Claude Code)",
       cli=("--version",),
       absent={"tui": "a one-line report for issue reports; the card's U names an available update"},
       stream="stdout", exit="user"),
    Op("install.update-check", "install", "check whether a newer release exists", cli=("update --check",),
       tui=(_t("card.update", "card → U", "the update badge is shown",
               f"{T_CARD}.CardKeyTests.test_u_shows_what_update_will_do_only_while_the_badge_shows"),
            _t("resume-card.update", "claude-multi -r → U", "the update badge is shown",
               f"{T_DISPATCH}.ResumeCardDispatchTests.test_u_on_the_resume_card_shows_the_update")),
       stream="stdout", exit="user"),
    Op("install.update", "install", "update an installed release (plan, confirm, install)",
       cli=("update", "update --yes"),
       tui=(_t("card.update", "card → U", "an installed release with an update",
               f"{T_CARD}.CardKeyTests.test_u_on_an_installed_release_runs_the_update_journey"),),
       confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("install.update-nix", "install", "update the Nix package (the flake names the update)",
       cli=("update",),
       tui=(_t("card.update", "card → U", "the Nix package",
               f"{T_CARD}.CardKeyTests.test_u_on_the_nix_package_says_to_update_the_flake"),),
       stream="stdout", exit="user"),
    Op("install.update-local", "install", "update from a local directory or another location",
       audience="maintenance", cli=("update --from-dir", "update --base-url"),
       absent={"tui": "an explicit release source is a maintainer's input; U uses the release's own location"},
       confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("install.rollback", "install", "switch back to the previously installed version",
       cli=("update --rollback",), absent={"tui": INSTALL_TUI}, destructive=True,
       confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("install.uninstall-preview", "install", "show what uninstalling would remove",
       cli=("uninstall --dry-run",), absent={"tui": INSTALL_TUI}, stream="stdout", exit="user"),
    Op("install.uninstall", "install", "remove claude-multi (keeping what you choose)",
       cli=("uninstall", "uninstall --keep-setup", "uninstall --keep-credentials", "uninstall --force"),
       absent={"tui": INSTALL_TUI}, destructive=True, confirm="typed confirmation", stream="stdout",
       exit="user"),
    # ------------------------------------------------------------ the gateway process
    Op("gateway.status", "gateway", "the gateway's backend, endpoint, instance and hold",
       cli=("gateway status",),
       tui=(_t("providers.gateway", "card → G → W", "the gateway is not reachable",
               f"{T_CATALOG}.ProvidersScreenTests.test_w_opens_the_gateway_dialog_and_refreshes"),
            _t("card.doctor", "card → H", ALWAYS,
               f"{T_CARD}.CardKeyTests.test_h_offers_the_gateway_log_after_the_report")),
       stream="stdout", exit="user"),
    Op("gateway.start", "gateway", "start the gateway and wait until it is ready", cli=("gateway start",),
       tui=(_t("direct.gateway", "card → D → W → Start now", "the gateway is stopped",
               f"{T_DIRECT}.DirectGatewayDispatchTests.test_w_start_now_starts_a_stopped_gateway"),
            _t("providers.gateway", "card → G → W → Start now", "the gateway is stopped",
               f"{T_DISPATCH}.ProvidersGatewayDispatchTests.test_w_start_now_starts_a_stopped_gateway")),
       stream="stdout", exit="user"),
    Op("gateway.ensure", "gateway", "exit 0 only when the gateway runs and is ready (for scripts)",
       audience="scripting", cli=("gateway ensure", "gateway ensure --quiet"),
       absent={"tui": "the readiness contract of scripts and the token helper; every launch ensures it"},
       stream="stdout", exit="user"),
    Op("gateway.restart", "gateway", "stop, then start your gateway", cli=("gateway restart",),
       tui=(_t("card.doctor", "card → H → r", "the gateway is proven yours",
               f"{T_CARD}.CardKeyTests.test_h_then_r_restarts_a_proven_gateway"),),
       stream="stdout", exit="user"),
    Op("gateway.stop", "gateway", "stop your gateway", cli=("gateway stop",), absent={"tui": GATEWAY_STOP_TUI},
       stream="stdout", exit="user"),
    Op("gateway.logs", "gateway", "the newest gateway log lines", cli=("gateway logs",),
       tui=(_t("card.doctor", "card → H → l", ALWAYS,
               f"{T_CARD}.CardKeyTests.test_h_offers_the_gateway_log_after_the_report"),
            _t("providers.gateway", "card → G → W → Show log", "the gateway is not reachable",
               f"{T_DISPATCH}.ProvidersGatewayDispatchTests.test_w_show_log_opens_the_tail")),
       stream="stdout", exit="user"),
    Op("gateway.logs-instance", "gateway", "the log of one earlier gateway instance",
       audience="maintenance", cli=("gateway logs --instance",),
       absent={"tui": "historical inspection of an earlier instance by its id; the TUI shows the current "
                      "instance's log"},
       stream="stdout", exit="user"),
    Op("gateway.clear-hold", "gateway", "clear the persistence hold after verifying the credentials",
       cli=("gateway clear-hold",), absent={"tui": GATEWAY_CLEAR_HOLD_TUI}, destructive=True,
       confirm="typed confirmation", stream="stdout", exit="user"),
    # ------------------------------------------------------------ the gateway service
    Op("gateway-service.status", "gateway-service", "whether the supervised service is installed",
       cli=("gateway service status",),
       tui=(_t("providers.gateway", "card → G → W", "the gateway is not reachable",
               f"{T_DISPATCH}.ProvidersGatewayDispatchTests.test_w_names_the_service"),),
       stream="stdout", exit="user"),
    Op("gateway-service.install", "gateway-service", "install or refresh the supervised service",
       cli=("gateway service install",),
       tui=(_t("card.doctor", "card → H → i", "a user service manager and no service (or a stale one)",
               f"{T_CARD}.CardKeyTests.test_h_then_i_installs_the_gateway_service"),),
       stream="stdout", exit="user"),
    Op("gateway-service.uninstall", "gateway-service", "hand the gateway back to the on-demand start",
       cli=("gateway service uninstall",), absent={"tui": GATEWAY_SERVICE_UNINSTALL_TUI},
       stream="stdout", exit="user"),
    # ------------------------------------------------------------ providers
    Op("providers.list", "providers", "list every provider: source, on or off, connection, lines",
       cli=("providers list", "providers list --json"),
       tui=(_t("card.providers", "card → G", ALWAYS,
               f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),),
       stream="stdout", exit="user"),
    Op("providers.show", "providers", "one provider's details", cli=("providers show",),
       tui=(_t("providers.details", "card → G → Q", "a provider row",
               f"{T_DISPATCH}.ProvidersDispatchTests.test_q_shows_the_details"),),
       stream="stdout", exit="user"),
    Op("providers.resolved", "providers", "the resolved provider and lines as JSON", audience="scripting",
       cli=("providers show --resolved",), absent={"tui": MACHINE_TUI}, stream="stdout", exit="user"),
    Op("providers.add", "providers", "add a provider: your endpoint (by hand or from a preset)",
       cli=("providers add", "providers add --preset"),
       tui=(_t("providers.new", "card → G → N", ALWAYS,
               f"{T_DISPATCH}.ProvidersDispatchTests.test_n_opens_the_provider_picker"),
            _t("get-started.add-provider", "card → W → A", ALWAYS,
               f"{T_GS}.ProvidersStepTests.test_a_adds_your_own_endpoint_from_any_step"),
            _t("get-started.add-provider", "card → W → A → a preset", "a reviewed preset with an API key",
               f"{T_GS}.PresetPickerTests.test_enter_on_a_preset_adds_it_with_its_route_and_key"),
            _t("providers.new", "card → G → N → a preset", "a reviewed preset with an API key",
               f"{T_PROVIDERS}.PresetTests.test_n_adds_a_preset_with_its_route_and_key"),
            _t("providers.new", "card → G → N → a preset → another address",
               "a reviewed preset whose vendor documents another address",
               f"{T_PROVIDERS}.PresetTests.test_another_documented_address_is_declared_and_approved")),
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("providers.declare-only", "providers", "write an inert declaration without approving its route",
       audience="scripting", cli=("providers add --declare-only",),
       absent={"tui": "an unapproved declaration is a review step for scripts; the TUI declares and "
                      "approves in one flow"},
       stream="stdout", exit="user"),
    Op("providers.edit", "providers", "edit a provider you added", cli=("providers edit",),
       tui=(_t("providers.edit", "card → G → E", "a provider you added",
               f"{T_DISPATCH}.ProvidersDispatchTests.test_e_edits_a_provider_in_the_form"),),
       stream="terminal", exit="user"),
    Op("providers.approve", "providers", "approve a declared provider's credential route",
       cli=("providers approve",),
       tui=(_t("providers.primary", "card → G → Enter", "a provider you added whose route is not approved",
               f"{T_PROVIDERS}.OwnProviderTests.test_enter_approves_an_unapproved_route_then_asks_for_nothing_more"),),
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("providers.enable", "providers", "turn a provider on or off for new sessions",
       cli=("providers enable", "providers disable"),
       tui=(_t("providers.toggle", "card → G → Space", "a provider row",
               f"{T_CATALOG}.ProvidersScreenTests.test_space_toggles_enabled_in_settings"),),
       stream="stdout", exit="user"),
    Op("providers.remove", "providers", "remove a provider you added", cli=("providers rm",),
       tui=(_t("providers.remove", "card → G → X", "a provider you added that nothing uses",
               f"{T_PROVIDERS}.OwnProviderTests.test_removing_a_provider_offers_its_key_too"),),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("providers.apply", "providers", "make the gateway serve your changes", cli=("providers apply",),
       tui=(_t("providers.apply", "card → G → P", "the gateway does not serve the current setup",
               f"{T_PROVIDERS}.ApplyTests.test_p_applies_only_when_it_is_offered"),),
       stream="stdout", exit="user"),
    Op("providers.transport", "providers",
       "show or switch Anthropic or OpenAI between the account and an API key",
       cli=("providers transport",),
       tui=(_t("providers.primary", "card → G → Enter on Anthropic", "the Anthropic row",
               f"{T_PROVIDERS}.AnthropicTransportTests.test_enter_on_anthropic_switches_the_account_to_an_api_key"),
            _t("providers.primary", "card → G → Enter on OpenAI", "the OpenAI row, with a model reviewed for its key",
               f"{T_PROVIDERS}.OpenAITransportTests.test_enter_on_openai_switches_the_account_to_an_api_key"),
            _t("providers.primary", "card → G → Enter on OpenAI → its account", "OpenAI with its API key in use",
               f"{T_PROVIDERS}.OpenAITransportTests.test_switching_back_offers_removing_the_unused_key")),
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("providers.validate", "providers", "validate the declarations (or one candidate file)",
       audience="scripting", cli=("providers validate", "providers template"),
       absent={"tui": "declaration-file review for files written by hand; the TUI forms validate before "
                      "they write"},
       stream="stdout", exit="user"),
    Op("providers.migrate-custom", "providers", "move the legacy custom registry into provider files",
       audience="compatibility", cli=("providers migrate-custom", "providers migrate-custom --apply"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    # ------------------------------------------------------------ credentials and accounts
    Op("credentials.set-key", "credentials",
       "set or replace a provider's API key (a key other providers share is replaced for each of them, "
       "after a question naming them)",
       cli=("providers set-key", "providers set-key --yes"),
       tui=(_t("providers.set-key", "card → G → K", "a provider with an API key",
               f"{T_PROVIDERS}.KeyTests.test_k_replaces_only_on_an_explicit_choice"),
            _t("providers.primary", "card → G → Enter", "a provider whose key is missing",
               f"{T_PROVIDERS}.KeyTests.test_enter_on_a_missing_key_saves_it"),
            _t("providers.set-key", "card → G → K on OpenAI", "OpenAI with its API key in use",
               f"{T_PROVIDERS}.OpenAITransportTests.test_k_replaces_the_openai_key_in_use"),
            _t("providers.set-key", "card → G → K on Anthropic", "Anthropic's API key in use and not saved",
               f"{T_PROVIDERS}.SelectedTransportKeyTests.test_k_saves_the_missing_key_of_the_selected_transport"),
            _t("providers.set-key", "card → G → K on a shared key", "another provider uses the saved key",
               f"{T_PROVIDERS}.SharedKeyTests.test_k_names_every_provider_and_cancel_keeps_the_key_of_both")),
       confirm="y/N, default No", stream="stdout", exit="user"),
    Op("credentials.continue-to-models", "credentials",
       "after a key for a provider with no models, add one and optionally record an admission badge",
       cli=("models add", "models admit"),
       tui=(_t("providers.set-key", "card → G → K on a provider with no models → add models", "no model lines",
               f"{T_DISPATCH}.ZeroModelProviderTests.test_a_key_continues_into_adding_and_admitting_a_model"),),
       stream="stdout", exit="user"),
    Op("credentials.remove-key", "credentials", "remove a provider's saved API key",
       cli=("providers remove-key",),
       tui=(_t("providers.remove", "card → G → X", "a shipped provider with a key set",
               f"{T_PROVIDERS}.KeyTests.test_x_removes_a_shipped_providers_key_after_a_confirmation"),),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("credentials.remove-orphan-key", "credentials", "remove a key kept after its provider was removed",
       cli=("providers remove-key --name",),
       absent={"tui": "a key without a provider has no row to select; removing the provider (X) offers "
                      "its key at that moment"},
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("credentials.key-file-input", "credentials", "read a key from a private 0600 file",
       audience="scripting",
       cli=("providers set-key --secret-file", "providers add --secret-file", "providers transport --secret-file"),
       absent={"tui": "a key file is a scripting input; the TUI takes the key in a masked field"},
       stream="stdout", exit="user"),
    Op("credentials.preset-shared-key", "credentials",
       "one more provider from a preset whose API key another provider uses: a key of its own (the "
       "default), the saved key shared, or that key replaced for every provider using it",
       cli=("providers add --reuse-key", "providers add --replace-key"),
       tui=(_t("get-started.add-provider", "card → W → A → a preset → a key of its own",
               "another provider uses the preset's API key",
               f"{T_GS}.PresetPickerTests.test_one_more_instance_of_a_preset_gets_its_own_key"),
            _t("get-started.add-provider", "card → W → A → a preset → the saved key",
               "another provider uses the preset's API key and it is saved",
               f"{T_GS}.PresetPickerTests.test_sharing_the_saved_key_types_none"),
            _t("get-started.add-provider", "card → W → A → a preset → replace the saved key",
               "another provider uses the preset's API key",
               f"{T_GS}.PresetPickerTests.test_replacing_a_shared_key_is_confirmed_by_name_and_cancel_keeps_it"),
            _t("providers.new", "card → G → N → a preset → a key of its own",
               "another provider uses the preset's API key",
               f"{T_PROVIDERS}.PresetTests.test_n_gives_one_more_instance_its_own_key")),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("credentials.sign-in", "credentials", "sign in to a Claude or ChatGPT account (personal use)",
       cli=("providers sign-in",),
       tui=(_t("providers.primary", "card → G → Enter on an account provider not signed in", "an account provider",
               f"{T_PROVIDERS}.SignInFlowTests.test_a_wrong_word_changes_nothing_and_personal_signs_in"),),
       confirm="typed confirmation", stream="stdout", exit="user"),
    Op("credentials.sign-in-address", "credentials", "sign in by printing the address instead of a browser",
       cli=("providers sign-in --no-browser",),
       tui=(_t("get-started.add-provider", "card → W → A → an account (over SSH the address method is preselected)",
               "an account provider",
               f"{T_GS}.ProvidersStepTests.test_a_over_ssh_signs_in_to_an_account_by_its_address"),),
       confirm="typed confirmation", stream="stdout", exit="user"),
    Op("credentials.sign-out", "credentials", "sign out of an account (the records are kept as a backup)",
       cli=("providers sign-out",),
       tui=(_t("providers.remove", "card → G → X", "a signed-in account",
               f"{T_PROVIDERS}.AccountTests.test_x_on_a_signed_in_account_signs_out_through_the_layer"),),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("credentials.accounts", "credentials", "the saved accounts of an account provider",
       cli=("providers show",),
       tui=(_t("providers.sign-in", "card → G → L", "an account provider, whichever connection is active",
               f"{T_DISPATCH}.AccountInventoryTests.test_a_saved_account_stays_listed_after_a_key_switch"),),
       stream="stdout", exit="user"),
    Op("credentials.test", "credentials", "test providers with one small request each", cli=("providers test",),
       tui=(_t("providers.test", "card → G → T", "a connected provider",
               f"{T_PROVIDERS}.TestAndLetterTests.test_t_asks_once_and_a_no_sends_nothing"),),
       confirm="consent naming every request", stream="stdout", exit="user"),
    # ------------------------------------------------------------ models
    Op("models.list", "models", "the models: generation, provider, class, efforts, status",
       cli=("models", "models list", "models list --json"),
       tui=(_t("card.models", "card → M", ALWAYS, f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),),
       stream="stdout", exit="user"),
    Op("models.show", "models", "inspect one line: origin, selectors, efforts, context, admission",
       cli=("models show",),
       tui=(_t("models.primary", "card → M → Enter", "a catalog line",
               f"{T_DISPATCH}.ModelsDispatchTests.test_enter_inspects_a_catalog_line"),
            _t("models.details", "card → M → V", "a line",
               f"{T_DISPATCH}.ModelsDispatchTests.test_v_shows_the_full_details"),
            _t("binding-picker.details", "profile editor → Enter on an agent → V", "a model in the picker",
               f"{T_EDITOR}.PickerTests.test_v_shows_the_models_full_details")),
       stream="stdout", exit="user"),
    Op("models.evidence", "models", "the resolved entry or its evidence as JSON", audience="scripting",
       cli=("models show --resolved", "models show --evidence"), absent={"tui": MACHINE_TUI},
       stream="stdout", exit="user"),
    Op("models.candidates", "models", "advisory candidates from the registry and the gateway",
       cli=("models --candidates", "models --candidates --all"),
       tui=(_t("models.primary", "card → M → Enter on candidates", "the candidates row",
               f"{T_DISPATCH}.ModelsDispatchTests.test_a_candidate_opens_the_prefilled_declare_form"),),
       stream="stdout", exit="user"),
    Op("models.declare", "models", "declare a model of yours (New · not admitted)", cli=("models add",),
       tui=(_t("providers.add-models", "card → G → A → enter a model by hand", "a provider row",
               f"{T_DISPATCH}.ProvidersDispatchTests.test_a_offers_the_add_models_flow"),),
       stream="stdout", exit="user"),
    Op("models.declare-candidate", "models", "declare a candidate, its form prefilled",
       cli=("discover --add",),
       tui=(_t("models.primary", "card → M → Enter on candidates → Declare…", "an attributed candidate",
               f"{T_DISPATCH}.ModelsDispatchTests.test_a_candidate_opens_the_prefilled_declare_form"),),
       stream="stdout", exit="user"),
    Op("models.edit", "models", "edit a model you declared", cli=("models edit",),
       tui=(_t("models.edit", "card → M → E", "a model you declared",
               f"{T_DISPATCH}.ModelsDispatchTests.test_e_edits_a_declared_model"),),
       stream="terminal", exit="user"),
    Op("models.admit", "models", "record an optional local admission badge (zero inference)",
       cli=("models admit",),
       tui=(_t("models.primary", "card → M → Enter", "a New line",
               f"{T_CATALOG}.ModelsScreenTests.test_admit_writes_admitted_lines"),),
       confirm="y/N, default No", stream="stdout", exit="user"),
    Op("models.revoke", "models", "remove only the admission badge (use and evidence unchanged)",
       cli=("models revoke", "models revoke --yes"),
       tui=(_t("models.primary", "card → M → Enter", "an admitted line",
               f"{T_CATALOG}.ModelsScreenTests.test_new_row_admit_and_revoke"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("models.remove", "models", "remove a line you added", cli=("models rm",),
       tui=(_t("models.remove", "card → M → X", "a model you declared",
               f"{T_MODELS}.RemoveTests.test_an_unused_model_is_removed_after_a_confirmation"),),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("models.remove-successor", "models", "remove a line and rewrite what uses it to a successor",
       cli=("models rm --successor",),
       tui=(_t("models.remove", "card → M → X → choose a replacement", "a model a profile uses",
               f"{T_MODELS}.RemoveTests.test_where_profiles_use_it_a_replacement_is_chosen_first"),),
       destructive=True, confirm="y/N, default No", stream="stdout", exit="user"),
    Op("models.qualify", "models", "optional model diagnostics (consented, bounded checks)",
       cli=("models qualify", "models qualify --smoke", "models qualify --efforts", "models qualify --tools",
            "models qualify --stream", "models qualify --context", "models qualify --agents"),
       tui=(_t("models.qualify", "card → M → Q", "a model you declared",
               f"{T_DISPATCH}.ModelsDispatchTests.test_q_opens_the_qualification_form"),),
       confirm="consent naming every request", stream="stdout", exit="user"),
    # ------------------------------------------------------------ discovery
    Op("discovery.listing", "discovery", "list the models a provider offers (one consented request)",
       cli=("discover",),
       tui=(_t("providers.add-models", "card → G → A → list its models", "a provider with a listing",
               f"{T_DISPATCH}.ProvidersDispatchTests.test_a_offers_the_add_models_flow"),),
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("discovery.all", "discovery", "list every enabled provider's models", cli=("discover --all",),
       absent={"tui": "advanced discovery: every enabled provider in one consented pass; A lists one "
                      "provider at a time"},
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("discovery.feed", "discovery", "compare the public model feed with the pinned registry",
       cli=("discover --feed",),
       absent={"tui": "advanced discovery: a feed comparison for maintainers of the catalog"},
       confirm="consent naming every request", stream="stdout", exit="user"),
    Op("discovery.declare", "discovery", "declare listed models New · not admitted", cli=("discover --add",),
       tui=(_t("providers.add-models", "card → G → A → list → Space → Enter", "a listing was read",
               f"{T_DISPATCH}.ProvidersDispatchTests.test_a_offers_the_add_models_flow"),),
       stream="stdout", exit="user"),
    Op("discovery.context-override", "discovery", "declare a context above the listed one, with a reason",
       cli=("discover --over-listed",),
       absent={"tui": "a context above the listing needs a written justification; the TUI form refuses "
                      "raising a listed context"},
       stream="stdout", exit="user"),
    # ------------------------------------------------------------ profiles
    Op("profiles.list", "profiles", "list the profiles with origin and last use",
       cli=("profile list", "profile list --json"),
       tui=(_t("card.profiles", "card → P", ALWAYS,
               f"{T_CARD_ONB}.ProfilesKeyTests.test_p_opens_profiles_on_the_card_profile"),),
       stream="stdout", exit="user"),
    Op("profiles.show", "profiles", "a profile's evaluated lineup", cli=("profile show",),
       tui=(_t("card.details", "card → V", "the card shows a profile",
               f"{T_CARD}.CardNoticeTests.test_v_names_the_lead_set_the_kept_environment_and_managed_differences"),
            _t("resume-card.details", "claude-multi -r → V", "the resume card",
               f"{T_DISPATCH}.ResumeCardDispatchTests.test_v_shows_every_row_on_the_resume_card")),
       stream="stdout", exit="user"),
    Op("profiles.new", "profiles", "create a profile (from scratch or from another)",
       cli=("profile new", "profile new --keep-fallback"),
       tui=(_t("profiles.new", "card → P → N", ALWAYS,
               f"{T_PROFILES}.NewTests.test_from_a_shipped_profile_edits_an_unsaved_draft"),),
       stream="terminal", exit="user"),
    Op("profiles.copy", "profiles", "copy a profile", cli=("profile duplicate", "profile duplicate --keep-fallback"),
       tui=(_t("profiles.copy", "card → P → C", "a profile that can be loaded",
               f"{T_PROFILES}.CopyTests.test_copy_drops_the_fallback_unless_kept"),),
       stream="stdout", exit="user"),
    Op("profiles.edit", "profiles", "edit a profile and save it (or reload it after a conflict)",
       cli=("profile edit",),
       tui=(_t("profiles.edit", "card → P → E", ALWAYS,
               f"{T_PROFILES}.EditTests.test_e_opens_the_editor_with_the_loaded_digest"),
            _t("card.edit", "card → E", "a fresh card",
               f"{T_CARD}.CardBlockedTests.test_blocked_evaluation_e_opens_the_editor_on_the_lead_row"),
            _t("editor.change", "profile editor → Enter", "a row of the editor",
               f"{T_EDITOR}.EditorKeyTests.test_enter_on_an_agent_opens_the_picker_and_binds_the_effort"),
            _t("editor.unbind", "profile editor → U", "an agent row",
               f"{T_EDITOR}.EditorKeyTests.test_u_unbinds_an_agent_and_refuses_the_lead"),
            _t("binding-picker.choose", "profile editor → Enter on an agent → Enter", "a picker row",
               f"{T_EDITOR}.PickerTests.test_effort_cycling_stops_at_the_ends"),
            _t("binding-picker.effort", "profile editor → Enter on an agent → ← →", "a model with efforts",
               f"{T_EDITOR}.PickerTests.test_effort_cycling_stops_at_the_ends"),
            _t("editor.save", "profile editor → Ctrl-S", "the editor",
               f"{T_EDITOR}.EditorSaveTests.test_plain_save_writes_the_profile")),
       stream="terminal", exit="user"),
    Op("profiles.review-routing", "profiles", "preview where reviews route in a profile",
       cli=("profile show",),
       tui=(_t("editor.routing", "profile editor → R", "the editor",
               f"{T_EDITOR}.EditorKeyTests.test_r_and_enter_on_review_open_the_routing_preview"),),
       stream="stdout", exit="user"),
    Op("profiles.rename", "profiles", "rename a profile (the default and followers move with it)",
       cli=("profile rename",),
       tui=(_t("profiles.rename", "card → P → R", "a profile of yours",
               f"{T_PROFILES}.RenameTests.test_rename_without_followers_goes_straight_to_the_name"),),
       stream="stdout", exit="user"),
    Op("profiles.remove", "profiles", "remove a profile of yours (a copy is kept)",
       cli=("profile rm", "profile rm --yes"),
       tui=(_t("profiles.delete", "card → P → X", "a profile of yours",
               f"{T_PROFILES}.DeleteTests.test_delete_keeps_a_copy_and_clears_the_default"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("profiles.reseed", "profiles", "restore or update a shipped profile (your version is kept)",
       cli=("profile reseed", "profile reseed --yes"),
       tui=(_t("profiles.reseed", "card → P → U", "a shipped profile that changed",
               f"{T_PROFILES}.ReseedTests.test_an_edited_seed_shows_the_changes_and_keeps_your_version"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("profiles.fallback-filter", "profiles", "list only the fallback profiles", cli=("profile list",),
       tui=(_t("profiles.fallback", "card → P → F", ALWAYS, f"{T_PROFILES}.UseTests.test_f_lists_the_fallback_profiles"),),
       stream="stdout", exit="user"),
    Op("profiles.default", "profiles", "show or set the default profile", cli=("profile default",),
       tui=(_t("profiles.default", "card → P → D", "a profile that can be loaded",
               f"{T_PROFILES}.DefaultTests.test_d_on_a_connected_profile_makes_it_the_default"),
            _t("settings.edit", "card → O → default profile → Enter", ALWAYS,
               f"{T_CARD_ONB}.SettingsDefaultRowTests.test_enter_lists_automatic_first_then_the_profiles_and_chooses")),
       stream="stdout", exit="user"),
    Op("profiles.default-clear", "profiles", "go back to choosing the default automatically",
       cli=("profile default --clear",),
       tui=(_t("profiles.default", "card → P → D on the default", "the default profile",
               f"{T_PROFILES}.DefaultTests.test_d_on_the_default_clears_it"),
            _t("settings.reset", "card → O → default profile → R", "a default is set",
               f"{T_CARD_ONB}.SettingsDefaultRowTests.test_r_resets_to_automatic")),
       stream="stdout", exit="user"),
    Op("profiles.starter", "profiles", "preview and save a starter profile from what is connected",
       cli=("profile starter", "profile starter --apply"),
       tui=(_t("profiles.new", "card → P → N → starter", "a provider is connected",
               f"{T_PROFILES}.NewTests.test_starter_previews_saves_and_offers_the_default"),),
       stream="stdout", exit="user"),
    # ------------------------------------------------------------ managed sessions
    Op("sessions.list", "sessions", "list managed sessions", cli=("sessions list", "sessions list --json"),
       tui=(_t("card.sessions", "card → S", ALWAYS, f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),
            _t("resume-card.sessions", "claude-multi -r → S", "the resume card",
               f"{T_CARD}.CardSessionsKeyTests.test_s_on_the_resume_card_opens_the_sessions_screen")),
       stream="stdout", exit="user"),
    Op("sessions.filter", "sessions", "this directory's sessions or every directory's", cli=("sessions list",),
       tui=(_t("sessions.directories", "card → S → C", ALWAYS,
               f"{T_SESSIONS}.SessionsRowTests.test_c_toggles_this_directory_and_all_directories"),),
       stream="stdout", exit="user"),
    Op("sessions.show", "sessions", "a session's identity, directory, lineup, pending change and usage",
       cli=("sessions show",),
       tui=(_t("sessions.details", "card → S → V", "a session row",
               f"{T_RECOVERY}.SessionsDetailsTests.test_v_shows_identity_lineup_pending_and_unavailable_usage"),),
       stream="stdout", exit="user"),
    Op("sessions.rename", "sessions", "name a session (shown in the TUI)",
       tui=(_t("sessions.rename", "card → S → R", "a current-format session",
               f"{T_SESSIONS}.SessionsRenameTests.test_r_sets_and_clears_the_title"),),
       absent={"cli": "a session's name is a label of the TUI's list; commands take its id, a prefix or "
                      "the name"}),
    Op("sessions.forget", "sessions", "forget a session's record and scope (the transcript is kept)",
       cli=("sessions forget", "sessions forget --yes"),
       tui=(_t("sessions.forget", "card → S → X", "a session that is not running",
               f"{T_SESSIONS}.SessionsForgetTests.test_x_forgets_through_the_modal"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("sessions.force-forget", "sessions", "forget a session whose liveness cannot be determined",
       cli=("sessions forget --force",),
       tui=(_t("sessions.forget", "card → S → X → type the id", "liveness unknown",
               f"{T_RECOVERY}.UnknownLivenessTests.test_forget_needs_the_typed_id"),),
       destructive=True, confirm="typed session id when liveness is unknown", stream="stdout", exit="user"),
    Op("sessions.stop", "sessions", "stop a session running in the background", cli=("sessions stop",),
       tui=(_t("sessions.stop", "card → S → E", "a running session", f"{T_SESSIONS}.SessionsStopTests.test_e_stops_a_live_row"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    Op("sessions.force-stop", "sessions", "stop a session whose liveness cannot be determined",
       cli=("sessions stop --force",),
       tui=(_t("sessions.stop", "card → S → E → type the id", "liveness unknown",
               f"{T_RECOVERY}.UnknownLivenessTests.test_stop_needs_the_typed_id_and_sends_claude_stop"),),
       destructive=True, confirm="typed session id when liveness is unknown", stream="stdout", exit="user"),
    Op("sessions.mark-ended", "sessions", "record the end of a session that exited without one",
       cli=("sessions mark-ended",),
       tui=(_t("sessions.mark-ended", "card → S → M", "a session that exited without an end",
               f"{T_RECOVERY}.SessionsMarkEndedTests.test_m_marks_an_exited_session_ended"),),
       confirm="dialog with Cancel focused first", stream="stdout", exit="user"),
    Op("sessions.mark-ended-all", "sessions", "record the end of every session that exited without one",
       audience="maintenance", cli=("sessions mark-ended --all-dead",),
       absent={"tui": "bulk maintenance over every record; M marks one session after showing it"},
       stream="stdout", exit="user"),
    Op("sessions.repair", "sessions", "rebuild one session's generated scope", cli=("doctor --repair",),
       tui=(_t("sessions.repair", "card → S → P", "a current-format session",
               f"{T_RECOVERY}.SessionsRepairTests.test_p_repairs_through_the_doctor_operation"),),
       confirm="dialog with Cancel focused first", stream="stdout", exit="user"),
    Op("sessions.repair-all", "sessions", "rebuild every session's generated scope",
       cli=("doctor --repair-all", "doctor --repair-all --include-live"),
       tui=(_t("card.doctor", "card → H → repair all", "a finding names it",
               f"{T_CARD}.CardKeyTests.test_h_offers_a_bulk_repair_only_for_session_repair_findings"),),
       stream="stdout", exit="user"),
    Op("sessions.link", "sessions", "adopt a native session", cli=("sessions link",),
       tui=(_t("sessions.link", "card → S → L", "a native session here",
               f"{T_SESSIONS}.SessionsLinkTests.test_l_links_a_native_session_to_a_profile"),),
       stream="stdout", exit="user"),
    Op("sessions.relink-runtime", "sessions", "repair a record with the native runtime id",
       cli=("sessions relink-runtime",),
       tui=(_t("sessions.resume", "card → S → Enter → Relink", "the resume gate finds another runtime id",
               f"{T_SESSIONS}.ResumeGateModalTests.test_repair_modal_relinks_and_the_resume_prepares"),),
       stream="stdout", exit="user"),
    Op("sessions.relink-directory", "sessions", "point a session at its moved project directory",
       cli=("sessions relink-runtime --cwd",),
       tui=(_t("sessions.resume", "card → S → Enter → Relink to …", "the project directory moved",
               f"{T_RECOVERY}.RelinkGateTests.test_enter_on_a_session_whose_directory_moved_relinks_it_here"),
            _t("sessions.resume", "card → S → Enter → Relink to… → a directory",
               "the transcript is filed under a directory that cannot be told apart or no longer exists",
               f"{T_RECOVERY}.RelinkGateTests.test_enter_on_an_undecodable_transcript_relinks_it_here")),
       stream="stdout", exit="user"),
    Op("sessions.fork-adopt", "sessions", "adopt a native fork", cli=("sessions link",),
       tui=(_t("sessions.resume", "card → S → Enter on a forked session → Adopt", "a pending fork",
               f"{T_SESSIONS}.SessionsForkTests.test_adopt_links_the_fork_to_a_profile"),),
       stream="stdout", exit="user"),
    Op("sessions.fork-discard", "sessions", "stop tracking a native fork (its transcript is kept)",
       cli=("sessions resolve-fork", "sessions resolve-fork --yes"),
       tui=(_t("sessions.resume", "card → S → Enter on a forked session → Discard", "a pending fork",
               f"{T_SESSIONS}.SessionsForkTests.test_the_modal_names_the_fork_and_discard_clears_it"),),
       destructive=True, confirm="y/N, default No; --yes off a terminal", stream="stdout", exit="user"),
    # ------------------------------------------------------------ the lineup and pending changes
    Op("lineup.show", "lineup", "a session's lineup, pending change and windows", cli=("lineup",), cm=("show",),
       tui=(_t("sessions.details", "card → S → V", "a session row",
               f"{T_RECOVERY}.SessionsDetailsTests.test_v_shows_identity_lineup_pending_and_unavailable_usage"),),
       stream="stdout", exit="user"),
    Op("lineup.profiles", "lineup", "the profiles a session can switch to", cli=("lineup",), cm=("profiles",),
       tui=(_t("lineup-dialog.choose", "card → S → T → ← →", "the lineup dialog",
               f"{T_SESSIONS}.SessionsLineupTests.test_tab_cycles_the_targets_and_left_right_the_profiles"),),
       stream="stdout", exit="user"),
    Op("lineup.profile", "lineup", "follow another profile (live or at the next resume)", cli=("lineup",),
       cm=("profile",),
       tui=(_t("sessions.lineup", "card → S → T", "a current-format session",
               f"{T_SESSIONS}.SessionsLineupTests.test_t_from_the_screen_opens_the_dialog"),
            _t("lineup-dialog.apply", "card → S → T → Enter", "a previewed change",
               f"{T_SESSIONS}.SessionsLineupTests.test_apply_is_called_with_the_exact_lineup_args")),
       stream="stdout", exit="user"),
    Op("lineup.set", "lineup", "bind one agent", cli=("lineup",), cm=("set",),
       tui=(_t("lineup-dialog.pick", "card → S → T → Tab to agents → Space", "the lineup dialog",
               f"{T_SESSIONS}.SessionsLineupTests.test_per_agent_pick_builds_one_set_request"),),
       stream="stdout", exit="user"),
    Op("lineup.unset", "lineup", "unbind one agent", cli=("lineup",), cm=("unset",),
       tui=(_t("lineup-dialog.pick", "card → S → T → Tab to agents → unbind", "the lineup dialog",
               f"{T_SESSIONS}.SessionsLineupTests.test_per_agent_unset_builds_one_unset_request"),),
       stream="stdout", exit="user"),
    Op("lineup.direct", "lineup", "change to a lead with no agents", cli=("lineup",), cm=("direct",),
       tui=(_t("lineup-dialog.target", "card → S → T → Tab to direct", "the lineup dialog",
               f"{T_RECOVERY}.LineupTargetsTests.test_tab_to_direct_and_space_picks_the_lead"),),
       stream="stdout", exit="user"),
    Op("lineup.preview", "lineup", "preview a change's effect before anything is written", cli=("lineup",),
       cm=("fallback",),
       tui=(_t("sessions.lineup", "card → S → T", "a current-format session",
               f"{T_SESSIONS}.SessionsLineupTests.test_t_from_the_screen_opens_the_dialog"),),
       stream="stdout", exit="user"),
    Op("lineup.relaunch", "lineup", "record a change for the next resume, or relaunch now",
       cli=("lineup --relaunch", "lineup --its-exited"),
       tui=(_t("lineup-dialog.apply", "card → S → T → Enter (RELAUNCH)", "a change that needs a relaunch",
               f"{T_SESSIONS}.SessionsLineupTests.test_relaunch_request_is_recorded_pending_via_lineup_apply"),),
       stream="stdout", exit="user"),
    Op("lineup.pin", "lineup", "keep the current lineup and discard a pending change (Follow off)",
       cli=("lineup",), cm=("pin",),
       tui=(_t("lineup-dialog.target", "card → S → T → Keep current lineup — discard pending",
               "a pending change", f"{T_RECOVERY}.LineupTargetsTests.test_tab_to_keep_and_enter_applies_pin_semantics"),
            _t("sessions.follow", "card → S → F", "a session that follows its profile",
               f"{T_SESSIONS}.SessionsFollowTests.test_f_pins_a_following_session")),
       stream="stdout", exit="user"),
    Op("lineup.follow", "lineup", "follow the session's profile again, after a preview", cli=("lineup",),
       cm=("follow",),
       tui=(_t("sessions.follow", "card → S → F", "a pinned session",
               f"{T_SESSIONS}.SessionsFollowTests.test_f_follows_a_pinned_session"),),
       stream="stdout", exit="user"),
    Op("lineup.fallback", "lineup", "move the roles on one provider to a fallback lineup", cli=("lineup",),
       cm=("fallback",),
       tui=(_t("lineup-dialog.target", "card → S → T → Fallback → provider", "a provider with a fallback profile",
               f"{T_RECOVERY}.LineupTargetsTests.test_a_single_provider_lineup_falls_back_to_another_provider"),),
       stream="stdout", exit="user"),
    Op("lineup.propagate", "lineup", "apply a saved profile to its running sessions now or later",
       cli=("profile edit",),
       tui=(_t("editor.save", "profile editor → Ctrl-S → apply now / later", "running followers",
               f"{T_EDITOR}.ProfileEditEntryTests.test_ctrl_s_saves_and_offers_the_change_to_the_followers"),),
       stream="terminal", exit="user"),
    # ------------------------------------------------------------ diagnostics and observations
    Op("diagnostics.doctor", "diagnostics", "check this installation and its sessions",
       cli=("doctor", "doctor --verbose"),
       tui=(_t("card.doctor", "card → H", ALWAYS, f"{T_CARD}.CardKeyTests.test_h_runs_doctor_in_place_and_prints_its_sections"),
            _t("resume-card.doctor", "claude-multi -r → H", "the resume card",
               f"{T_DISPATCH}.ResumeCardDispatchTests.test_h_runs_doctor_on_the_resume_card")),
       stream="stdout", exit="user"),
    Op("diagnostics.first-run", "diagnostics", "the first-run checks in order, one fix each",
       cli=("doctor --first-run",),
       tui=(_t("card.doctor", "card → H", "nothing is connected yet",
               f"{T_CARD_ONB}.HealthKeyTests.test_h_shows_the_first_run_checks_while_nothing_is_connected"),),
       stream="stdout", exit="user"),
    Op("diagnostics.json", "diagnostics", "the structured report with its environment block",
       audience="scripting", cli=("doctor --json",), absent={"tui": MACHINE_TUI}, stream="stdout", exit="user"),
    Op("diagnostics.quota", "diagnostics", "account quota and credential health (one local read)",
       cli=("quota", "quota --json"), cm=("quota",),
       tui=(_t("card.providers", "card → G (the quota column and Q details)", "an account provider",
               f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),),
       stream="stdout", exit="user"),
    Op("diagnostics.explain", "diagnostics", "a session's recorded bindings and observed routing",
       cli=("explain",),
       tui=(_t("sessions.details", "card → S → V", "a session row",
               f"{T_RECOVERY}.SessionsDetailsTests.test_v_shows_identity_lineup_pending_and_unavailable_usage"),),
       stream="stdout", exit="user"),
    Op("diagnostics.usage", "diagnostics", "observed requests per model and session", cli=("usage",),
       tui=(_t("sessions.details", "card → S → V (the last 24 hours)", "a session row",
               f"{T_RECOVERY}.SessionsDetailsTests.test_v_shows_identity_lineup_pending_and_unavailable_usage"),),
       stream="stdout", exit="user"),
    Op("diagnostics.plan", "diagnostics", "what the gateway would serve after pending changes",
       cli=("plan",), absent={"tui": "served-change inspection for review; the provider and model flows "
                                     "show each change's effect before it is written"},
       stream="stdout", exit="user"),
    Op("diagnostics.plan-assets", "diagnostics", "what a candidate package's assets would serve",
       audience="maintenance", cli=("plan --assets",),
       absent={"tui": "candidate-package inspection for maintainers"}, stream="stdout", exit="user"),
    # ------------------------------------------------------------ settings and local choices
    Op("settings.inspect", "settings", "the launcher settings and their effect", absent={"cli": SETTINGS_CLI},
       tui=(_t("card.settings", "card → O", ALWAYS, f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),)),
    Op("settings.edit", "settings", "edit a setting (applies at the next launch or resume)",
       absent={"cli": SETTINGS_CLI},
       tui=(_t("settings.edit", "card → O → Enter", "an editable row",
               f"{T_CATALOG}.SettingsScreenTests.test_compaction_edit_recomputes_the_effective_row"),)),
    Op("settings.reset", "settings", "reset a setting to its default", absent={"cli": SETTINGS_CLI},
       tui=(_t("settings.reset", "card → O → R", "a row with a default",
               f"{T_CATALOG}.SettingsScreenTests.test_review_rounds_arrows_and_reset"),),
       destructive=True, confirm="dialog with Cancel focused first"),
    Op("settings.feedback-drafts", "settings", "offer or withhold Claude Code's feedback drafts",
       absent={"cli": SETTINGS_CLI},
       tui=(_t("settings.edit", "card → O → feedback drafts", ALWAYS,
               f"{T_CATALOG}.SettingsScreenTests.test_feedback_drafts_preference_cycles_resets_and_never_touches_settings"),)),
    Op("settings.kept-environment", "settings", "keep chosen API-key variables (names only) in sessions",
       absent={"cli": "a names-only Settings choice, validated against the providers' key names when it is "
                      "saved and at every launch; no general settings command is promised"},
       tui=(_t("settings.edit", "card → O → kept environment", ALWAYS,
               f"{T_CATALOG}.SettingsScreenTests.test_kept_environment_row_edits_refuses_and_resets_names_only"),)),
    Op("settings.ceiling-show", "settings", "show the context window ceiling (set or default)",
       cli=("window-ceiling",),
       tui=(_t("card.settings", "card → O → context window ceiling", ALWAYS,
               f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),),
       stream="stdout", exit="user"),
    Op("settings.ceiling-set", "settings", "set the context window ceiling (200K to 800K)",
       cli=("window-ceiling",),
       tui=(_t("settings.edit", "card → O → context window ceiling → Enter", "choices.json can be read",
               f"{T_WINDOW}.WindowCeilingSettingsScreenTests.test_edit_reset_and_refusal"),),
       stream="stdout", exit="user"),
    Op("settings.ceiling-reset", "settings", "reset the context window ceiling to its default",
       cli=("window-ceiling --reset",),
       tui=(_t("settings.reset", "card → O → context window ceiling → R → Reset", "a ceiling is set",
               f"{T_WINDOW}.WindowCeilingSettingsScreenTests.test_edit_reset_and_refusal"),),
       reversible=("the result names the ceiling it replaced, and claude-multi window-ceiling VALUE sets it "
                   "again; running sessions keep their window either way"),
       stream="stdout", exit="user"),
    Op("settings.role-windows", "settings", "each role's effective context window (the lineup summary)",
       cli=("lineup",), cm=("show",),
       tui=(_t("card.details", "card (the context row) → V", "a lineup",
               f"{T_CARD}.CardKeyTests.test_every_key_dispatches_and_the_keybar_lists_it"),),
       stream="stdout", exit="user"),
    Op("settings.role-windows-launch", "settings", "each role's effective context window in a launch plan",
       audience="scripting", cli=("--print-launch",),
       tui=(_t("card.launch", "card (the context row) → Enter", "a lineup",
               f"{T_CARD}.CardKeyTests.test_enter_on_ready_returns_the_prepared_perform_intent"),),
       stream="terminal", exit="user"),
    Op("settings.token-rotation", "settings", "rotate the local gateway token", cli=("doctor --rotate-token",),
       tui=(_t("settings.edit", "card → O → gateway token → Enter", ALWAYS,
               f"{T_CATALOG}.SettingsScreenTests.test_token_rotation_runs_on_the_screen_streams"),),
       confirm="dialog with Cancel focused first", stream="stdout", exit="user"),
    # ------------------------------------------------------------ named bindings
    Op("bindings.list", "bindings", "the named bindings and the profiles that use them",
       absent={"cli": BINDINGS_CLI},
       tui=(_t("profiles.bindings", "card → P → B", ALWAYS,
               f"{T_PROFILES}.NamedBindingsTests.test_b_opens_named_bindings_with_the_editor_store_access"),
            _t("editor.named-bindings", "profile editor → N", ALWAYS,
               f"{T_EDITOR}.NamedBindingsTests.test_the_editor_n_key_reloads_bindings"))),
    Op("bindings.create", "bindings", "add a named binding", absent={"cli": BINDINGS_CLI},
       tui=(_t("named-bindings.add", "card → P → B → A", ALWAYS, f"{T_EDITOR}.NamedBindingsTests.test_add_through_the_picker"),)),
    Op("bindings.edit", "bindings", "change a named binding (then apply it to the profiles using it)",
       absent={"cli": BINDINGS_CLI},
       tui=(_t("named-bindings.edit", "card → P → B → Enter", "a named binding",
               f"{T_EDITOR}.NamedBindingsTests.test_enter_edits_a_binding_through_the_picker"),)),
    Op("bindings.delete", "bindings", "delete a named binding nothing uses", absent={"cli": BINDINGS_CLI},
       tui=(_t("named-bindings.delete", "card → P → B → X", "an unused binding",
               f"{T_EDITOR}.NamedBindingsTests.test_delete_removes_an_unused_binding"),),
       destructive=True, confirm="dialog with Cancel focused first"),
    Op("bindings.select", "bindings", "bind an agent to a named binding", absent={"cli": BINDINGS_CLI},
       tui=(_t("binding-picker.choose", "profile editor → Enter on an agent → a named binding", "a named binding exists",
               f"{T_EDITOR}.PickerTests.test_the_zero_model_row_and_named_rows"),)),
    # ------------------------------------------------------------ portability
    Op("portability.export", "portability", "write your configuration as a portable file (no keys)",
       cli=("export", "export --out"), absent={"tui": PORTABLE_TUI}, stream="stdout", exit="user"),
    Op("portability.import-preview", "portability", "preview an exported configuration here",
       cli=("import",), absent={"tui": PORTABLE_TUI}, stream="stdout", exit="user"),
    Op("portability.import", "portability", "apply an exported configuration's ready items",
       cli=("import --apply",), absent={"tui": PORTABLE_TUI}, confirm="y/N, default No", stream="stdout",
       exit="user"),
    # ------------------------------------------------------------ maintenance
    Op("maintenance.prune-preview", "maintenance", "preview removing stale staging and orphan scopes",
       audience="maintenance", cli=("doctor --preview",), absent={"tui": MAINTENANCE_TUI},
       stream="stdout", exit="user"),
    Op("maintenance.prune", "maintenance", "remove stale staging and scopes whose records are gone",
       audience="maintenance", cli=("doctor --prune",), absent={"tui": MAINTENANCE_TUI},
       stream="stdout", exit="user"),
    Op("maintenance.prune-aliases", "maintenance", "remove continuity aliases no live session uses",
       audience="maintenance", cli=("doctor --prune-aliases",), absent={"tui": MAINTENANCE_TUI},
       stream="stdout", exit="user"),
    Op("management-key.rotate", "maintenance", "stage a new management key", audience="maintenance",
       proxy=("rotate-management-key",), absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("management-key.disable", "maintenance", "disable the management key (quota reads off)",
       audience="maintenance", proxy=("disable-management-key",),
       absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("auth.snapshot", "maintenance", "copy the gateway's auth directory aside", audience="maintenance",
       proxy=("snapshot-auth",), absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("auth.restore", "maintenance", "restore a copy of the auth directory (gateway stopped)",
       audience="maintenance", proxy=("snapshot-auth",), absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI},
       destructive=True, confirm="typed confirmation", exit="proxy"),
    Op("gateway.prepare", "maintenance", "prepare the gateway's configuration, key and directories",
       audience="service", proxy=("init",), absent={"tui": SERVICE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("gateway.reload-check", "maintenance", "verify the gateway's hot reload", audience="service",
       proxy=("init",), absent={"tui": SERVICE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("gateway.start-check", "maintenance", "wait until a (re)started gateway is ready", audience="service",
       proxy=("init",), absent={"tui": SERVICE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("gateway.adopt-root", "maintenance", "adopt another state root after its inventory",
       audience="maintenance", proxy=("init",), absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI},
       exit="proxy"),
    Op("gateway.run", "maintenance", "exec the gateway under its single-instance lock", audience="service",
       proxy=("run",), absent={"tui": SERVICE_TUI, "cli": SERVICE_TUI}, exit="proxy"),
    Op("gateway.health", "maintenance", "loopback health and the management key state",
       audience="maintenance", proxy=("status",), absent={"tui": MAINTENANCE_TUI, "cli": SERVICE_TUI},
       exit="proxy"),
    Op("account.sign-in", "maintenance", "the gateway's own sign-in commands (behind the acknowledgement)",
       audience="maintenance", proxy=("claude-login", "codex-device-login"),
       absent={"tui": "claude-multi providers sign-in (and L in Providers) is the sign-in a person uses",
               "cli": "claude-multi providers sign-in is the sign-in a person uses"},
       confirm="typed confirmation", exit="proxy"),
    # ------------------------------------------------------------ compatibility
    Op("compatibility.custom", "compatibility", "the legacy custom-model registry", audience="compatibility",
       cli=("custom list", "custom add-provider", "custom remove-provider", "custom add-model",
            "custom remove-model"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    Op("compatibility.profile-migrate", "compatibility", "convert legacy compositions to profiles",
       audience="compatibility",
       cli=("profile migrate", "profile migrate --dry-run", "profile migrate --apply"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    Op("compatibility.session-migrate", "compatibility", "convert legacy session records",
       audience="compatibility", cli=("migrate", "migrate --dry-run", "migrate --json"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    Op("compatibility.restore-check", "compatibility", "check what restore-2x would put back",
       audience="compatibility", cli=("restore-2x --check", "restore-2x --json"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    Op("compatibility.restore", "compatibility", "put back the legacy records a migration backed up",
       audience="compatibility", cli=("restore-2x",), absent={"tui": COMPAT_TUI}, destructive=True,
       confirm="y/N, default No", stream="stdout", exit="user"),
    Op("compatibility.aliases", "compatibility", "the earlier spellings (accepted, not shown)",
       audience="compatibility",
       cli=("compose list", "compose show", "compose delete", "compose duplicate", "compose rename",
            "compose restore-default", "compose use-as-template", "show", "sessions link --composition",
            "sessions link --model"),
       absent={"tui": COMPAT_TUI}, stream="stdout", exit="user"),
    Op("compatibility.aliases-interactive", "compatibility",
       "the earlier spellings of an editor, a launch or a relaunch (accepted, not shown)",
       audience="compatibility",
       cli=("compose new", "compose edit", "sessions transition", "sessions transition --its-exited",
            "--composition", "--composition-file"),
       absent={"tui": COMPAT_TUI}, stream="terminal", exit="user"),
    Op("compatibility.retired-flags", "compatibility", "a retired launch flag, parsed only to say what replaced it",
       audience="compatibility", cli=("--legacy",), absent={"tui": COMPAT_TUI}, stream="terminal", exit="user"),
    Op("compatibility.retired-approval", "compatibility",
       "a retired discovery approval flag, parsed only to say what replaced it", audience="compatibility",
       cli=("discover --yes-i-approve-this-provider-call",), absent={"tui": COMPAT_TUI}, stream="stdout",
       exit="user"),
    # ------------------------------------------------------------ in-session review
    Op("review.request", "review", "request a review, preferring a recognized different family", cm=("review",),
       absent={"cli": IN_SESSION, "tui": IN_SESSION}, exit="skill"),
    # ------------------------------------------------------------ the interface
    Op("interface.presentation", "interface", "plain lines instead of the full screen; no colour",
       cli=("--line", "--no-color"),
       absent={"tui": "they choose how the TUI itself is drawn"}, stream="terminal", exit="user"),
    Op("interface.hooks", "interface", "the managed sessions' lifecycle hooks", audience="internal",
       cli=("session-event",), absent={"tui": "Claude Code runs them; a person never does"}, stream="stdout",
       exit="hook"),
)

# Option-derived spellings are written "<command path> <flag>"; the root
# command's options stand alone ("--profile"). "--" is the passthrough
# boundary of the launches.
PASSTHROUGH = "--"

# Flags that modify an operation rather than name one: they need no row of
# their own (every other switch of the parser does).
MODIFIER_FLAGS: Mapping[str, str] = {
    "--json": "one machine-readable document",
    "--yes": "skip the y/N question (required off a terminal)",
    "-y": "skip the y/N question (required off a terminal)",
    "--line": "presentation: plain lines",
    "--no-color": "presentation: no colour",
    "-v": "presentation: more detail lines",
    "--verbose": "presentation: more detail lines",
    "--quiet": "presentation: nothing on success",
    "--help": "help",
    "-h": "help",
    "--version": "the release identity",
}

# Value-taking options that only supply an input of the operation their
# command already performs (that command's row classifies it), each with the
# input it supplies: the reviewed classification of every value-taking
# option no row spells. A value-taking option in neither a row nor here
# (an operation of its own, ``sessions list --archive-to FILE``, say) is
# unclassified.
_REGISTRY_MODEL = "an input of the model the earlier registry adds: its "
_REGISTRY_PROVIDER = "an input of the provider the earlier registry adds: its "
_DECLARED_MODEL = "an input of the model declared: its "
_DECLARED_PROVIDER = "an input of the provider declaration: its "
OPTION_ARGUMENTS: Mapping[str, str] = {
    "custom add-model --context": _REGISTRY_MODEL + "declared context",
    "custom add-model --display": _REGISTRY_MODEL + "display name",
    "custom add-model --provider": _REGISTRY_MODEL + "provider",
    "custom add-model --wire": _REGISTRY_MODEL + "wire model id",
    "custom add-provider --auth": _REGISTRY_PROVIDER + "key transport",
    "custom add-provider --base-url": _REGISTRY_PROVIDER + "base URL",
    "custom add-provider --display": _REGISTRY_PROVIDER + "display name",
    "custom add-provider --header": _REGISTRY_PROVIDER + "key header",
    "custom add-provider --secret-env": _REGISTRY_PROVIDER + "key variable name",
    "direct --model": "the lead of the direct session (without it a terminal opens the picker)",
    "discover --as": "the local key of a model the listing adds",
    "discover --context": "the declared context of a model the listing adds",
    "explain --agent": "narrows the explanation to one agent",
    "gateway ensure --base-url": "the gateway address the launcher ensures",
    "gateway ensure --max-wait": "how long ensure waits for the gateway",
    "gateway logs --lines": "how many log lines are shown",
    "gateway service install --name": "the name of the supervised unit",
    "lineup --session": "the session whose lineup is shown or changed",
    "models add --as": _DECLARED_MODEL + "local key",
    "models add --context": _DECLARED_MODEL + "declared context",
    "models add --default-effort": _DECLARED_MODEL + "default effort",
    "models add --display": _DECLARED_MODEL + "display name",
    "models add --effort": _DECLARED_MODEL + "efforts",
    "models add --source": _DECLARED_MODEL + "context source",
    "models add --source-ref": _DECLARED_MODEL + "context source reference",
    "models qualify --tool-choice": "the tool-choice mode the qualification checks",
    "profile new --from": "the profile the new one copies",
    "profile starter --name": "the name the starter is previewed and saved under",
    "providers add --as": _DECLARED_PROVIDER + "id for a preset",
    "providers add --auth": _DECLARED_PROVIDER + "key transport",
    "providers add --base-url": _DECLARED_PROVIDER + "base URL",
    "providers add --contracts": _DECLARED_PROVIDER + "payload contracts",
    "providers add --display": _DECLARED_PROVIDER + "display name",
    "providers add --family": _DECLARED_PROVIDER + "independence family",
    "providers add --header": _DECLARED_PROVIDER + "key header",
    "providers add --kind": _DECLARED_PROVIDER + "kind",
    "providers add --listing-auth": _DECLARED_PROVIDER + "model list authentication",
    "providers add --listing-shape": _DECLARED_PROVIDER + "model list shape",
    "providers add --listing-url": _DECLARED_PROVIDER + "model list URL",
    "providers add --secret-ref": _DECLARED_PROVIDER + "key reference (env:NAME)",
    "providers template --kind": "the kind of provider the template is for",
    "restore-2x --assume-dead": "sessions the restore treats as ended, recording their end",
    "restore-2x --not-running": "sessions the restore treats as exited",
    "session-event --hook-protocol": "the protocol of the lifecycle hook that reports the event",
    "session-event --launch-epoch": "the launch the lifecycle event belongs to",
    "session-event --managed-id": "the managed session the lifecycle event belongs to",
    "sessions link --cwd": "the directory the linked session is recorded under",
    "sessions link --direct": "the direct lead the linked session adopts",
    "sessions link --profile": "the profile the linked session adopts",
    "sessions transition --composition": "the profile (earlier spelling) the transition moves to",
    "usage --session": "narrows the count to one session",
    "usage --since": "the period the count covers",
}

# TUI actions that move between screens or within one, with what they do;
# every other action is an operation's mapping.
TUI_NAVIGATION: Mapping[str, str] = {
    "card.quit": "leave without launching",
    "resume-card.cancel": "go back without resuming",
    "lineup-dialog.cancel": "close without writing",
    "get-started.card": "go to the launch card",
    "get-started.profiles": "open Profiles",
    "direct.providers": "open Providers",
    "providers.refresh": "read the gateway again",
}
# Action verbs that are navigation on every screen.
NAVIGATION_VERBS = frozenset({"help", "back"})

# The operations the product must offer, independent of the rows (an
# operation with no implementation is still looked for).
REQUIRED: tuple[str, ...] = (
    "launch.choose-profile", "launch.direct", "launch.fresh", "launch.continue", "launch.resume",
    "launch.force-resume", "launch.profile-file", "launch.print-launch", "launch.passthrough",
    "launch.no-subagents", "launch.save-direct",
    "setup.status", "setup.run", "setup.redo", "setup.preflight", "setup.claude", "setup.claude-from",
    "setup.gateway", "setup.providers", "setup.test", "setup.profile", "setup.check", "setup.answers",
    "setup.keys-file", "setup.proxy",
    "install.identity", "install.update-check", "install.update", "install.update-nix", "install.update-local",
    "install.rollback", "install.uninstall-preview", "install.uninstall",
    "gateway.status", "gateway.start", "gateway.ensure", "gateway.restart", "gateway.stop", "gateway.logs",
    "gateway.logs-instance", "gateway.clear-hold",
    "gateway-service.status", "gateway-service.install", "gateway-service.uninstall",
    "providers.list", "providers.show", "providers.resolved", "providers.add", "providers.declare-only",
    "providers.edit", "providers.approve", "providers.enable", "providers.remove", "providers.apply",
    "providers.transport", "providers.validate", "providers.migrate-custom",
    "credentials.set-key", "credentials.continue-to-models", "credentials.remove-key",
    "credentials.remove-orphan-key", "credentials.key-file-input", "credentials.preset-shared-key",
    "credentials.sign-in",
    "credentials.sign-in-address", "credentials.sign-out", "credentials.accounts", "credentials.test",
    "models.list", "models.show", "models.evidence", "models.candidates", "models.declare",
    "models.declare-candidate", "models.edit", "models.admit", "models.revoke", "models.remove",
    "models.remove-successor", "models.qualify",
    "discovery.listing", "discovery.all", "discovery.feed", "discovery.declare", "discovery.context-override",
    "profiles.list", "profiles.show", "profiles.new", "profiles.copy", "profiles.edit", "profiles.rename",
    "profiles.remove", "profiles.reseed", "profiles.fallback-filter", "profiles.default",
    "profiles.default-clear", "profiles.starter",
    "sessions.list", "sessions.filter", "sessions.show", "sessions.rename", "sessions.forget",
    "sessions.force-forget", "sessions.stop", "sessions.force-stop", "sessions.mark-ended",
    "sessions.mark-ended-all", "sessions.repair", "sessions.repair-all", "sessions.link",
    "sessions.relink-runtime", "sessions.relink-directory", "sessions.fork-adopt", "sessions.fork-discard",
    "lineup.show", "lineup.profiles", "lineup.profile", "lineup.set", "lineup.unset", "lineup.direct",
    "lineup.preview", "lineup.relaunch", "lineup.pin", "lineup.follow", "lineup.fallback", "lineup.propagate",
    "diagnostics.doctor", "diagnostics.first-run", "diagnostics.json", "diagnostics.quota",
    "diagnostics.explain", "diagnostics.usage", "diagnostics.plan", "diagnostics.plan-assets",
    "settings.inspect", "settings.edit", "settings.reset", "settings.feedback-drafts",
    "settings.kept-environment", "settings.ceiling-show", "settings.ceiling-set", "settings.ceiling-reset",
    "settings.role-windows", "settings.token-rotation",
    "bindings.list", "bindings.create", "bindings.edit", "bindings.delete", "bindings.select",
    "portability.export", "portability.import-preview", "portability.import",
    "maintenance.prune-preview", "maintenance.prune", "maintenance.prune-aliases", "management-key.rotate",
    "management-key.disable", "auth.snapshot", "auth.restore", "gateway.prepare", "gateway.run",
    "compatibility.custom", "compatibility.profile-migrate", "compatibility.session-migrate",
    "compatibility.restore-check", "compatibility.restore", "compatibility.aliases",
    "review.request",
    "interface.presentation", "interface.hooks",
)


def rows() -> dict[str, Op]:
    """The operations by id (in :data:`OPERATIONS` order)."""

    return {op.id: op for op in OPERATIONS}


def split_endpoint(spelling: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``"providers remove-key --name"`` -> ``(("providers", "remove-key"),
    ("--name",))``; ``"--profile"`` -> ``((), ("--profile",))``; ``""`` ->
    the bare command."""

    words = spelling.split()
    return (tuple(word for word in words if not word.startswith("-")),
            tuple(word for word in words if word.startswith("-")))


def _bad_reason(reason: str) -> str | None:
    text = reason.strip()
    if len(text) < 12:
        return "too short to be a reason"
    lowered = f" {text.lower()} "
    for word in UNREASONS:
        if f" {word} " in lowered or f" {word}." in lowered or f" {word}," in lowered:
            return f"leans on {word!r}"
    if re.search(r"(?<![\w.])\d{3}(?![\w.])", text):
        return "points at a numbered work item"
    return None


def census(
    operations: Iterable[Op],
    *,
    parser_leaves: Iterable[tuple[str, ...]],
    parser_flags: Mapping[tuple[str, ...], Mapping[str, tuple[bool, str]]],
    proxy_commands: Mapping[str, tuple[str, ...]],
    proxy_usage: str,
    cm_verbs: Iterable[str],
    tui_actions: Iterable[str],
    required: Iterable[str] = REQUIRED,
    fixture_check=None,
    stream_of=None,
    option_arguments: Mapping[str, str] | None = None,
) -> list[str]:
    """Every disagreement between the rows and the surfaces discovered.

    ``parser_leaves``: every command path of the argument parser that runs
    something (``()`` is the bare command); ``parser_flags``: per command
    path, each option string with whether it is a switch (takes no value)
    and its canonical spelling (the spellings of one option share it);
    ``proxy_commands``: name -> the operation ids its descriptor names;
    ``proxy_usage``: the gateway tool's overall usage text; ``cm_verbs``:
    the /cm verbs; ``tui_actions``: every action id. ``fixture_check``
    (dotted test id, action id -> None, or why that test is not a dispatch
    fixture of that action: it does not exist, never drives the action's
    screen, never feeds its key to the input driver, asserts nothing after
    the press) and ``stream_of`` (command path, flag ->
    "stdout"/"terminal") check the fixtures and the stream table when given.
    ``option_arguments`` (default :data:`OPTION_ARGUMENTS`) classifies the
    value-taking options no row spells; every other option needs a row.
    """

    ops = list(operations)
    option_arguments = OPTION_ARGUMENTS if option_arguments is None else option_arguments
    problems: list[str] = []
    seen: set[str] = set()
    for op in ops:
        where = f"row {op.id}"
        if op.id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(op.id)
        if op.object not in OBJECTS:
            problems.append(f"{where}: unknown object {op.object!r}")
        if op.audience not in AUDIENCES:
            problems.append(f"{where}: unknown audience {op.audience!r}")
        if not op.does:
            problems.append(f"{where}: says nothing about what it does")
        surfaces = op.surfaces()
        if not surfaces and not op.absent:
            problems.append(f"{where}: neither a surface nor a reason")
        for surface, reason in op.absent.items():
            if surface not in SURFACES:
                problems.append(f"{where}: an absence reason for an unknown surface {surface!r}")
            elif surface in surfaces:
                problems.append(f"{where}: both a {surface} surface and a reason for its absence")
            bad = _bad_reason(reason)
            if bad is not None:
                problems.append(f"{where}: the {surface} absence reason {bad}")
        if op.audience == "user" and op.cli and not op.tui and "tui" not in op.absent:
            problems.append(f"{where}: a user command with neither a TUI path nor its reason")
        if op.audience == "user" and op.tui and not op.cli and not op.cm and "cli" not in op.absent:
            problems.append(f"{where}: a TUI-only operation without the reason it has no command")
        if op.destructive and (op.confirm is None or op.confirm not in CONFIRMS):
            problems.append(f"{where}: destructive without its confirmation")
        if op.reversible is not None:
            if op.destructive:
                problems.append(f"{where}: destructive and reversible at once")
            bad = _bad_reason(op.reversible)
            if bad is not None:
                problems.append(f"{where}: the reversible reason {bad}")
        if op.confirm is not None and op.confirm not in CONFIRMS:
            problems.append(f"{where}: unknown confirmation {op.confirm!r}")
        if op.cli:
            if op.stream not in STREAMS:
                problems.append(f"{where}: a command without its stream class")
            if op.exit not in EXITS:
                problems.append(f"{where}: a command without its exit category")
        if op.proxy and op.exit != "proxy":
            problems.append(f"{where}: a gateway-tool command keeps the tool's own exit statuses")
        if op.cm and not op.cli and op.exit != "skill":
            problems.append(f"{where}: a /cm-only operation keeps the skill transport's exits")
        for mapping in op.tui:
            for name in ("action", "path", "available", "fixture"):
                if not getattr(mapping, name):
                    problems.append(f"{where}: a TUI mapping without its {name}")
            if mapping.fixture and mapping.action and fixture_check is not None:
                why = fixture_check(mapping.fixture, mapping.action)
                if why is not None:
                    problems.append(f"{where}: TUI fixture {mapping.fixture} {why}")
    by_id = {op.id: op for op in ops}
    for needed in required:
        if needed not in by_id:
            problems.append(f"required operation {needed}: no row")
    # The argument parser, both ways.
    leaves = {tuple(leaf) for leaf in parser_leaves}
    claimed_paths: set[tuple[str, ...]] = set()
    claimed_flags: set[tuple[tuple[str, ...], str]] = set()
    for op in ops:
        for spelling in op.cli:
            path, flags = split_endpoint(spelling)
            claimed_paths.add(path)
            if path not in leaves:
                problems.append(f"row {op.id}: {spelling!r} names no command")
                continue
            for flag in flags:
                if flag == PASSTHROUGH and path in ((), ("direct",)):
                    continue
                if flag not in parser_flags.get(path, {}):
                    problems.append(f"row {op.id}: {spelling!r} names no option of that command")
                else:
                    claimed_flags.add((path, parser_flags[path][flag][1]))
            if stream_of is not None and op.stream in STREAMS:
                actual = stream_of(path, flags[0] if flags else None)
                if actual is not None and actual != op.stream:
                    problems.append(f"row {op.id}: {spelling!r} writes to {actual}, the row says {op.stream}")
    for leaf in sorted(leaves - claimed_paths):
        problems.append(f"command {' '.join(leaf) or '(bare)'!s}: no row classifies it")
    # Every option, switch or value-taking: a row spells it, it is a
    # presentation/confirmation modifier, or (a value-taking one) the
    # reviewed classification names the input it supplies.
    classified = set(option_arguments)
    for path, flags in sorted(parser_flags.items()):
        unclaimed = sorted({canonical for flag, (switch, canonical) in flags.items()
                            if flag not in MODIFIER_FLAGS and canonical not in MODIFIER_FLAGS
                            and (path, canonical) not in claimed_flags
                            and (switch or " ".join((*path, canonical)) not in classified)})
        for canonical in unclaimed:
            problems.append(f"option {' '.join((*path, canonical))}: no row classifies it")
    known_options = {" ".join((*path, canonical)) for path, flags in parser_flags.items()
                     for _flag, (switch, canonical) in flags.items() if not switch}
    for spelling in sorted(classified - known_options):
        problems.append(f"option {spelling}: classified as an input, but no command takes it")
    # The gateway tool's command table.
    claimed_proxy = {name for op in ops for name in op.proxy}
    for name, operation_ids in sorted(proxy_commands.items()):
        if name not in claimed_proxy:
            problems.append(f"claude-multi-proxy {name}: no row classifies it")
        if f"  {name}" not in proxy_usage:
            problems.append(f"claude-multi-proxy {name}: missing from its usage")
        for operation_id in operation_ids:
            op = by_id.get(operation_id)
            if op is None:
                problems.append(f"claude-multi-proxy {name}: operation {operation_id} has no row")
            elif name not in op.proxy:
                problems.append(f"claude-multi-proxy {name}: row {operation_id} does not list it")
    for name in sorted(claimed_proxy - set(proxy_commands)):
        problems.append(f"claude-multi-proxy {name}: a row names a command the tool does not have")
    # The /cm verbs.
    verbs = set(cm_verbs)
    claimed_cm = {verb for op in ops for verb in op.cm}
    for verb in sorted(verbs - claimed_cm):
        problems.append(f"/cm {verb}: no row classifies it")
    for verb in sorted(claimed_cm - verbs):
        problems.append(f"/cm {verb}: a row names a verb /cm does not have")
    # The TUI actions.
    actions = set(tui_actions)
    claimed_tui = {mapping.action for op in ops for mapping in op.tui}
    for action in sorted(claimed_tui - actions):
        problems.append(f"TUI action {action}: a row names an action no screen offers")
    for action in sorted(actions - claimed_tui):
        verb = action.split(".", 1)[-1]
        if verb in NAVIGATION_VERBS or action in TUI_NAVIGATION:
            continue
        problems.append(f"TUI action {action}: no row classifies it")
    for action in sorted(set(TUI_NAVIGATION) - actions):
        problems.append(f"TUI action {action}: classified as navigation but no screen offers it")
    return problems


def document() -> dict[str, object]:
    """The rows as one JSON-ready document, in their order (the source of
    the documentation's task tables and their TUI path column)."""

    return {
        "schema_version": 1,
        "objects": list(OBJECTS),
        "operations": [
            {
                "id": op.id, "object": op.object, "does": op.does, "audience": op.audience,
                "cli": [f"claude-multi {spelling}".rstrip() for spelling in op.cli],
                "proxy": [f"claude-multi-proxy {name}" for name in op.proxy],
                "cm": [f"/cm {verb}" for verb in op.cm],
                "tui": [{"action": t.action, "path": t.path, "available": t.available} for t in op.tui],
                "absent": dict(sorted(op.absent.items())),
                "destructive": op.destructive, "confirm": op.confirm, "reversible": op.reversible,
                "stream": op.stream, "exit": op.exit,
            }
            for op in OPERATIONS
        ],
    }


__all__ = [
    "AUDIENCES", "CONFIRMS", "EXITS", "MODIFIER_FLAGS", "NAVIGATION_VERBS", "OBJECTS", "OPERATIONS",
    "OPTION_ARGUMENTS", "Op", "PASSTHROUGH", "REQUIRED", "STREAMS", "SURFACES", "TUI_NAVIGATION", "Tui", "census", "document", "rows",
    "split_endpoint",
]
