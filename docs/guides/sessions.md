# Sessions

Every managed session is recorded: its lineup, its project directory, its
profile (or direct lead) and its history of changes. It can be resumed
with the same lineup after an exit, an update or a restart. Claude Code's
transcript stays Claude Code's: claude-multi never reads, moves or deletes
it.

## Two ids

- The **managed id** is claude-multi's stable id for the session (a UUID;
  inside the session it is `$CLAUDE_MULTI_MANAGED_ID`). Commands that take
  `SESSION` accept it, a prefix of 8 or more characters, or the session's
  name.
- The **runtime id** is the id Claude Code runs the session under. It
  changes after `/clear`, a compaction or a fork adoption; claude-multi
  follows it. `claude-multi sessions show <id>` prints it as
  `runtime_session_id`, and `claude-multi doctor -v` prints one
  `Session <id>` line per session, with `→ runtime <runtime-id>` when the
  two differ.

## Continue and resume

```bash
claude-multi -c                  # continue the last session in this directory
claude-multi -r <id>             # resume one session (id, 8+ character prefix, or name)
claude-multi -r                  # open the sessions screen
```

Resume reopens the same transcript with the recorded lineup, compiled
fresh against the installed release, plus any change you recorded for it
(the resume card shows that change). Resume runs from the recorded
project directory, so it works from anywhere.

Before a resume launches, claude-multi checks what could go wrong and asks
when something needs a decision:

- **running in the background** (●): resuming would fork it. Stop it and
  resume, resume anyway, or cancel. `--force` skips only this check.
- **needs a choice** (!): the lead's model line is gone with no
  successor; choose a profile or a direct model.
- **identity repair needed** (!): repair and resume keeps the recorded
  project directory.
- **transcript missing**: restore it from a backup, or forget the session;
  claude-multi never deletes transcripts.
- **project directory moved**: point the session at the new directory
  (the screen offers Relink).

A resume after a release that changes a session's agent context class
shows each move, for example "agent class 200K → 1M".

## The sessions screen

**S** on the card, or `claude-multi -r`. Managed sessions are listed first
(this directory's, **C** for all), then native Claude Code sessions.

| Key | Action |
| --- | --- |
| Enter | resume (a live one opens the takeover question; ⚠ opens adopt or discard; ! opens the chooser) |
| V | details: identity, directory, runtime id, lineup, the pending change and why it waits, routing and the last day's observed requests |
| T | change its lineup ([lineup.md](lineup.md)) |
| F | follow its profile, or pin its lineup |
| R | rename (shown here only) |
| X | forget its record and generated files (the transcript stays) |
| E | stop a session running in the background |
| M | mark ended |
| P | repair its generated files |
| L | link a native session to a profile or a direct model |
| C | this directory, or all directories |

Marks: ● running · ◐ unknown (no end recorded, no process seen) · ○ ended
· ↻ relaunch change recorded · ⚠ fork waiting · ! needs a choice.

From a terminal: `claude-multi sessions list` (`--json` for one
document) and `claude-multi sessions show <id>` (the record as JSON on
stdout; notices on stderr).

## What a session did

```bash
claude-multi explain <id>        # recorded intent, compiled fence, spawn binding, gateway route, observed execution
claude-multi usage --since 24h   # observed client requests per model and session
```

A binding in the lineup is not proof of the model the provider served:
`explain` keeps intent and observation apart, and an unknown join stays
unknown. `usage` counts completed client requests, not upstream attempts
or billed tokens.

## Stop, end, forget

- **Stop** a session running in the background: **E**, or
  `claude-multi sessions stop <id>`. It uses Claude Code's own
  `claude stop`; the conversation is kept and can be resumed.
- **Mark ended** a session that exited without recording its end:
  **M**, `claude-multi sessions mark-ended <id>`, or
  `claude-multi sessions mark-ended --all-dead`. Refused while the session
  is live.
- **Forget** a session: **X**, or `claude-multi sessions forget <id>`
  (asks first). It deletes the record, the generated scope and the lineup
  log; the transcript stays, and **L** can link it again.

When liveness cannot be determined (a background process or a daemon
entry cannot be read), forget and stop ask you to type the session id;
`--force` is the command-line form, only for when you know it is safe.
Mark ended has no override.

## Native forks

Claude Code forks a session when a second process opens a session another
process owns, typically one the background supervisor adopted. The fork
is a new session with a copy of the transcript. claude-multi notices it
and blocks resuming the parent until you decide (⚠):

- **Adopt** the fork as its own session: Enter → Adopt, or
  `claude-multi sessions link <fork-id> --profile <name>`.
- **Discard** the marker: Enter → Discard, or
  `claude-multi sessions resolve-fork <id> <fork-id>`. The fork's
  transcript stays on disk.

`sessions resolve-fork` first refuses a marker it cannot discard, then
names the fork, what discarding it removes and the command that would
adopt it instead, and asks y/N (default No). No changes nothing (“Nothing
was changed: fork <fork-id> is still pending.”, exit status 3). `--yes`
discards without the question; without a terminal, `--yes` is required.

Either decision retires the fork's hook credential. No decision is needed
when the fork already is the live branch: the marker clears by itself.

## Adopt a plain Claude Code session

```bash
claude-multi sessions link <id> --profile <name>    # or --direct <model>
```

or **L** on its row. The session becomes managed under a new managed id;
the transcript is untouched. An adopted conversation keeps its original
system prompt until its next compaction; lineup notices apply at once.

## Repair

```bash
claude-multi doctor --repair <id>     # rebuild one session's generated files from its record
claude-multi doctor --repair-all      # every session that is not running
claude-multi sessions relink-runtime <id> <runtime-id>   # the record names the wrong runtime id
```

Generated files are rebuilt from the record; never edit them or the
record by hand. `--repair-all --include-live` also repairs sessions that
may be running; do that only when you know they can take it.

## When providers change under a running session

A session keeps its lineup, but its requests go through the gateway as it
is now:

- turning a provider off (**G** → Space, `claude-multi providers disable <provider>`)
  applies at the next launch or resume; running sessions keep their
  routes;
- switching Anthropic or OpenAI between the account and the API key
  moves running sessions at their next request; the switch lists them
  before you confirm;
- removing a provider's key, or signing out of an account, stops its
  models being served: that provider's requests fail in every session
  until you set a key or sign in again;
- revoking a model's admission leaves running sessions their fence until
  they relaunch; removing a model you added leaves it to the sessions
  that already use it until they end; a provider you added cannot be
  removed while a live session uses it.

During an outage, `/cm fallback <provider>` moves a session's roles to a
profile on another provider ([lineup.md](lineup.md#fallback-during-an-outage)).

## Limits

- A session that was running when its liveness could not be read counts
  as live or unknown: removals and repairs that could harm it refuse.
- With the state directory on a network file system, sessions running on
  another computer are invisible here, so forget, stop and mark ended
  treat their liveness as unknown.
- Claude Code finds transcripts by their original directory; resume
  through claude-multi, which runs from the recorded directory.
