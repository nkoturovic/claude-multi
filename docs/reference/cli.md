# Command-line reference

claude-multi installs two commands:

- `claude-multi`: the launcher and every user command;
- `claude-multi-proxy`: the local gateway's own tool (`claude-multi-proxy
  --help`), which the launcher and the gateway service run; you rarely need
  it directly.

A source checkout also has `claude-multi-dev`, the maintainers' tool for
reviewed catalog changes; it is not part of an installation.

Every command's `--help` lists its options, examples and exit behaviour.
Exit statuses of the user commands: 0 done, 1 failed, refused or
unavailable, 2 usage, 3 you declined, 130 interrupted.

<!-- generated: cli-reference (tools/docs_gen.py) -->

## Launch

```text
claude-multi [--profile <name> | --profile-file <path>] [-c | -r [<id>]] [--force] [--line] [--no-color] [--print-launch] [--version] [-- <claude-args>]
claude-multi <command>
```

| Argument | What it does |
| --- | --- |
| `--profile <name>` | launch a saved profile (the session follows it) |
| `--profile-file <path>` | launch one unsaved profile JSON (pinned, nameless); '-' reads stdin and forces noninteractive; resume/continue refuse it; nothing is saved |
| `-c`, `--continue` | continue the last managed session in this directory |
| `-r [<id>]`, `--resume [<id>]` | resume a managed session: its id, an id prefix of 8 or more characters, or its name; no value opens the sessions picker |
| `--force` | resume despite a background-liveness marker (only bypasses the heuristic ● check; identity/transcript guards still apply) |
| `--line` | force the line-based UI (no full-screen curses interface) |
| `--no-color` | disable all color output (the NO_COLOR environment variable is also honored) |
| `--print-launch` | print the exact Claude argv and env summary instead of launching (the gateway token is never shown); says whether the launch would succeed |
| `--version` | show the release identity (launcher, catalog, gateway, Claude Code) and exit |

Examples:

| Command | What it does |
| --- | --- |
| `claude-multi` | open the launcher |
| `claude-multi --profile <name>` | launch a saved profile |
| `claude-multi -c` | continue the last session in this directory |
| `claude-multi -r <id>` | resume a session (its id, an 8+ character prefix, or its name) |
| `claude-multi direct --model <model>` | launch one model as the lead |
| `claude-multi doctor` | check health, with one fix for each finding |
| `claude-multi --profile <name> -- <claude-args>` | pass `<claude-args>` to Claude Code unchanged |

### claude-multi direct

Launch one model as the lead, with no cm-\* agents (recorded and resumable like every session).

```text
claude-multi direct [--model <model>] [--no-subagents] [--force] [--print-launch] [-c | -r [<id>]] [-- <claude-args>]
```

| Argument | What it does |
| --- | --- |
| `--model <model>` | the model to run as the lead (claude-multi models list); without it a terminal opens the Direct picker |
| `--no-subagents` | hard-deny subagent delegation for this session (recorded; resume re-applies it, a mismatching explicit flag is rejected) |
| `--force` | resume despite a background-liveness marker (only bypasses the heuristic ● check) |
| `--print-launch` | print the exact Claude argv and env summary instead of launching |
| `-c`, `--continue` | continue the last managed session in this directory |
| `-r [<id>]`, `--resume [<id>]` | resume a managed session: its id, an id prefix of 8 or more characters, or its name |

## Inspect (read only)

### claude-multi doctor

Check this installation and its sessions. The verdict is Ready, Attention or BLOCKED; each finding names one fix. Plain doctor refreshes the helper shims as routine housekeeping; use doctor --json for a read-only report. --repair, --prune and --rotate-token act.

```text
claude-multi doctor [--json | --repair <id> | --repair-all | --prune | --rotate-token | --prune-aliases [<alias>…]] [-v] [--first-run] [--preview] [--include-live]
```

| Argument | What it does |
| --- | --- |
| `--json` | read-only diagnostic report (one JSON document) |
| `-v`, `--verbose` | also show every session's line and the observation details |
| `--first-run` | the first-run checks in order, one fix each (with --json: one object) |
| `--repair <id>` | converge a session's scope to its record authority (its id, an id prefix of 8 or more characters, or its name) |
| `--repair-all` | converge every durable session's scope (and refresh its record snapshot) against the installed catalog |
| `--preview` | read-only generated-state prune preview |
| `--prune` | remove stale staging dirs and scopes whose records are gone |
| `--rotate-token` | rotate the local gateway token hitlessly (dual-key window, ~5 min helper-TTL wait, reload verified; interactive; rerun resumes) |
| `--prune-aliases [<alias>…]` | remove gateway continuity aliases no live session references (all, or the named ones) |
| `--include-live` | with --repair-all: also converge sessions whose last event is not `end` (live or unknown) |

### claude-multi quota

Account quota and credential health from the local gateway (one local read; no provider call).

```text
claude-multi quota [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one read-only report object |

### claude-multi explain

Explain a session's recorded bindings and observed routing.

```text
claude-multi explain [<id>] [--agent <agent>] [--json]
```

| Argument | What it does |
| --- | --- |
| `[<id>]` | its id, an id prefix of 8 or more characters, or its name; defaults to the current session |
| `--agent <agent>` | bound cm-\* role or exact observed agent id |
| `--json` | one read-only report object |

### claude-multi usage

Observed client requests per model and session (not token usage).

```text
claude-multi usage [--since <when>] [--session <id>] [--json]
```

| Argument | What it does |
| --- | --- |
| `--since <when>` | RFC 3339 instant or duration up to 30d; default 24h |
| `--session <id>` | its id, an id prefix of 8 or more characters, or its name; exact attribution only |
| `--json` | one read-only report object |

### claude-multi plan

Preview what the gateway would serve after pending changes; writes nothing.

```text
claude-multi plan [--assets <path>] [--json]
```

| Argument | What it does |
| --- | --- |
| `--assets <path>` | plan a candidate package's assets (the running launcher is unchanged) |
| `--json` | one read-only plan object |

## Manage

### claude-multi setup

Run the setup steps that are not done yet, in order: preflight, claude, gateway, providers, test (optional), profile, check. Nothing is saved until you confirm. --step runs one step (also when it is done); claude puts claude-multi's own copy of the pinned Claude Code in place; gateway records the gateway's outbound proxy (an unauthenticated http, https or socks5 URL; the gateway never inherits proxy variables) and starts the gateway, or re-renders a running one.

```text
claude-multi setup [--step preflight|claude|gateway|providers|test|profile|check] [--redo] [--status] [--answers <file>] [--keys-file <path>] [--claude-from <path>] [--proxy <url> | --no-proxy]
```

| Argument | What it does |
| --- | --- |
| `--step preflight\|claude\|gateway\|providers\|test\|profile\|check` | run this step only (also when it is done) |
| `--redo` | run the steps that are done too |
| `--status` | show the steps and exit 0 when every required one is done, else 1 |
| `--answers <file>` | a prepared setup (keys by file only; sign-ins and tests need you at the terminal) |
| `--keys-file <path>` | use an existing private key file (NAME=value lines) for API keys |
| `--claude-from <path>` | claude step: copy the pinned Claude Code from this file (checked by size and sha256) |
| `--proxy <url>` | gateway step: the gateway's outbound proxy (no credentials in it) |
| `--no-proxy` | gateway step: remove the gateway's outbound proxy |

### claude-multi profile

List, show, create, edit, copy, rename, remove and restore profiles; choose the default profile.

```text
claude-multi profile <command>
```

#### claude-multi profile list

List profiles with origin and last use.

```text
claude-multi profile list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields) |

#### claude-multi profile show

Show a profile's evaluated lineup.

```text
claude-multi profile show [<name>]
```

| Argument | What it does |
| --- | --- |
| `[<name>]` | profile name (default: balanced) |

#### claude-multi profile new

Create a profile ($EDITOR, or --from SRC).

```text
claude-multi profile new <name> [--from <profile>] [--keep-fallback]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--from <profile>` | copy SRC (non-interactive) |
| `--keep-fallback` | with --from: the copy stays a fallback profile for SRC's provider |

#### claude-multi profile edit

Edit a profile in $VISUAL/$EDITOR.

```text
claude-multi profile edit <name>
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |

#### claude-multi profile rm

Remove a profile of yours (a copy is kept).

```text
claude-multi profile rm <name> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--yes` | skip the y/N confirmation |

#### claude-multi profile rename

Rename a profile.

```text
claude-multi profile rename <name> <new>
```

| Argument | What it does |
| --- | --- |
| `<name>` | source profile name |
| `<new>` | target profile name |

#### claude-multi profile duplicate

Duplicate a profile.

```text
claude-multi profile duplicate <name> <new> [--keep-fallback]
```

| Argument | What it does |
| --- | --- |
| `<name>` | source profile name |
| `<new>` | target profile name |
| `--keep-fallback` | the copy stays a fallback profile for the source's provider |

#### claude-multi profile reseed

Restore a shipped profile to its shipped version (your version is kept as a copy).

```text
claude-multi profile reseed <name> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--yes` | skip the y/N confirmation |

#### claude-multi profile default

Show, set or clear the default profile (used where nothing else chooses one).

```text
claude-multi profile default [<name>] [--clear]
```

| Argument | What it does |
| --- | --- |
| `[<name>]` | the profile to make the default |
| `--clear` | go back to choosing automatically |

#### claude-multi profile starter

Preview a starter profile of locally ready lines (--apply saves it after confirmation).

```text
claude-multi profile starter [--name <name>] [--apply]
```

| Argument | What it does |
| --- | --- |
| `--name <name>` | profile name to save (default: starter; an existing name is refused) |
| `--apply` | save the previewed profile after a y/N confirmation |

#### claude-multi profile migrate

Convert legacy compositions to profiles (a bare run is a dry run).

It reads or converts an earlier format; its own `--help` lists its options.

### claude-multi sessions

List, show, stop, forget, link and repair managed sessions.

```text
claude-multi sessions <command>
```

#### claude-multi sessions list

List managed sessions (a report; pick one to resume with claude-multi -r).

```text
claude-multi sessions list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields; no transcript content) |

#### claude-multi sessions show

Show a managed session's record (JSON).

```text
claude-multi sessions show <id>
```

| Argument | What it does |
| --- | --- |
| `<id>` | its id, an id prefix of 8 or more characters, or its name |

#### claude-multi sessions forget

Forget a managed session: its record and generated scope (the transcript is kept).

```text
claude-multi sessions forget <id> [--force] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<id>` | its id, an id prefix of 8 or more characters, or its name |
| `--force` | forget even when background liveness cannot be determined |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### claude-multi sessions link

Adopt a native session (must exist in local Claude metadata).

```text
claude-multi sessions link [<runtime-id>] [--profile <name> | --direct <model>] [--cwd <dir>]
```

| Argument | What it does |
| --- | --- |
| `[<runtime-id>]` | native session UUID |
| `--profile <name>` | adopt following this profile |
| `--direct <model>` | adopt as an ad-hoc direct session |
| `--cwd <dir>` | authoritative original project directory (validated against metadata) |

#### claude-multi sessions relink-runtime

Repair a managed record with the authoritative native runtime UUID.

```text
claude-multi sessions relink-runtime <id> <runtime-id> [--cwd <dir>]
```

| Argument | What it does |
| --- | --- |
| `<id>` | the managed session: its id, an id prefix of 8 or more characters, or its name |
| `<runtime-id>` | UUID shown by native /status or /resume |
| `--cwd <dir>` | also replace the recorded original project CWD |

#### claude-multi sessions resolve-fork

Discard a pending native-fork marker (the fork transcript is kept).

```text
claude-multi sessions resolve-fork <id> <fork-id> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<id>` | the parent session: its id, an id prefix of 8 or more characters, or its name |
| `<fork-id>` | runtime UUID of the native fork to stop tracking |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### claude-multi sessions stop

Stop a live background-owned session (upstream `claude stop`; the conversation is always kept).

```text
claude-multi sessions stop <id> [--force] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<id>` | its id, an id prefix of 8 or more characters, or its name |
| `--force` | send claude stop even when background liveness cannot be determined |
| `--yes` | skip the interactive confirmation (required non-interactively) |

#### claude-multi sessions mark-ended

Record a synthetic SessionEnd for a session you know has exited (refused while it is live in the daemon or a running process).

```text
claude-multi sessions mark-ended (<id> | --all-dead)
```

| Argument | What it does |
| --- | --- |
| `[<id>]` | its id, an id prefix of 8 or more characters, or its name |
| `--all-dead` | every record whose last event is not `end` and that no daemon socket or running process names |

### claude-multi providers

List, add, connect (API key or sign-in), test, turn on or off, apply and remove providers.

```text
claude-multi providers <command>
```

#### claude-multi providers list

List every provider, shipped and yours: source, on or off, connection and line counts.

```text
claude-multi providers list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields; never a key value) |

