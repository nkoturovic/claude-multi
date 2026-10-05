# Settings and files

Where claude-multi keeps its settings, its state and its credentials. Paths
below show the defaults; each configuration entry names its root:

- **Config root**: `$XDG_CONFIG_HOME/claude-multi`, defaulting to
  `~/.config/claude-multi`.
- **HOME-relative**: always under `~/.config/claude-multi`, regardless of
  `XDG_CONFIG_HOME` (including providers, the operator ledger, endpoint,
  continuity and gateway credentials).
- **State root**: `$XDG_STATE_HOME/claude-multi`, defaulting to
  `~/.local/state/claude-multi`.

Relative `XDG_CONFIG_HOME` and `XDG_STATE_HOME` values are ignored; only
absolute paths select another root. Change settings through the launcher
(**O**) or the commands named here; edit the files by hand only where
this page says so.

## Your choices: `choices.json`

`~/.config/claude-multi/choices.json`, mode 0600; **Config root**.

| Field | Type, default | Effect | Set with |
| --- | --- | --- | --- |
| `default_profile` | profile name, unset | the profile a launch uses when nothing else chooses one ([guides/profiles.md](../guides/profiles.md#the-default-profile)) | `claude-multi profile default <name>`, **P** → **D**, **O** |
| `session_env_keep` | list of variable names, empty | `*_API_KEY` variables a managed session keeps; every other one is removed. Names only; provider key names are refused | **O** → kept environment |
| `acknowledgements` | per account, empty | the personal-use texts you confirmed for the Claude and ChatGPT sign-ins | the sign-in |
| `window_ceiling` | tokens, 800000 (200000–800000) | the context window ceiling of the lead and every agent | `claude-multi window-ceiling <n>`, **O** |

A change applies at the next launch or resume. A file that cannot be read
is reported by doctor with its fix; without it every choice has its
default.

## Session policies: `settings.json`

`~/.config/claude-multi/settings.json`; **Config root**, edited on the
Settings screen (**O**: Enter edits, **R** resets).

| Field | Default | Effect |
| --- | --- | --- |
| `compaction_percent` | 90 (60–95) | when automatic compaction starts, as a percentage of the session's window; a profile may override it (◆ marks that) |
| `explore_inherit_cap_disabled` | true | turns off Claude Code's Explore inheritance cap for Fable leads (other leads are not affected) |
| `review_round_cap` | 2 (1–3) | the review rounds the lineup allows, printed in the lineup |
| `workflow_default_binding` | none | a model and effort for workflow agents started without a `cm-*` agent type, and for Claude Code's general-purpose agent |

The Settings screen also rotates the gateway token
([guides/gateway.md](../guides/gateway.md#the-gateway-token)) and edits
the choices above.

## Host preferences: `preferences.json`

`~/.config/claude-multi/preferences.json`; **Config root**: `claude_feedback_drafts`, `off`
(the default: managed sessions never offer Claude Code's feedback
drafts) or `notify` (Claude Code's own default). Settings → feedback
drafts edits it; it applies at each session's next launch or resume.

## The API-key file

One private file (mode 0600) of `NAME=value` lines holds every API key.
claude-multi uses, in this order:

1. the file `CLAUDE_MULTI_SECRET_ENV` names (the supervised gateway
   service ignores this variable);
2. the file `claude-multi setup --keys-file <file>` selected, recorded in
   `~/.config/claude-multi/secret-file.json`. It stays where it is, and
   “the key file must be inside ~/.config/claude-multi/, or in a private
   folder of its own that you own (no group or other access: chmod 700),
   not your home folder itself”. Every managed session is denied reading
   that file by name, and doctor names the selection when it is the cause
   of a finding;
3. `~/.config/claude-multi/secrets/provider-keys.env`.

The launcher, the gateway and every key command read the same file. The
file is never parsed by a shell. Key names are listed per provider in
[providers/api-keys.md](../providers/api-keys.md).

## Other configuration

| Path under the named root | Root | What |
| --- | --- | --- |
| `profiles/<name>.json` | Config root | your profiles (and `.<name>.<reason>-<time>.json` backups) |
| `bindings.json` | Config root | named bindings |
| `providers.d/<id>.json` | HOME-relative | the providers you declared |
| `operator-ledger.json` | HOME-relative | route approvals, admissions and served aliases (claude-multi writes it; never edit) |
| `endpoint.json` | HOME-relative | the gateway's port, backend, unit and outbound proxy |
| `continuity.json` | HOME-relative | aliases kept serving for sessions of retired models |
| `custom.json` | Config root (launcher); HOME-relative (gateway) | the earlier registry of your own models (`claude-multi providers migrate-custom` moves it); differing copies are reported as a mismatch |
| `secret-file.json`, `secrets/provider-keys.env` | HOME-relative | the key-file selection and default key file; a selected key file stays at its chosen path |
| `api-key`, `previous-key`, `config.yaml`, `management-key*` | HOME-relative | the gateway's keys and rendered configuration: secrets, names only |

## State

Every row uses the **State root**, not the configuration root.

| Path under the state root | What |
| --- | --- |
| `sessions/<id>.json` | session records |
| `scopes/<id>/` | each session's generated files (never edit) |
| `lineup-log/<id>.log` | one line per agent start and lineup change (the binding, not proof of the served model) |
| `hook-errors.log` | hook failures, metadata only |
| `gateway/` | the gateway's working directory and its logs (a `.env` file there stops the gateway) |
| `channel` | the installation that owns this state: `bundle`, `nix` or `source` |
| `state-version`, `migration.lock` | the state format and the lock migrations hold |
| `bin/` | hook shims and the token helper: never run them by hand |

## Installation

| Path | What |
| --- | --- |
| `~/.local/share/claude-multi/install/` | release bundles: `versions/<version>`, `current`, `previous`, and the receipt `installer.json` |
| `~/.local/share/claude-multi/nix/current` | the Nix package the supervised service runs (only after `claude-multi gateway service install`) |
| `~/.local/share/claude-multi/claude/<version>/claude` | claude-multi's own copy of the pinned Claude Code |
| `~/.local/share/claude-multi/` (other files) | account sign-in records and their backups: secrets |
| `~/.config/systemd/user/claude-multi-gateway.service` | the supervised gateway unit (Linux, optional) |

## Session environment

Every managed session removes from its environment: the gateway credential
variables, every provider key variable claude-multi knows of, every other
`*_API_KEY` variable not listed in `session_env_keep`, and the variables
that would override its model, effort, thinking or compaction policy. It
unsets `CLAUDE_CONFIG_DIR`, so Claude Code's settings, MCP servers, memory
and sessions come from `~/.claude`. `claude-multi --print-launch` (or
`claude-multi -r <id> --print-launch`) prints every variable it removes.
Claude Code's own state (`~/.claude`) is Claude Code's; claude-multi never
writes there.
