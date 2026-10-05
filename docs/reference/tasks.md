# Tasks by surface

Every task claude-multi offers, and where you reach it: the launcher's
screens (TUI), the command line, and `/cm` inside a session. When a task
has no form on one surface, the table says why.

<!-- generated: tasks (tools/docs_gen.py) -->

## The launcher's screens

| Key on the card | What it does |
| --- | --- |
| Enter | launch the profile on the card |
| U | update claude-multi (shown when an update applies) |
| W | open Get started |
| E | edit the profile on the card |
| Tab | show the next profile (Shift-Tab: the previous one) |
| P | open Profiles |
| D | open Direct |
| V | every card row in full, the /model lead set and the kept environment |
| S | open Sessions |
| G | open Providers |
| M | open Models |
| O | open Settings |
| H | run doctor here, then the gateway's actions |
| ? | this screen's help |
| Esc | leave without launching |

## Launch

In full: [quickstart.md](../quickstart.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| launch the profile shown on the card | card → Enter: the card's profile can launch | `claude-multi` | not a `/cm` request | asks nothing |
| choose the profile to launch | card → Tab: more than one profile; card → P → Enter: a profile that can be loaded | `claude-multi --profile <name>` | not a `/cm` request | asks nothing |
| launch one model as the lead (choose the model and its effort) | card → D; card → D → Enter: a lead-capable model is offered; card → D → ← →: a model whose gateway serves several efforts | `claude-multi direct [--model <model>]` | not a `/cm` request | asks nothing |
| continue the last session in this directory | card → S → Enter on this directory's newest session: a session here | `claude-multi -c`; `claude-multi direct -c` | not a `/cm` request | asks nothing |
| select a managed session and resume it | card → S → Enter: a managed session that is not running; claude-multi -r → Enter: the resume card | `claude-multi -r [<id>]`; `claude-multi direct -r [<id>]` | not a `/cm` request | asks nothing |
| resume despite a background-liveness marker | card → S → Enter → Resume anyway: the background-liveness gate | `claude-multi -r <id> --force`; `claude-multi direct -r <id> --force` | not a `/cm` request | asks nothing |
| launch an unsaved profile file (or stdin) | not in the launcher: an unsaved profile document is a file or stdin input for scripts; the TUI launches and edits saved profiles | `claude-multi --profile-file <path>` | not a `/cm` request | for scripts; asks nothing |
| print what a launch would run and whether it would succeed | not in the launcher: a dry run for scripts and issue reports; the card shows the plan before Enter | `claude-multi --print-launch` | not a `/cm` request | for scripts; asks nothing |
| print what a direct launch would run | not in the launcher: a dry run for scripts and issue reports; Direct shows the model before Enter | `claude-multi direct --print-launch` | not a `/cm` request | for scripts; asks nothing |
| pass arguments to Claude Code unchanged | not in the launcher: arguments for Claude Code are a command-line input; the card launches without any | `claude-multi -- <claude-args>`; `claude-multi direct -- <claude-args>` | not a `/cm` request | for scripts; asks nothing |
| hard-deny subagent delegation for a direct session | not in the launcher: the deny is fixed at launch and recorded for every resume, so it is chosen where the launch is spelled out; Direct launches with delegation allowed, and a lead-only profile is the TUI's session without agents | `claude-multi direct --no-subagents` | not a `/cm` request | asks nothing |
| save a direct choice as a lead-only profile | card → D → Tab: a catalog model (not one you declared) | `claude-multi profile new <name>` | not a `/cm` request | asks nothing |

## Setup

In full: [quickstart.md](../quickstart.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| show the setup steps and what is done | card → W; card → P → W | `claude-multi setup --status` | not a `/cm` request | asks nothing |
| run the steps not done yet, in order | card → W → Enter on the first open step: a step not done | `claude-multi setup` | not a `/cm` request | asks nothing |
| run one step again, done or not | card → W → Enter on any step | `claude-multi setup --step <step>`; `claude-multi setup --redo` | not a `/cm` request | asks nothing |
| check this computer before anything is installed | card → W → this computer | `claude-multi setup --step preflight` | not a `/cm` request | asks nothing |
| acquire and verify claude-multi's own copy of the pinned Claude Code | card → W → Claude Code | `claude-multi setup --step claude` | not a `/cm` request | asks nothing |
| install the pinned Claude Code from a local file | not in the launcher: a local copy of the pinned build is a file input for offline machines; the Claude Code step downloads and verifies the same copy | `claude-multi setup --step claude --claude-from <path>` | not a `/cm` request | asks nothing |
| set up and start the local gateway | card → W → local gateway | `claude-multi setup --step gateway` | not a `/cm` request | asks nothing |
| set or clear the gateway's outbound proxy | card → W → local gateway → outbound proxy | `claude-multi setup --step gateway --proxy <url>`; `claude-multi setup --step gateway --no-proxy` | not a `/cm` request | asks nothing |
| connect providers (a key, a sign-in or your own endpoint) | card → W → providers; card → W → A | `claude-multi setup --step providers` | not a `/cm` request | asks nothing |
| test the connected providers (one consented request each) | card → W → test: a provider is connected | `claude-multi setup --step test` | not a `/cm` request | asks first: consent naming every request |
| choose or build the profile to use (a starter from what is connected) | card → W → profile: a provider is connected | `claude-multi setup --step profile` | not a `/cm` request | asks nothing |
| the final check, then the launch card | card → W → check | `claude-multi setup --step check` | not a `/cm` request | asks nothing |
| apply a prepared setup (answers file) | not in the launcher: a prepared answers file is a scripting input; Get started asks each step | `claude-multi setup --answers <file>` | not a `/cm` request | for scripts; asks first: y/N, default No |
| use an existing private key file for API keys | not in the launcher: the key file is chosen once per computer by its path, checked before it is used; every TUI key prompt then writes to the file chosen | `claude-multi setup --keys-file <path>` | not a `/cm` request | asks nothing |

## Install, update and uninstall

In full: [update.md](../update.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| show the release identity (launcher, catalog, gateway, Claude Code) | not in the launcher: a one-line report for issue reports; the card's U names an available update | `claude-multi --version` | not a `/cm` request | asks nothing |
| check whether a newer release exists | card → U: the update badge is shown; claude-multi -r → U: the update badge is shown | `claude-multi update --check` | not a `/cm` request | asks nothing |
| update an installed release (plan, confirm, install) | card → U: an installed release with an update | `claude-multi update`; `claude-multi update --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| update the Nix package (the flake names the update) | card → U: the Nix package | `claude-multi update` | not a `/cm` request | asks nothing |
| update from a local directory or another location | not in the launcher: an explicit release source is a maintainer's input; U uses the release's own location | `claude-multi update --from-dir <dir>`; `claude-multi update --base-url <url>` | not a `/cm` request | maintenance; asks first: y/N, default No; --yes off a terminal |
| switch back to the previously installed version | not in the launcher: it replaces or removes the running installation, so it runs from a terminal where the launcher starts again cleanly | `claude-multi update --rollback` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| show what uninstalling would remove | not in the launcher: it replaces or removes the running installation, so it runs from a terminal where the launcher starts again cleanly | `claude-multi uninstall --dry-run` | not a `/cm` request | asks nothing |
| remove claude-multi (keeping what you choose) | not in the launcher: it replaces or removes the running installation, so it runs from a terminal where the launcher starts again cleanly | `claude-multi uninstall`; `claude-multi uninstall --keep-setup`; `claude-multi uninstall --keep-credentials`; `claude-multi uninstall --force` | not a `/cm` request | asks first: typed confirmation |

## The local gateway

In full: [guides/gateway.md](../guides/gateway.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| the gateway's backend, endpoint, instance and hold | card → G → W: the gateway is not reachable; card → H | `claude-multi gateway status` | not a `/cm` request | asks nothing |
| start the gateway and wait until it is ready | card → D → W → Start now: the gateway is stopped; card → G → W → Start now: the gateway is stopped | `claude-multi gateway start` | not a `/cm` request | asks nothing |
| exit 0 only when the gateway runs and is ready (for scripts) | not in the launcher: the readiness contract of scripts and the token helper; every launch ensures it | `claude-multi gateway ensure`; `claude-multi gateway ensure --quiet` | not a `/cm` request | for scripts; asks nothing |
| stop, then start your gateway | card → H → r: the gateway is proven yours | `claude-multi gateway restart` | not a `/cm` request | asks nothing |
| stop your gateway | not in the launcher: a launch never needs it and it is refused while the persistence hold is active; run claude-multi gateway stop in a terminal | `claude-multi gateway stop` | not a `/cm` request | asks nothing |
| the newest gateway log lines | card → H → l; card → G → W → Show log: the gateway is not reachable | `claude-multi gateway logs` | not a `/cm` request | asks nothing |
| the log of one earlier gateway instance | not in the launcher: historical inspection of an earlier instance by its id; the TUI shows the current instance's log | `claude-multi gateway logs --instance <nonce>` | not a `/cm` request | maintenance; asks nothing |
| clear the persistence hold after verifying the credentials | not in the launcher: it needs a typed confirmation in a terminal outside Claude Code: claude-multi gateway clear-hold | `claude-multi gateway clear-hold` | not a `/cm` request | asks first: typed confirmation |

## The gateway service

In full: [guides/gateway.md](../guides/gateway.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| whether the supervised service is installed | card → G → W: the gateway is not reachable | `claude-multi gateway service status` | not a `/cm` request | asks nothing |
| install or refresh the supervised service | card → H → i: a user service manager and no service (or a stale one) | `claude-multi gateway service install` | not a `/cm` request | asks nothing |
| hand the gateway back to the on-demand start | not in the launcher: it reverses a deliberate choice and changes the service manager; run claude-multi gateway service uninstall in a terminal | `claude-multi gateway service uninstall` | not a `/cm` request | asks nothing |

## Providers

In full: [providers/api-keys.md](../providers/api-keys.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| list every provider: source, on or off, connection, lines | card → G | `claude-multi providers list`; `claude-multi providers list --json` | not a `/cm` request | asks nothing |
| one provider's details | card → G → Q: a provider row | `claude-multi providers show <provider>` | not a `/cm` request | asks nothing |
| the resolved provider and lines as JSON | not in the launcher: a machine-readable report for scripts and issue reports; the screens show the same facts | `claude-multi providers show <provider> --resolved` | not a `/cm` request | for scripts; asks nothing |
| add a provider: your endpoint (by hand or from a preset) | card → G → N; card → W → A; card → W → A → a preset: a reviewed preset with an API key; card → G → N → a preset: a reviewed preset with an API key; card → G → N → a preset → another address: a reviewed preset whose vendor documents another address | `claude-multi providers add <provider> --base-url <url> --secret-ref <secret-ref>`; `claude-multi providers add --preset <preset> [--as <provider>]` | not a `/cm` request | asks first: consent naming every request |
| write an inert declaration without approving its route | not in the launcher: an unapproved declaration is a review step for scripts; the TUI declares and approves in one flow | `claude-multi providers add [<provider>] --declare-only` | not a `/cm` request | for scripts; asks nothing |
| edit a provider you added | card → G → E: a provider you added | `claude-multi providers edit <provider>` | not a `/cm` request | asks nothing |
| approve a declared provider's credential route | card → G → Enter: a provider you added whose route is not approved | `claude-multi providers approve <provider>` | not a `/cm` request | asks first: consent naming every request |
| turn a provider on or off for new sessions | card → G → Space: a provider row | `claude-multi providers enable <provider>`; `claude-multi providers disable <provider>` | not a `/cm` request | asks nothing |
| remove a provider you added | card → G → X: a provider you added that nothing uses | `claude-multi providers rm <provider>` | not a `/cm` request | asks first: y/N, default No |
| make the gateway serve your changes | card → G → P: the gateway does not serve the current setup | `claude-multi providers apply` | not a `/cm` request | asks nothing |
| show or switch Anthropic or OpenAI between the account and an API key | card → G → Enter on Anthropic: the Anthropic row; card → G → Enter on OpenAI: the OpenAI row, with a model reviewed for its key; card → G → Enter on OpenAI → its account: OpenAI with its API key in use | `claude-multi providers transport <provider> [oauth-pool\|api-key]` | not a `/cm` request | asks first: consent naming every request |
| validate the declarations (or one candidate file) | not in the launcher: declaration-file review for files written by hand; the TUI forms validate before they write | `claude-multi providers validate [<file>]`; `claude-multi providers template` | not a `/cm` request | for scripts; asks nothing |

## Keys and sign-ins

In full: [providers/api-keys.md](../providers/api-keys.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| set or replace a provider's API key (a key other providers share is replaced for each of them, after a question naming them) | card → G → K: a provider with an API key; card → G → Enter: a provider whose key is missing; card → G → K on OpenAI: OpenAI with its API key in use; card → G → K on Anthropic: Anthropic's API key in use and not saved; card → G → K on a shared key: another provider uses the saved key | `claude-multi providers set-key <provider>`; `claude-multi providers set-key <provider> --yes` | not a `/cm` request | asks first: y/N, default No |
| after a key for a provider with no models, add and admit one before the starter | card → G → K on a provider with no models → add models: no model lines | `claude-multi models add <provider> <wire> --context <n> --source docs\|operator\|registry`; `claude-multi models admit <line>` | not a `/cm` request | asks nothing |
| remove a provider's saved API key | card → G → X: a shipped provider with a key set | `claude-multi providers remove-key <provider>` | not a `/cm` request | asks first: y/N, default No |
| remove a key kept after its provider was removed | not in the launcher: a key without a provider has no row to select; removing the provider (X) offers its key at that moment | `claude-multi providers remove-key <provider> --name <key-name>` | not a `/cm` request | asks first: y/N, default No |
| read a key from a private 0600 file | not in the launcher: a key file is a scripting input; the TUI takes the key in a masked field | `claude-multi providers set-key <provider> --secret-file <file>`; `claude-multi providers add --preset <preset> --secret-file <file>`; `claude-multi providers transport <provider> api-key --secret-file <file>` | not a `/cm` request | for scripts; asks nothing |
| one more provider from a preset whose API key another provider uses: a key of its own (the default), the saved key shared, or that key replaced for every provider using it | card → W → A → a preset → a key of its own: another provider uses the preset's API key; card → W → A → a preset → the saved key: another provider uses the preset's API key and it is saved; card → W → A → a preset → replace the saved key: another provider uses the preset's API key; card → G → N → a preset → a key of its own: another provider uses the preset's API key | `claude-multi providers add --preset <preset> --reuse-key`; `claude-multi providers add --preset <preset> --replace-key --secret-file <file>` | not a `/cm` request | asks first: y/N, default No |
| sign in to a Claude or ChatGPT account (personal use) | card → G → Enter on an account provider not signed in: an account provider | `claude-multi providers sign-in anthropic\|openai` | not a `/cm` request | asks first: typed confirmation |
| sign in by printing the address instead of a browser | card → W → A → an account (over SSH the address method is preselected): an account provider | `claude-multi providers sign-in anthropic --no-browser` | not a `/cm` request | asks first: typed confirmation |
| sign out of an account (the records are kept as a backup) | card → G → X: a signed-in account | `claude-multi providers sign-out anthropic\|openai` | not a `/cm` request | asks first: y/N, default No |
| the saved accounts of an account provider | card → G → L: an account provider, whichever connection is active | `claude-multi providers show <provider>` | not a `/cm` request | asks nothing |
| test providers with one small request each | card → G → T: a connected provider | `claude-multi providers test <provider>…` | not a `/cm` request | asks first: consent naming every request |

## Models

In full: [guides/models.md](../guides/models.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| the models: generation, provider, class, efforts, status | card → M | `claude-multi models`; `claude-multi models list`; `claude-multi models list --json` | not a `/cm` request | asks nothing |
| inspect one line: origin, selectors, efforts, context, admission | card → M → Enter: a catalog line; card → M → V: a line; profile editor → Enter on an agent → V: a model in the picker | `claude-multi models show <line>` | not a `/cm` request | asks nothing |
| the resolved entry or its evidence as JSON | not in the launcher: a machine-readable report for scripts and issue reports; the screens show the same facts | `claude-multi models show <line> --resolved`; `claude-multi models show <line> --evidence` | not a `/cm` request | for scripts; asks nothing |
| advisory candidates from the registry and the gateway | card → M → Enter on candidates: the candidates row | `claude-multi models --candidates`; `claude-multi models --candidates --all` | not a `/cm` request | asks nothing |
| declare a model of yours (New · Off) | card → G → A → enter a model by hand: a provider row | `claude-multi models add <provider> <wire> --context <n> --source docs\|operator\|registry` | not a `/cm` request | asks nothing |
| declare a candidate, its form prefilled | card → M → Enter on candidates → Declare…: an attributed candidate | `claude-multi discover <provider> --add <wire> [--as <new-id>]` | not a `/cm` request | asks nothing |
| edit a model you declared | card → M → E: a model you declared | `claude-multi models edit <line>` | not a `/cm` request | asks nothing |
| admit a New · Off line (checklist and one consented smoke) | card → M → Enter: a New line | `claude-multi models admit <line>` | not a `/cm` request | asks first: consent naming every request |
| revoke a line's admission | card → M → Enter: an admitted line | `claude-multi models revoke <line>`; `claude-multi models revoke <line> --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| remove a line you added | card → M → X: a model you declared | `claude-multi models rm <line>` | not a `/cm` request | asks first: y/N, default No |
| remove a line and rewrite what uses it to a successor | card → M → X → choose a replacement: a model a profile uses | `claude-multi models rm <line> --successor <line>` | not a `/cm` request | asks first: y/N, default No |
| qualify a model of yours (consented, bounded checks) | card → M → Q: a model you declared | `claude-multi models qualify <line>`; `claude-multi models qualify <line> --smoke`; `claude-multi models qualify <line> --efforts`; `claude-multi models qualify <line> --tools`; `claude-multi models qualify <line> --stream`; `claude-multi models qualify <line> --context <n>`; `claude-multi models qualify <line> --agents` | not a `/cm` request | asks first: consent naming every request |

## Model listings

In full: [guides/models.md](../guides/models.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| list the models a provider offers (one consented request) | card → G → A → list its models: a provider with a listing | `claude-multi discover <provider>` | not a `/cm` request | asks first: consent naming every request |
| list every enabled provider's models | not in the launcher: advanced discovery: every enabled provider in one consented pass; A lists one provider at a time | `claude-multi discover [<provider>] --all` | not a `/cm` request | asks first: consent naming every request |
| compare the public model feed with the pinned registry | not in the launcher: advanced discovery: a feed comparison for maintainers of the catalog | `claude-multi discover [<provider>] --feed` | not a `/cm` request | asks first: consent naming every request |
| declare listed models New · Off | card → G → A → list → Space → Enter: a listing was read | `claude-multi discover <provider> --add <wire>… [--as <new-id>]` | not a `/cm` request | asks nothing |
| declare a context above the listed one, with a reason | not in the launcher: a context above the listing needs a written justification; the TUI form refuses raising a listed context | `claude-multi discover <provider> --add <wire> --context <n> --over-listed <text>` | not a `/cm` request | asks nothing |

## Profiles

In full: [guides/profiles.md](../guides/profiles.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| list the profiles with origin and last use | card → P | `claude-multi profile list`; `claude-multi profile list --json` | not a `/cm` request | asks nothing |
| a profile's evaluated lineup | card → V: the card shows a profile; claude-multi -r → V: the resume card | `claude-multi profile show [<name>]` | not a `/cm` request | asks nothing |
| create a profile (from scratch or from another) | card → P → N | `claude-multi profile new <name> [--from <profile>]`; `claude-multi profile new <name> --from <profile> --keep-fallback` | not a `/cm` request | asks nothing |
| copy a profile | card → P → C: a profile that can be loaded | `claude-multi profile duplicate <name> <new>`; `claude-multi profile duplicate <name> <new> --keep-fallback` | not a `/cm` request | asks nothing |
| edit a profile and save it (or reload it after a conflict) | card → P → E; card → E: a fresh card; profile editor → Enter: a row of the editor; profile editor → U: an agent row; profile editor → Enter on an agent → Enter: a picker row; profile editor → Enter on an agent → ← →: a model with efforts; profile editor → Ctrl-S: the editor | `claude-multi profile edit <name>` | not a `/cm` request | asks nothing |
| preview where reviews route in a profile | profile editor → R: the editor | `claude-multi profile show [<name>]` | not a `/cm` request | asks nothing |
| rename a profile (the default and followers move with it) | card → P → R: a profile of yours | `claude-multi profile rename <name> <new>` | not a `/cm` request | asks nothing |
| remove a profile of yours (a copy is kept) | card → P → X: a profile of yours | `claude-multi profile rm <name>`; `claude-multi profile rm <name> --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| restore or update a shipped profile (your version is kept) | card → P → U: a shipped profile that changed | `claude-multi profile reseed <name>`; `claude-multi profile reseed <name> --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| list only the fallback profiles | card → P → F | `claude-multi profile list` | not a `/cm` request | asks nothing |
| show or set the default profile | card → P → D: a profile that can be loaded; card → O → default profile → Enter | `claude-multi profile default [<name>]` | not a `/cm` request | asks nothing |
| go back to choosing the default automatically | card → P → D on the default: the default profile; card → O → default profile → R: a default is set | `claude-multi profile default [<name>] --clear` | not a `/cm` request | asks nothing |
| preview and save a starter profile from what is connected | card → P → N → starter: a provider is connected | `claude-multi profile starter`; `claude-multi profile starter --apply` | not a `/cm` request | asks nothing |

## Sessions

In full: [guides/sessions.md](../guides/sessions.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| list managed sessions | card → S; claude-multi -r → S: the resume card | `claude-multi sessions list`; `claude-multi sessions list --json` | not a `/cm` request | asks nothing |
| this directory's sessions or every directory's | card → S → C | `claude-multi sessions list` | not a `/cm` request | asks nothing |
| a session's identity, directory, lineup, pending change and usage | card → S → V: a session row | `claude-multi sessions show <id>` | not a `/cm` request | asks nothing |
| name a session (shown in the TUI) | card → S → R: a current-format session | no command: a session's name is a label of the TUI's list; commands take its id, a prefix or the name | not a `/cm` request | asks nothing |
| forget a session's record and scope (the transcript is kept) | card → S → X: a session that is not running | `claude-multi sessions forget <id>`; `claude-multi sessions forget <id> --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| forget a session whose liveness cannot be determined | card → S → X → type the id: liveness unknown | `claude-multi sessions forget <id> --force` | not a `/cm` request | asks first: typed session id when liveness is unknown |
| stop a session running in the background | card → S → E: a running session | `claude-multi sessions stop <id>` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |
| stop a session whose liveness cannot be determined | card → S → E → type the id: liveness unknown | `claude-multi sessions stop <id> --force` | not a `/cm` request | asks first: typed session id when liveness is unknown |
| record the end of a session that exited without one | card → S → M: a session that exited without an end | `claude-multi sessions mark-ended <id>` | not a `/cm` request | asks first: dialog with Cancel focused first |
| record the end of every session that exited without one | not in the launcher: bulk maintenance over every record; M marks one session after showing it | `claude-multi sessions mark-ended --all-dead` | not a `/cm` request | maintenance; asks nothing |
| rebuild one session's generated scope | card → S → P: a current-format session | `claude-multi doctor --repair <id>` | not a `/cm` request | asks first: dialog with Cancel focused first |
| rebuild every session's generated scope | card → H → repair all: a finding names it | `claude-multi doctor --repair-all`; `claude-multi doctor --repair-all --include-live` | not a `/cm` request | asks nothing |
| adopt a native session | card → S → L: a native session here | `claude-multi sessions link <runtime-id> [--profile <name> \| --direct <model>]` | not a `/cm` request | asks nothing |
| repair a record with the native runtime id | card → S → Enter → Relink: the resume gate finds another runtime id | `claude-multi sessions relink-runtime <id> <runtime-id>` | not a `/cm` request | asks nothing |
| point a session at its moved project directory | card → S → Enter → Relink to …: the project directory moved; card → S → Enter → Relink to… → a directory: the transcript is filed under a directory that cannot be told apart or no longer exists | `claude-multi sessions relink-runtime <id> <runtime-id> --cwd <dir>` | not a `/cm` request | asks nothing |
| adopt a native fork | card → S → Enter on a forked session → Adopt: a pending fork | `claude-multi sessions link <fork-id> --profile <name>` | not a `/cm` request | asks nothing |
| stop tracking a native fork (its transcript is kept) | card → S → Enter on a forked session → Discard: a pending fork | `claude-multi sessions resolve-fork <id> <fork-id>`; `claude-multi sessions resolve-fork <id> <fork-id> --yes` | not a `/cm` request | asks first: y/N, default No; --yes off a terminal |

## Lineups and pending changes

In full: [guides/lineup.md](../guides/lineup.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| a session's lineup, pending change and windows | card → S → V: a session row | `claude-multi lineup --session <runtime-id> show` | `/cm show` | asks nothing |
| the profiles a session can switch to | card → S → T → ← →: the lineup dialog | `claude-multi lineup --session <runtime-id> profiles` | `/cm profiles` | asks nothing |
| follow another profile (live or at the next resume) | card → S → T: a current-format session; card → S → T → Enter: a previewed change | `claude-multi lineup --session <runtime-id> profile <name>` | `/cm profile <name>` | asks nothing |
| bind one agent | card → S → T → Tab to agents → Space: the lineup dialog | `claude-multi lineup --session <runtime-id> set <agent>=<model>[:<effort>]` | `/cm set <agent>=<model>[:<effort>]` | asks nothing |
| unbind one agent | card → S → T → Tab to agents → unbind: the lineup dialog | `claude-multi lineup --session <runtime-id> unset <agent>` | `/cm unset <agent>` | asks nothing |
| change to a lead with no agents | card → S → T → Tab to direct: the lineup dialog | `claude-multi lineup --session <runtime-id> direct [<model>[:<effort>]]` | `/cm direct [<model>[:<effort>]]` | asks nothing |
| preview a change's effect before anything is written | card → S → T: a current-format session | `claude-multi lineup --session <runtime-id> fallback <provider> [--preview]` | `/cm fallback <provider> [--preview]` | asks nothing |
| record a change for the next resume, or relaunch now | card → S → T → Enter (RELAUNCH): a change that needs a relaunch | `claude-multi lineup --session <runtime-id> --relaunch <request>`; `claude-multi lineup --session <runtime-id> --relaunch --its-exited <request>` | not a `/cm` request | asks nothing |
| keep the current lineup and discard a pending change (Follow off) | card → S → T → Keep current lineup — discard pending: a pending change; card → S → F: a session that follows its profile | `claude-multi lineup --session <runtime-id> pin` | `/cm pin` | asks nothing |
| follow the session's profile again, after a preview | card → S → F: a pinned session | `claude-multi lineup --session <runtime-id> follow` | `/cm follow` | asks nothing |
| move the roles on one provider to a fallback lineup | card → S → T → Fallback → provider: a provider with a fallback profile | `claude-multi lineup --session <runtime-id> fallback <provider> [--preview]` | `/cm fallback <provider> [--preview]` | asks nothing |
| apply a saved profile to its running sessions now or later | profile editor → Ctrl-S → apply now / later: running followers | `claude-multi profile edit <name>` | not a `/cm` request | asks nothing |

## Diagnostics

In full: [troubleshooting.md](../troubleshooting.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| check this installation and its sessions | card → H; claude-multi -r → H: the resume card | `claude-multi doctor`; `claude-multi doctor --verbose` | not a `/cm` request | asks nothing |
| the first-run checks in order, one fix each | card → H: nothing is connected yet | `claude-multi doctor --first-run` | not a `/cm` request | asks nothing |
| the structured report with its environment block | not in the launcher: a machine-readable report for scripts and issue reports; the screens show the same facts | `claude-multi doctor --json` | not a `/cm` request | for scripts; asks nothing |
| account quota and credential health (one local read) | card → G (the quota column and Q details): an account provider | `claude-multi quota`; `claude-multi quota --json` | `/cm quota` | asks nothing |
| a session's recorded bindings and observed routing | card → S → V: a session row | `claude-multi explain [<id>]` | not a `/cm` request | asks nothing |
| observed requests per model and session | card → S → V (the last 24 hours): a session row | `claude-multi usage` | not a `/cm` request | asks nothing |
| what the gateway would serve after pending changes | not in the launcher: served-change inspection for review; the provider and model flows show each change's effect before it is written | `claude-multi plan` | not a `/cm` request | asks nothing |
| what a candidate package's assets would serve | not in the launcher: candidate-package inspection for maintainers | `claude-multi plan --assets <path>` | not a `/cm` request | maintenance; asks nothing |

## Settings

In full: [settings.md](settings.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| the launcher settings and their effect | card → O | no command: an interactive, validated editor of local choices; no general settings command is promised | not a `/cm` request | asks nothing |
| edit a setting (applies at the next launch or resume) | card → O → Enter: an editable row | no command: an interactive, validated editor of local choices; no general settings command is promised | not a `/cm` request | asks nothing |
| reset a setting to its default | card → O → R: a row with a default | no command: an interactive, validated editor of local choices; no general settings command is promised | not a `/cm` request | asks first: dialog with Cancel focused first |
| offer or withhold Claude Code's feedback drafts | card → O → feedback drafts | no command: an interactive, validated editor of local choices; no general settings command is promised | not a `/cm` request | asks nothing |
| keep chosen API-key variables (names only) in sessions | card → O → kept environment | no command: a names-only Settings choice, validated against the providers' key names when it is saved and at every launch; no general settings command is promised | not a `/cm` request | asks nothing |
| show the context window ceiling (set or default) | card → O → context window ceiling | `claude-multi window-ceiling` | not a `/cm` request | asks nothing |
| set the context window ceiling (200K to 800K) | card → O → context window ceiling → Enter: choices.json can be read | `claude-multi window-ceiling <ceiling>` | not a `/cm` request | asks nothing |
| reset the context window ceiling to its default | card → O → context window ceiling → R → Reset: a ceiling is set | `claude-multi window-ceiling --reset` | not a `/cm` request | asks nothing |
| each role's effective context window (the lineup summary) | card (the context row) → V: a lineup | `claude-multi lineup --session <runtime-id> show` | `/cm show` | asks nothing |
| each role's effective context window in a launch plan | card (the context row) → Enter: a lineup | `claude-multi --print-launch` | not a `/cm` request | for scripts; asks nothing |
| rotate the local gateway token | card → O → gateway token → Enter | `claude-multi doctor --rotate-token` | not a `/cm` request | asks first: dialog with Cancel focused first |

## Named bindings

In full: [guides/profiles.md](../guides/profiles.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| the named bindings and the profiles that use them | card → P → B; profile editor → N | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | asks nothing |
| add a named binding | card → P → B → A | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | asks nothing |
| change a named binding (then apply it to the profiles using it) | card → P → B → Enter: a named binding | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | asks nothing |
| delete a named binding nothing uses | card → P → B → X: an unused binding | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | asks first: dialog with Cancel focused first |
| bind an agent to a named binding | profile editor → Enter on an agent → a named binding: a named binding exists | no command: named bindings are edited in their validated TUI editor; there is no binding command | not a `/cm` request | asks nothing |

## Another computer

In full: [guides/move-machines.md](../guides/move-machines.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| write your configuration as a portable file (no keys) | not in the launcher: portable-file input and output: the file is written for, or read from, another computer | `claude-multi export`; `claude-multi export --out <file>` | not a `/cm` request | asks nothing |
| preview an exported configuration here | not in the launcher: portable-file input and output: the file is written for, or read from, another computer | `claude-multi import <file>` | not a `/cm` request | asks nothing |
| apply an exported configuration's ready items | not in the launcher: portable-file input and output: the file is written for, or read from, another computer | `claude-multi import <file> --apply` | not a `/cm` request | asks first: y/N, default No |

## Maintenance

In full: [troubleshooting.md](../troubleshooting.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| preview removing stale staging and orphan scopes | not in the launcher: guarded maintenance of generated state, run deliberately from a terminal | `claude-multi doctor --preview` | not a `/cm` request | maintenance; asks nothing |
| remove stale staging and scopes whose records are gone | not in the launcher: guarded maintenance of generated state, run deliberately from a terminal | `claude-multi doctor --prune` | not a `/cm` request | maintenance; asks nothing |
| remove continuity aliases no live session uses | not in the launcher: guarded maintenance of generated state, run deliberately from a terminal | `claude-multi doctor --prune-aliases [<alias>…]` | not a `/cm` request | maintenance; asks nothing |

## Reviews

In full: [guides/lineup.md](../guides/lineup.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| ask another model family for a review | not in the launcher: it runs inside a managed session, where the lead asks for it | no command: it runs inside a managed session, where the lead asks for it | `/cm review [high-stakes] [<range>]` | asks nothing |

## The interface

In full: [cli.md](cli.md).

| Task | TUI | CLI | In-session | Notes |
| --- | --- | --- | --- | --- |
| plain lines instead of the full screen; no colour | not in the launcher: they choose how the TUI itself is drawn | `claude-multi --line`; `claude-multi --no-color` | not a `/cm` request | asks nothing |

Not listed here: 10 compatibility operations; 1 internal operation; 7 maintenance operations of the gateway tool alone; 4 operations the gateway service and the launcher run.

<!-- end of generated: tasks -->