#### claude-multi providers show

Show one provider declaration.

```text
claude-multi providers show <provider> [--resolved]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id (providers.d/\<id>.json) |
| `--resolved` | print the resolved provider and lines as JSON |

#### claude-multi providers validate

Validate providers.d (or one candidate FILE); writes nothing.

```text
claude-multi providers validate [<file>]
```

| Argument | What it does |
| --- | --- |
| `[<file>]` | a candidate \<id>.json checked with the current files |

#### claude-multi providers template

Print a providers.d template for a reviewed kind.

```text
claude-multi providers template [--kind anthropic-compatible|openai-compatible-lan|openai-compatible]
```

| Argument | What it does |
| --- | --- |
| `--kind anthropic-compatible\|openai-compatible-lan\|openai-compatible` | reviewed provider kind (default anthropic-compatible) |

#### claude-multi providers add

Declare a new provider, by hand or from a reviewed --preset (route approval follows unless --declare-only).

```text
claude-multi providers add [<provider>] [--preset <preset>] [--as <provider>] [--secret-file <file>] [--reuse-key | --replace-key] [--kind anthropic-compatible|openai-compatible-lan|openai-compatible] [--base-url <url>] [--auth bearer|header|none] [--header <header>] [--secret-ref <secret-ref>] [--family <family>] [--display <text>] [--contracts <contracts>] [--listing-url <url>] [--listing-auth provider|none] [--listing-shape anthropic|openai] [--declare-only]
```

| Argument | What it does |
| --- | --- |
| `[<provider>]` | new provider id (never a catalog id) |
| `--preset <preset>` | declare from a reviewed preset or sample (a template, never a grant) |
| `--as <provider>` | provider id for --preset |
| `--secret-file <file>` | import the key from a private 0600 file into the secret store (terminal only) |
| `--reuse-key` | for --preset when another provider already uses its API key: share that saved key (none is imported; by default the new provider gets its own key name) |
| `--replace-key` | for --preset when another provider already uses its API key: replace that shared key with --secret-file (asks first, naming every provider using it) |
| `--kind anthropic-compatible\|openai-compatible-lan\|openai-compatible` | generation protocol (default: anthropic-compatible, recommended for documented Messages endpoints; explicit preset kind wins) |
| `--base-url <url>` | provider base URL (https when keyed) |
| `--auth bearer\|header\|none` | credential transport |
| `--header <header>` | header name for --auth header (x-api-key only) |
| `--secret-ref <secret-ref>` | logical credential reference in the secret store (never the value) |
| `--family <family>` | independence family (lower-case id) |
| `--display <text>` | display name |
| `--contracts <contracts>` | reviewed payload contracts it uses |
| `--listing-url <url>` | model listing URL (with --listing-shape) |
| `--listing-auth provider\|none` | listing authentication only (default: provider for keyed routes, none for LAN) |
| `--listing-shape anthropic\|openai` | model listing shape (with --listing-url) |
| `--declare-only` | write an inert, unapproved declaration (no approval, no provider call) |

#### claude-multi providers edit

Edit a declaration in $VISUAL/$EDITOR (validated before it is written).

```text
claude-multi providers edit <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### claude-multi providers approve

