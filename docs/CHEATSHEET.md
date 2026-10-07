# claude-multi cheatsheet

One page of tasks, keys and files, then symptom → command. The full pages
are listed in [USAGE.md](USAGE.md); [troubleshooting.md](troubleshooting.md)
has the longer symptom tables.

Placeholders: `<id>` is the full managed session id, a UUID (inside a
session `$CLAUDE_MULTI_MANAGED_ID`; outside, `claude-multi doctor -v`
prints a `Session <id>` line per session, with `→ runtime <runtime-id>`
when the two differ; `claude-multi sessions list` shows only the first 8
characters, and commands also accept an 8+ character prefix or the
session's name). `<runtime-id>` is the id Claude Code runs the session
under, `runtime_session_id` in `claude-multi sessions show <id>` (`/cm`
passes it itself). `<name>` is a profile, `<model>` a model line
(`claude-multi models`), `<agent>` an agent id such as `implementer`,
`<provider>` a provider id (`claude-multi providers list`).

## Commands at a glance

<!-- generated: cheatsheet-tasks (tools/docs_gen.py) -->

| Task | TUI (from the card) | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| get started | W | `claude-multi setup --status`; `claude-multi setup` | not a `/cm` request | the steps not done yet, in order |
| launch | Enter; Tab | `claude-multi`; `claude-multi --profile <name>` | not a `/cm` request | the card shows the profile it launches |
| launch one model as the lead | D | `claude-multi direct [--model <model>]` | not a `/cm` request | no `cm-*` agents |
| continue, resume | S → Enter | `claude-multi -c`; `claude-multi -r [<id>]` | not a `/cm` request | resume keeps the lineup |
| see a launch without launching | not in the launcher: a dry run for scripts and issue reports; the card shows the plan before Enter | `claude-multi --print-launch` | not a `/cm` request | the gateway key is never shown |
| connect a provider | G → K, Enter on an account provider not signed in | `claude-multi providers set-key <provider>`; `claude-multi providers sign-in anthropic\|openai` | not a `/cm` request | [providers](providers/api-keys.md) |
| test a provider | G → T | `claude-multi providers test <provider>…` | not a `/cm` request | one request each, after you agree; may be billed |
| profiles | P; V; S → T → ← → | `claude-multi profile list`; `claude-multi profile show [<name>]` | `/cm profiles` | P is the Profiles screen, not provider fallback |
| fallback profiles for an outage | P → F; S → T → Fallback → provider | `claude-multi profile list`; `claude-multi lineup --session <runtime-id> fallback <provider> [--preview]` | `/cm fallback <provider> [--preview]` | one provider each |
| make a profile the default | P → D | `claude-multi profile default [<name>]`; `claude-multi profile default [<name>] --clear` | not a `/cm` request | otherwise chosen automatically from what is connected |
| a starter from what is connected | P → N → starter | `claude-multi profile starter`; `claude-multi profile starter --apply` | not a `/cm` request | previews before saving |
| create, copy, edit, rename | P → N, C, E, R | `claude-multi profile new <name> [--from <profile>]`; `claude-multi profile duplicate <name> <new>`; `claude-multi profile edit <name>`; `claude-multi profile rename <name> <new>` | not a `/cm` request | saving offers the change to following sessions |
| remove, restore a shipped profile | P → X, U | `claude-multi profile rm <name>`; `claude-multi profile reseed <name>` | not a `/cm` request | your version is kept as a backup |
| named bindings | P → B | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | a binding names a model and effort profiles share |
| change the lineup | S → V, T | `claude-multi lineup --session <runtime-id> show`; `claude-multi lineup --session <runtime-id> set <agent>=<model>[:<effort>]`; `claude-multi lineup --session <runtime-id> profile <name>` | `/cm show`; `/cm set <agent>=<model>[:<effort>]`; `/cm profile <name>` | then `/reload-plugins` |
| follow or pin | S → T → Keep current lineup — discard pending; S → F | `claude-multi lineup --session <runtime-id> pin`; `claude-multi lineup --session <runtime-id> follow` | `/cm pin`; `/cm follow` | a following session takes its profile's edits |
| models | M | `claude-multi models`; `claude-multi models show <line>` | not a `/cm` request | lines, selectors, retired keys |
| add a model of your own | G → A → list its models; M → Enter, Q | `claude-multi discover <provider>`; `claude-multi models admit <line>`; `claude-multi models qualify <line> --agents` | not a `/cm` request | [guides/models.md](guides/models.md) |
| context window ceiling | O → context window ceiling; card (the context row) → V | `claude-multi window-ceiling`; `claude-multi window-ceiling <ceiling>`; `claude-multi window-ceiling --reset`; `claude-multi lineup --session <runtime-id> show` | `/cm show` | applies at the next launch or resume |
| settings | O | no command: an interactive, validated editor of local choices; no general settings command is promised | not a `/cm` request | [reference/settings.md](reference/settings.md) |
| sessions | S | `claude-multi sessions list`; `claude-multi sessions show <id>` | not a `/cm` request | records, never transcripts |
| stop, mark ended, forget | S → E, M, X | `claude-multi sessions stop <id>`; `claude-multi sessions mark-ended <id>`; `claude-multi sessions forget <id>` | not a `/cm` request | transcripts are never touched |
| forks and adoption | S → Enter on a forked session → Adopt, Discard | `claude-multi sessions link <fork-id> --profile <name>`; `claude-multi sessions resolve-fork <id> <fork-id>` | not a `/cm` request | the fork transcript is kept either way |
| health | H; S → P | `claude-multi doctor`; `claude-multi doctor --first-run`; `claude-multi doctor --repair <id>` | not a `/cm` request | Ready / Attention / BLOCKED |
| quota | G (the quota column and Q details) | `claude-multi quota` | `/cm quota` | an observation, not a balance |
| review by another model family | not in the launcher: it runs inside a managed session, where the lead asks for it | no command: it runs inside a managed session, where the lead asks for it | `/cm review [high-stakes] [<range>]` | report only |
| the gateway | G → W; D → W → Start now; H → r, l | `claude-multi gateway status`; `claude-multi gateway start`; `claude-multi gateway restart`; `claude-multi gateway logs` | not a `/cm` request | [guides/gateway.md](guides/gateway.md) |
| the gateway as a user service (Linux) | H → i; G → W | `claude-multi gateway service install`; `claude-multi gateway service status` | not a `/cm` request | unit `claude-multi-gateway` |
| update | U | `claude-multi update --check`; `claude-multi update` | not a `/cm` request | shows its plan, then asks |
| roll back, uninstall | not in the launcher: it replaces or removes the running installation, so it runs from a terminal where the launcher starts again cleanly | `claude-multi update --rollback`; `claude-multi uninstall --dry-run` | not a `/cm` request | [update.md](update.md), [uninstall.md](uninstall.md) |
| export, import | not in the launcher: portable-file input and output: the file is written for, or read from, another computer | `claude-multi export --out <file>`; `claude-multi import <file>` | not a `/cm` request | no keys travel |

<!-- end of generated: cheatsheet-tasks -->

Lineup changes: LIVE (agents only, inside the session's fence) apply after
you type `/reload-plugins`; RELAUNCH changes are recorded (↻) and apply at
the next `claude-multi -r <id>`.

`/cm` grammar: `/cm show · /cm profiles · /cm profile <name> · /cm set <agent>=<model>[:<effort>] · /cm unset <agent> · /cm direct [<model>[:<effort>]] · /cm pin · /cm follow · /cm fallback <provider> [--preview] · /cm review [high-stakes] [<range>] · /cm quota`

### Keys

<!-- generated: cheatsheet-keys (tools/docs_gen.py) -->

| Screen | Keys |
| --- | --- |
| launch card (`claude-multi`) | **Enter** launch the profile on the card · **U** update claude-multi (shown when an update applies) · **W** open Get started · **E** edit the profile on the card · **Tab** show the next profile (Shift-Tab: the previous one) · **P** open Profiles · **D** open Direct · **V** every card row in full, the /model lead set and the kept environment · **S** open Sessions · **G** open Providers · **M** open Models · **O** open Settings · **H** run doctor here, then the gateway's actions · **?** this screen's help · **Esc** leave without launching |
| resume card (`claude-multi -r <id>`) | **Enter** resume the session with the lineup shown · **U** update claude-multi (shown when an update applies) · **V** every card row in full, the /model lead set and the kept environment · **S** open Sessions · **H** run doctor here, then the gateway's actions · **?** this screen's help · **Esc** go back without resuming |
| sessions (S) | **Enter** resume the selected session · **V** identity, directory, lineup, pending change, drift and usage · **T** change the session's lineup, with its effect first · **F** follow the session's profile or pin its lineup · **R** rename the session (shown here only) · **X** forget the record and its generated scope · **E** stop a session running in the background · **M** record the end of a session that exited without one · **P** rebuild the session's generated scope · **L** manage a native session · **C** this directory or all directories · **?** this screen's help · **Esc** back |
| lineup dialog (S → T) | **Enter** apply the previewed change · **Tab** move to the next kind of change · **← →** cycle the profiles or fallback providers · **Space** list the choices of the current target · **?** this screen's help · **Esc** close without writing |
| direct (D) | **Enter** launch the model as a direct session · **Tab** save the choice as a lead-only profile · **G** open Providers · **← →** choose the effort of a gateway-effort model · **W** start the local gateway or read its log · **?** this screen's help · **Esc** back |
| providers (G) | **Enter** the row's main action: set a key, sign in, connect, approve or details · **P** make the gateway serve your changes · **K** set or replace an API key · **X** remove the key, the sign-in or a provider you added · **L** sign in or out of an account provider · **T** test the provider · **A** add models to the provider · **Space** turn the provider on or off · **Q** the provider's details · **E** edit a provider you added · **N** add a provider · **R** read the gateway again · **W** start the local gateway or read its log · **?** this screen's help · **Esc** back |
| models (M) | **Enter** inspect a line or add/remove its optional admission badge · **Q** run optional diagnostics with explicit consent · **E** edit a model you declared · **X** remove a model you declared · **V** the line's details · **?** this screen's help · **Esc** back |
| settings (O) | **Enter** edit the value, open the screen it summarizes or rotate the token · **R** reset the value to its default (Cancel is focused first) · **?** this screen's help · **Esc** back |
| profiles (P) | **Enter** use the profile for this launch · **N** create a profile · **E** edit the profile (or its file when it cannot be loaded) · **C** copy the profile · **R** rename the profile · **X** delete the profile (a copy is kept) · **U** update a shipped profile, or restore one that cannot be loaded · **D** make the profile the default · **F** list only the fallback profiles · **B** edit the named bindings · **W** open Get started · **?** this screen's help · **Esc** back |
| get started (W) | **Enter** open the selected step · **A** add a provider · **P** open Profiles · **?** this screen's help · **Esc** go to the launch card |
| profile editor (E) | **Enter** change the selected row · **U** unbind the selected agent · **N** edit the named bindings · **R** show the review routing · **^S** save the profile · **?** this screen's help · **Esc** back |
| binding picker (editor → Enter on an agent) | **← →** choose the effort · **Enter** bind the model, use a named binding or turn the slot off · **V** the model's details · **?** this screen's help · **Esc** back |
| named bindings (P → B) | **Enter** change the binding's model or effort · **A** add a named binding · **X** delete the named binding · **?** this screen's help · **Esc** back |

Marks on the sessions screen: ● running · ◐ unknown (no end event, no process seen) · ○ ended · ↻ relaunch change recorded · ⚠ fork waiting · ! needs a choice. On the settings screen: profile overrides marked ◆.

<!-- end of generated: cheatsheet-keys -->

Inside a session: `/model` offers the lead set only (press `s` to change it for this session only), `/cm` shows or changes the lineup and `/reload-plugins` applies a change. In the profile editor: “^S saves (^O too); only a valid profile saves. ^G edits the raw JSON in $EDITOR.”

### Files

| Path | What |
|---|---|
| `~/.config/claude-multi/` | `profiles/`, `bindings.json`, `settings.json`, `choices.json` (default profile, kept environment, sign-in confirmations, window ceiling), `preferences.json`, `providers.d/`, `endpoint.json`, `continuity.json`; `api-key`, `previous-key`, `config.yaml` and `management-key*` are secrets: names only |
| `~/.config/claude-multi/secrets/provider-keys.env` | the default API-key file (see [the key file](reference/settings.md#the-api-key-file)) |
| `~/.local/state/claude-multi/sessions/` | session records |
| `~/.local/state/claude-multi/scopes/<id>/` | each session's generated files (never edit) |
| `~/.local/state/claude-multi/lineup-log/<id>.log` | one line per agent start and lineup change (the binding, not proof of the served model) |
| `~/.local/state/claude-multi/gateway/` | the gateway's working directory and logs |
| `~/.local/share/claude-multi/` | releases (`install/`), the Claude Code copy (`claude/`), sign-in records (secrets) |
| `~/.claude/projects/` | transcripts: Claude Code's own; claude-multi never reads or deletes them |

## Troubleshooting (symptom → command)

The first command is the one to try. Text in curly quotes is what
claude-multi prints. A held gateway comes first: when doctor reports a
gateway credential save that failed or is unconfirmed, keep the gateway
running and follow [the persistence hold](guides/gateway.md#the-persistence-hold)
before any restart.

### Gateway

| You see | Do |
|---|---|
| doctor: “local gateway: <error> — <fix>”, or status stopped; no session connects | `claude-multi gateway start`; if it does not come up, `claude-multi gateway logs` shows why it stopped, then `claude-multi gateway status` |
| doctor: “local gateway key file: <error>” | follow the printed remedy: the key file must be a regular file you own, mode 0600; a missing key: `claude-multi-proxy init` |
| doctor: “gateway did not reload the current render (sentinel missing) — restart required” | between turns, `claude-multi gateway restart`, then `claude-multi doctor` |
| doctor: “the on-disk gateway config differs from a fresh render of the installed catalog” | `claude-multi providers apply` (re-renders and verifies the reload) |
| doctor: “the running gateway does not serve rendered selector”, or “more unserved rendered selectors” | `claude-multi gateway restart` between turns |
| doctor: “the running gateway rejected the local token (401 on /v1/models): token mismatch” | `claude-multi-proxy init` (verifies the reload); still 401: `claude-multi gateway restart` |
| doctor: “gateway key slots unusable” | `claude-multi doctor --rotate-token` in a terminal |
| doctor: “gateway continuity set unreadable” | move `~/.config/claude-multi/continuity.json` aside, then `claude-multi-proxy init` (it re-seeds the set) |
| doctor: “gateway managed for root <A>; this command used <B>” | run with the managed state directory (unset or correct `XDG_STATE_HOME`) |
| “the gateway is inhibited by <owner> (…)” | wait for that installer, update or service hand-off; an interrupted one: run the command after `fix:` |
| “the local gateway is not set up yet” | `claude-multi setup --step gateway` |
| doctor: “gateway working directory <path> holds a .env file” | move that `.env` aside, then `claude-multi gateway restart` |
| why did the gateway stop; what did it serve | `claude-multi gateway logs` (keys, tokens and account identifiers redacted; the log files themselves are not: never paste them publicly); `claude-multi doctor` summarizes errors per model without account details |
| want it supervised (Linux) | `claude-multi gateway service install`; back to on demand: `claude-multi gateway service uninstall` |
| one provider's agents stall; doctor counts 429, 402 or 403 for a model | `/cm fallback <provider>`, then `/reload-plugins`; new launches: P → F |
| HTTP 400 `prompt_cache_retention is not supported on this model` | the running gateway predates the release: `claude-multi gateway restart` between turns |

### Providers and models

| You see | Do |
|---|---|
| a provider is not connected | `claude-multi providers set-key <provider>`, or `claude-multi providers sign-in anthropic` (or `openai`) |
| doctor counts `invalid_grant` since a sign-in | sign in again |
| “needs a terminal outside Claude Code sessions” | run the command yourself in a terminal outside Claude Code |
| “… changed while you were deciding — nothing written” | run the command again and check its new preview |
| `providers rm <id> --yes`: the key was kept | `claude-multi providers remove-key <provider>` (with `--name`, as printed) |
| a keyed OpenAI-compatible provider: “keyed generic openai-compatible routes are unavailable in this build: their compatibility audit gate is closed” | this build has a closed audit; 1.0.0 offers the route. Use an audited release, an Anthropic-compatible endpoint or OpenRouter ([providers/openai-compatible.md](providers/openai-compatible.md)) |
| doctor: “gateway substitution:” | the provider served another model; rebind the agent if it matters |
| Anthropic or OpenAI on its API key: doctor “<name> is not set — claude-multi providers set-key” | save the key: `claude-multi providers set-key <provider>`, or G → K on the provider; or back to the account: `claude-multi providers transport <provider> oauth-pool` ([providers/api-keys.md](providers/api-keys.md#openai)) |
| doctor: “the <choice> transport of <provider> is not approved for the current …” or “its selectors are not served” | `claude-multi providers transport <provider> api-key` again (terminal), or back: `claude-multi providers transport <provider> oauth-pool` |
| a preset: “not available in this release” | this build has a closed OpenAI-compatible audit; the five keyed presets are available in 1.0.0. Use an audited release, the vendor's Anthropic-compatible endpoint or OpenRouter |
| a spending or usage limit reached at the provider | raise or wait out the limit in its console ([costs per provider](providers/api-keys.md#6-costs-and-limits)) |

### Lineups and `/cm`

| You see | Do |
|---|---|
| /cm: “next: type /reload-plugins” | type `/reload-plugins`; start agents fresh |
| “pending relaunch change” (↻) | after it exits, `claude-multi -r <id>`; or now: `claude-multi lineup --session <runtime-id> --relaunch profile <name>` |
| /model: “is not in this session's lead set” | relaunch with a profile whose lead is that model: `claude-multi lineup --session <runtime-id> --relaunch profile <name>` |
| doctor: “holds the claude-multi selector” | remove the key doctor names from `~/.claude/settings.json`; in `/model` press `s` instead of Enter |
| doctor: “which the compiled fence does not admit” | `claude-multi doctor --repair <id>` |
| `/cm` fails: `claude-multi` not found | `claude-multi` must be on the session's PATH |
| /cm fallback: “no complete fallback profile is available” | create a profile with that primary provider binding every role the session binds |
| doctor: “may shadow the session's /cm skill” | rename that `cm` skill |

### Sessions

| You see | Do |
|---|---|
| ⚠; doctor: “has an unresolved native fork” | Enter on the row (adopt or discard), or `claude-multi sessions resolve-fork <id> <fork-id>` |
| !; doctor: “identity is repair-needed” | `claude-multi -r <id>`, or `claude-multi sessions relink-runtime <id> <runtime-id>` |
| ● or ◐ but the session is gone | `claude-multi sessions mark-ended <id>` (all: `claude-multi sessions mark-ended --all-dead`) |
| doctor: “scope differs from the record-authoritative compile”, “scope unreadable” or “scope(s) diverged from record authority” | `claude-multi doctor --repair <id>`; all stopped sessions: `claude-multi doctor --repair-all` |
| doctor: “compiled hook target” | `claude-multi doctor --repair <id>` |
| doctor: “session record <id> is unreadable” | `claude-multi sessions show <id>` names the error; never edit the record |
| doctor: “session record directory is unavailable; records are unknown” | the state directory must be yours, mode 0700; fix it, then `claude-multi doctor` |
| doctor: “collides with the managed cm-* namespace” | rename or remove that project agent, or launch from another directory |
| resume says the session does not exist | `claude-multi -r <id>` |
| a session is missing from the list | C on the sessions screen (all directories) |

### Launch, Claude Code, settings

| You see | Do |
|---|---|
| “is not set up for claude-multi” or “does not match the verified sha256” | `claude-multi setup --step claude` |
| doctor: “managed Claude binary: <error>” | `claude-multi setup --step claude` |
| doctor: “runs Claude” and “not the pinned” | `/exit`, then `claude-multi -r <id>` |
| doctor: “the contract override is invalid and was IGNORED” | remove the file doctor names |
| doctor: “state marker:” | use the newer claude-multi the message names |
| “profile is blocked:” at launch | `claude-multi profile show <name>`, then `claude-multi profile edit <name>` |
| doctor: “named bindings:” | fix it in P → B |
| doctor: “leaves out the loopback addresses while a proxy is set” | add `127.0.0.1,localhost,::1` to `NO_PROXY` and `no_proxy` in the settings file doctor names |
| doctor or a launch: “ask the policy owner, or use plain claude” | a Claude Code managed policy blocks managed sessions; ask its owner |
| an MCP server misses its API key | add its name in O → kept environment |

### Install, update, uninstall

| You see | Do |
|---|---|
| the update badge on the card | `U`, or `claude-multi update` |
| “this account's claude-multi state belongs to the <channel> installation” | use that installation, or follow the printed fix |
| an install or update was interrupted | `sh install.sh --repair`, or run `claude-multi update` again |
| uninstall refuses: sessions may still run | end them (`claude-multi -r` shows ● and ◐), then run it again |
