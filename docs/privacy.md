# Privacy: what leaves your computer

claude-multi runs Claude Code and a local gateway on your computer and
sends your requests to the providers you connect. This page lists what
goes where, what stays local, and what claude-multi does not control.

## Data flows

| From | To | What | When |
| --- | --- | --- | --- |
| Claude Code (managed session) | the local gateway (loopback) | your prompts, file contents the session reads, tool results, the gateway key | every model request |
| the gateway | the provider serving each role | the requests above, with that provider's key or sign-in | every model request |
| Claude Code (managed session) | Anthropic | Claude Code's own start-up, feature-flag and other nonessential requests, as upstream Claude Code makes them | as Claude Code decides |
| the launcher | `downloads.claude.ai` | a download of the pinned Claude Code | only after you agree |
| the launcher | the release location | release metadata and signed checksums; the bundle when an update is accepted | metadata when you run update or check; the bundle after confirmation or `--yes` |
| the launcher | a provider's model-listing endpoint | the listing request, with the authentication named in its request plan, or no credential for a public/keyless listing | only after you agree to the listed requests |
| the launcher | the public third-party model feed | a credential-free GET to compare the feed with the pinned registry | only after you agree to the feed request |
| the launcher | your configured name resolver and a declared keyless LAN server's host and port | hostname resolution and a short TCP check; no HTTP request, prompts or credentials | when a diagnostic/readiness check probes that declared server |
| the launcher, through the gateway | the selected provider | a connection-test, admission-smoke or qualification request | only after you agree to the listed requests |
| the gateway's sign-in process and your browser | the account provider | sign-in authorization and token exchange | when you choose account sign-in |

Model listings and the public feed go directly from the launcher, not
through the gateway. The launcher also contacts loopback for gateway
status, quota and lifecycle checks; those do not send a provider request.

Isolated Linux x86_64 testing of the pinned Claude Code observed that the
fast-mode setting stopped the fast-mode prefetch in the tested scenario,
but other requests addressed to `api.anthropic.com` still carried the test
client key. The observed paths included `/api/claude_cli/bootstrap` and
`/api/eval/<id>`. These requests were intercepted locally, not sent to
Anthropic.

This is not an exhaustive list of Claude Code's destinations. Equivalent
traffic observations are not available for Linux aarch64, macOS or WSL2.
Do not treat a platform's installation result as a guarantee that
background requests are absent or credential-free.

### Claude Code's own traffic

Managed sessions keep Claude Code's upstream behaviour for its
nonessential traffic: **claude-multi does not guarantee suppressing Claude
Code background traffic.** Claude Code controls its own start-up and
feature-flag requests. Do not assume they are confined to loopback or
credential-free: a managed client's credential is the gateway's local key,
not a provider key. That key only opens your local gateway, which listens
on loopback; rotate it any time with `claude-multi doctor --rotate-token`.
The launcher sets `CLAUDE_CODE_DISABLE_FAST_MODE=1`; this setting is not
a guarantee that all non-model requests are suppressed. Claude Code's
feedback drafts are off in managed sessions by default (Settings →
feedback drafts).

### What providers receive about you

Besides the requests themselves:

- third-party Anthropic-compatible providers receive Claude Code's
  session id, and for agent requests the agent id (and the parent agent's
  where Claude Code sends one), as random identifiers per session;
- the two OpenAI routes (the ChatGPT account and the OpenAI API key)
  receive neither id, but a prompt-cache key the gateway derives from the
  model, the session and the agent: a stable value for one agent of one
  session, from which the ids cannot be read back;
- OpenAI-compatible routes receive none of these;
- claude-multi adds no identifying hint headers of its own.

On the OpenAI API key, the gateway sends the key as a plain API client
(no ChatGPT client version or session header) and adds no hosted tool
the client did not ask for.

Each provider's own terms and retention policies govern what it keeps,
and some differ by product: Meta's contributor tier (the
`muse-contributor` line) is priced lower in exchange for permission to
use your prompts and completions to train future Meta models
([providers/api-keys.md](providers/api-keys.md#meta)).

## What stays on your computer

| What | Where | Notes |
| --- | --- | --- |
| API keys | the key file ([reference/settings.md](reference/settings.md#the-api-key-file)) | mode 0600 |
| account sign-ins | `~/.local/share/claude-multi/` | plaintext token files, mode 0600, in a private directory |
| sign-out and profile backups | next to the originals | kept until you remove them |
| the gateway's configuration and local key | `~/.config/claude-multi/` (`config.yaml`, `api-key`) | contain secrets |
| session records and generated files | `~/.local/state/claude-multi/` | no transcript content |
| the lineup log | `~/.local/state/claude-multi/lineup-log/` | the binding of each agent start; no content |
| hook failures | `~/.local/state/claude-multi/hook-errors.log` | time, event, session and error class; never content |
| gateway logs | `~/.local/state/claude-multi/gateway/logs/` (on demand) or the service journal | can name account files and credential indexes; `claude-multi gateway logs` shows them redacted |

Backup tools, sync clients and desktop search indexers may copy files from
your home folder, these included. Exclude `~/.config/claude-multi/` and
`~/.local/share/claude-multi/` from them if you do not want credentials
copied.

claude-multi never reads Claude Code's transcripts.

## Inside managed sessions

A managed session does not inherit your credentials: every provider key
variable claude-multi knows of, and every other `*_API_KEY` variable in
your shell, is removed from the session's environment, except the names
you list in Settings (**O**) → kept environment (`session_env_keep` in
`choices.json`; names only, never values; provider key names are refused
there). The session's agents are also denied reading claude-multi's
configuration, its credential folders and your key file. See
[security.md](security.md#credential-containment).

## Exports and diagnostics

- `claude-multi export` writes your configuration without keys,
  sign-ins, sessions, logs or evidence
  ([guides/move-machines.md](guides/move-machines.md)).

### Diagnostics you share

- `claude-multi doctor --json` is the report to attach to an issue. Its
  environment block is an allowlist: folders are reported as present or
  not (no paths from your home), the terminal type is generalized, and
  custom hosts and proxy values are left out. Doctor never prints a
  secret.
- `claude-multi --version` prints the release identity (launcher,
  catalog, gateway, Claude Code); a missing component says unavailable.
- Gateway logs are for reading locally: the files can name account files
  and credential indexes. `claude-multi gateway logs` redacts keys, tokens,
  cookies and account identifiers; still check its output before you
  attach it anywhere public.
- Never attach a key file, a sign-in file, `config.yaml`, a transcript or
  an environment dump.