Approve a declared provider's credential route (terminal, outside sessions).

```text
claude-multi providers approve <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### claude-multi providers set-key

Set or replace a provider's API key (shipped providers and yours; terminal, outside sessions).

```text
claude-multi providers set-key <provider> [--secret-file <file>] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--secret-file <file>` | read the key from a private 0600 file instead of a hidden prompt |
| `--yes` | replace a saved key without the y/N question (for every provider that uses it) |

#### claude-multi providers remove-key

Remove a provider's saved API key (terminal, outside sessions).

```text
claude-multi providers remove-key <provider> [--yes] [--name <key-name>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--yes` | skip the y/N confirmation |
| `--name <key-name>` | the key's name, for a key kept after its provider was removed |

#### claude-multi providers sign-in

Sign in to your Claude or ChatGPT account in this terminal (personal use; you confirm that first).

```text
claude-multi providers sign-in anthropic|openai [--no-browser]
```

| Argument | What it does |
| --- | --- |
| `anthropic\|openai` | anthropic (Claude account) or openai (ChatGPT account) |
| `--no-browser` | print the address instead of opening a browser (Claude account) |

#### claude-multi providers sign-out

Sign out of your Claude or ChatGPT account (the records are kept as a backup).

```text
claude-multi providers sign-out anthropic|openai [--yes]
```

| Argument | What it does |
| --- | --- |
| `anthropic\|openai` | anthropic (Claude account) or openai (ChatGPT account) |
| `--yes` | skip the y/N confirmation |

#### claude-multi providers test

Send one small request to each named provider after one consent (may be billed).

```text
claude-multi providers test <provider>…
```

| Argument | What it does |
| --- | --- |
| `<provider>…` | provider id |

#### claude-multi providers enable

Turn a provider on for new sessions.

```text
claude-multi providers enable <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### claude-multi providers disable

Turn a provider off for new sessions (running sessions keep theirs).

```text
claude-multi providers disable <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### claude-multi providers rm

Remove a provider you added (refused while its lines are admitted, bound or live).

```text
claude-multi providers rm <provider> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--yes` | skip the y/N confirmation (the API key is kept) |

#### claude-multi providers apply

Re-render the gateway config from the catalog and providers.d, verify the reload.

```text
claude-multi providers apply
```

#### claude-multi providers transport

Show or switch a catalog OAuth-pool provider's reviewed transport (same selectors; switching is a guarded route approval).

```text
claude-multi providers transport <provider> [oauth-pool|api-key] [--secret-file <file>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | catalog provider id (e.g. anthropic) |
| `[oauth-pool\|api-key]` | the transport to use (omit to show the current one) |
| `--secret-file <file>` | import the key from a private 0600 file (api-key; terminal only) |

#### claude-multi providers migrate-custom

Migrate custom.json to providers.d (a bare run is a dry run).

It reads or converts an earlier format; its own `--help` lists its options.

### claude-multi models

Without a command (or with list): the models (generation, provider, class, efforts, status), their typed /model selectors and the retired keys. The other commands manage the models you add.

```text
claude-multi models [--candidates] [--all] [<command>]
```

| Argument | What it does |
| --- | --- |
| `--candidates` | advisory registry and served candidates (nothing declared or admitted) |
| `--all` | with --candidates: every registry channel and older candidates |

#### claude-multi models list

The listing (same as claude-multi models).

```text
claude-multi models list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields) |

#### claude-multi models add

Declare a model of yours (New · Off; allowed anywhere).

```text
claude-multi models add <provider> <wire> [--as <new-id>] --context <n> --source docs|operator|registry [--source-ref <text>] [--effort <effort>] [--default-effort <effort>] [--display <text>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | catalog or providers.d provider id |
| `<wire>` | upstream model id |
| `--as <new-id>` | line key (custom-...; derived from WIRE) |
| `--context <n>` | declared context tokens |
| `--source docs\|operator\|registry` | where the context figure comes from |
| `--source-ref <text>` | URL/date for --source docs\|registry |
| `--effort <effort>` | declared effort (repeatable; with contracts a map, else a list) |
| `--default-effort <effort>` | default effort (a declared one) |
| `--display <text>` | display name |

#### claude-multi models admit

Admit a New · Off line (checklist, one consented smoke; terminal, outside sessions).

```text
claude-multi models admit <line>
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |

#### claude-multi models revoke

Revoke a line's admission (running sessions keep their fence until relaunch).

```text
claude-multi models revoke <line> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### claude-multi models edit

Edit a model of yours in $VISUAL/$EDITOR (shows the admission consequences).

```text
claude-multi models edit <line>
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |

#### claude-multi models rm

Remove a line you added (captured aliases stay served until pruned).

```text
claude-multi models rm <line> [--successor <line>] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--successor <line>` | rewrite the profiles and named bindings that name KEY to this offered line |
| `--yes` | skip the y/N confirmation |

#### claude-multi models show

Show a line (status, selectors, class, evidence).

```text
claude-multi models show <line> [--resolved] [--evidence]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--resolved` | print the resolved entry as JSON |
| `--evidence` | print the tool-owned evidence as JSON |

#### claude-multi models qualify

Run the consented qualification battery on a model of yours (its own aliases) through the loopback gateway. Every request is listed before one y/N; no retries; 120 s and 256 KiB per request (context 300 s). Evidence only: it never admits a line or edits its declaration. No flag means --smoke. Your models only (shipped models carry reviewed evidence).

```text
claude-multi models qualify <line> [--smoke] [--efforts] [--tools] [--tool-choice forced|auto] [--stream] [--context <n>] [--agents]
```

| Argument | What it does |
| --- | --- |
| `<line>` | the key of a model you added |
| `--smoke` | one minimal generation at the default effort |
| `--efforts` | one minimal generation per declared effort |
| `--tools` | a strict-schema tool round trip (up to 2 requests; the second only after the expected tool call) |
| `--tool-choice forced\|auto` | --tools with a forced named tool (default) or a strict automatic tool; disclosed before consent, never retried the other way |
| `--stream` | one streamed generation ending in message_stop |
| `--context <n>` | a synthetic retrieval at about N input tokens (8192..declared; 300 s) |
| `--agents` | --smoke --efforts --tools --stream (not --context); first-party pool lines also run the offline exact-client check |

### claude-multi discover

List the models a provider advertises (PROVIDER), every enabled direct provider (--all), or compare the public feed with the pinned registry (--feed). Provider calls run only in a terminal outside Claude Code sessions, after one y/N naming every request.

```text
claude-multi discover [<provider>] [--all | --feed] [--add <wire>…] [--as <new-id>] [--context <n>] [--over-listed <text>]
```

| Argument | What it does |
| --- | --- |
| `[<provider>]` | provider id (see `claude-multi models`) |
| `--all` | list every enabled direct provider with a supported listing (observation only; `discover openai` stays separate) |
| `--feed` | compare the public model feed with the pinned registry (advisory) |
| `--add <wire>…` | declare listed WIRE(s) New · Off (declared, not admitted) |
| `--as <new-id>` | line key for exactly one --add WIRE (custom-...) |
| `--context <n>` | declared context when the listing states none, or an explicit override |
| `--over-listed <text>` | required to declare --context above the listing-stated value |

### claude-multi lineup

Show or change a session's lineup from a terminal (what /cm runs in a session).

```text
claude-multi lineup [--session <runtime-id>] [--relaunch] [--its-exited] <request>
```

| Argument | What it does |
| --- | --- |
| `--session <runtime-id>` | runtime session id (/cm passes it) |
| `--relaunch` | relaunch instead of a live apply |
| `--its-exited` | the session has exited (no confirmation) |
| `<request>` | lineup request: show, profiles, profile, set, unset, direct, pin, follow, fallback, review, quota |

The requests are `/cm`'s ([inside a session](#inside-a-session)); inside a session the runtime id comes from the environment.

### claude-multi window-ceiling

One ceiling governs the lead and every agent: a session's window is the smaller of it and the lead set's smallest provider bound, and agent lines whose bound is below that window keep the 200K class. Without arguments it shows the ceiling and whether it is set or the default.

```text
claude-multi window-ceiling [<ceiling> | --reset]
```

| Argument | What it does |
| --- | --- |
| `[<ceiling>]` | the new ceiling in tokens (400000) or thousands (400K), from 200K to 800K |
| `--reset` | go back to the default ceiling (800K) |

### claude-multi gateway

Manage the local gateway. It starts on demand when a session needs it; stop and restart act only on a gateway proven to be yours and never while a credential save may be unresolved.

```text
claude-multi gateway <command>
```

#### claude-multi gateway start

Start the gateway if it is not running and wait until it is ready.

```text
claude-multi gateway start
```

#### claude-multi gateway stop

Stop your gateway (refused while the persistence hold is active).

```text
claude-multi gateway stop
```

#### claude-multi gateway restart

Stop, then start your gateway (same checks as stop).

```text
claude-multi gateway restart
```

#### claude-multi gateway status

Backend, endpoint, instance, log and persistence hold (sends no token).

```text
claude-multi gateway status
```

#### claude-multi gateway logs

The newest gateway log lines (or one instance's).

```text
claude-multi gateway logs [-n <n>] [--instance <nonce>]
```

| Argument | What it does |
| --- | --- |
| `-n <n>`, `--lines <n>` | how many lines (default 50) |
| `--instance <nonce>` | the instance id shown by `claude-multi gateway status` |

#### claude-multi gateway clear-hold

Clear the persistence hold after you verified the credentials (a terminal outside Claude Code; typed confirmation; the gateway's own recovery for a hold its log reads could not settle).

```text
claude-multi gateway clear-hold
```

#### claude-multi gateway ensure

For scripts: exit 0 only when your gateway runs and is ready (starts it when stopped).

```text
claude-multi gateway ensure [--quiet] [--max-wait <n>] [--base-url <url>]
```

| Argument | What it does |
| --- | --- |
| `--quiet` | print nothing on success |
| `--max-wait <n>` | wait at most this long (default 45) |
| `--base-url <url>` | refuse unless the gateway's configured endpoint is URL (a session's compiled ANTHROPIC_BASE_URL) |

#### claude-multi gateway service

Hand the gateway to a hardened systemd user unit that restarts it on failure, or back to the on-demand start. Both directions stop the running gateway only after the persistence hold, and restore the previous backend when a step fails.

```text
claude-multi gateway service <command>
```

##### claude-multi gateway service install

Install (or refresh) the unit and hand the gateway to it.

```text
claude-multi gateway service install [--name <unit>]
```

| Argument | What it does |
| --- | --- |
| `--name <unit>` | the unit's name (default: claude-multi-gateway) |

##### claude-multi gateway service uninstall

Remove the unit and hand the gateway back to the on-demand start.

```text
claude-multi gateway service uninstall
```

##### claude-multi gateway service status

Whether the service is installed, its name, backend and unit state.

```text
claude-multi gateway service status
```

### claude-multi update

Update claude-multi itself: check, show the plan, ask, install (installed releases), or switch back to the previously installed version.

```text
claude-multi update [--check | --rollback] [--yes] [--from-dir <dir>] [--base-url <url>]
```

| Argument | What it does |
| --- | --- |
| `--check` | only check whether a newer release exists |
| `--rollback` | switch back to the previously installed version |
| `--yes`, `-y` | apply without asking (needed without a terminal) |
| `--from-dir <dir>` | use the release files in a local directory instead of downloading |
| `--base-url <url>` | download from this https location instead of the release's own |

### claude-multi export

Write your portable configuration (JSON on stdout by default): no secrets, keys, sessions, logs or evidence; trust travels as inert re-approval requests.

```text
claude-multi export [--out <file>]
```

| Argument | What it does |
| --- | --- |
| `--out <file>` | write FILE atomically (0600) and record a confirmed-export receipt |

### claude-multi import

Preview a portable export against this computer (writes nothing); --apply writes the ready items; imported trust is never active.

```text
claude-multi import <file> [--apply]
```

| Argument | What it does |
| --- | --- |
| `<file>` | a `claude-multi export` JSON file |
| `--apply` | confirm and apply the ready items shown (a terminal outside Claude Code) |

## Recovery and earlier formats

### claude-multi migrate

Convert legacy session records to the current format (backups are kept for restore-2x); --dry-run writes nothing.

It reads or converts an earlier format; its own `--help` lists its options.

### claude-multi restore-2x

Put back the legacy session records a migration backed up, before running an earlier launcher.

It reads or converts an earlier format; its own `--help` lists its options.

### claude-multi custom

The earlier custom-model registry (move it to providers: claude-multi providers migrate-custom).

It reads or converts an earlier format; its own `--help` lists its options.

### claude-multi uninstall

Remove claude-multi from this computer. The plan is shown first; your credentials go only after a typed confirmation; kept backups and your Claude Code are never removed.

```text
claude-multi uninstall [--dry-run] [--keep-setup] [--keep-credentials] [--yes] [--force]
```

| Argument | What it does |
| --- | --- |
| `--dry-run` | show the plan and remove nothing |
| `--keep-setup` | keep your setup and session state (profiles, settings, records) |
| `--keep-credentials` | keep your API keys, sign-ins and the local gateway key without asking |
| `--yes` | skip the y/N confirmation (never the typed one for credentials) |
| `--force` | remove even when sessions may still be running (they are named) |

## The gateway tool

`claude-multi-proxy` is the local gateway's own tool. The launcher and the gateway service run it; a doctor finding names it when it is the fix.

| Command | Arguments | What it does |
| --- | --- | --- |
| `claude-multi-proxy init` | `[--reload-check \| --start-check \| --prepare-start] [--state-root /abs]` | Prepare the gateway's configuration, key and directories and verify the hot reload (up to 2 s). |
| `claude-multi-proxy run` | `[--prepared \| --prepare-and-exec [--detach]] [--instance NONCE] [--state-root /abs]` | Exec the gateway, holding the single-instance lock across exec. |

`claude-multi-proxy --help` lists its 6 maintenance commands too, and the exit statuses of every command.

## Inside a session

`/cm` shows or changes the session's lineup. A change applies after you type `/reload-plugins`, or at the next resume when it needs a relaunch.

| Request | What it does | Changes the lineup |
| --- | --- | --- |
| `/cm show` | this session's lineup (also the empty request) | no |
| `/cm profiles` | the profiles this session can switch to | no |
| `/cm profile <name>` | follow another profile | yes |
| `/cm set <agent>=<model>[:<effort>]` | bind one agent | yes |
| `/cm unset <agent>` | unbind one agent | yes |
| `/cm direct [<model>[:<effort>]]` | a lead with no agents | yes |
| `/cm pin` | keep the current lineup and stop following the profile (drops a pending change) | yes |
| `/cm follow` | follow the session's profile again | yes |
| `/cm fallback <provider> [--preview]` | move the roles bound to a provider to a fallback lineup | yes |
| `/cm review [high-stakes] [<range>]` | ask for a review from another model family | no |
| `/cm quota` | the quota of this session's accounts | no |

`/cm` passes everything after it as one request; `claude-multi lineup --session <runtime-id> <request>` runs the same request from a terminal.

<!-- end of generated: cli-reference -->
