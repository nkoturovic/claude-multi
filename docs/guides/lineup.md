# Changing a lineup

A session's lineup can change while it runs: rebind one agent, switch the
whole session to another profile, or fall back to another provider during
an outage. Two commands work inside a session, `/model` for the lead and
`/cm` for everything else; the same changes are available from a terminal
and from the sessions screen.

## `/model` and `/cm`

`/model` inside a managed session lists only the session's **lead set**:
the models of the lead's context class, narrowed by the profile's lead
providers. As `/cm` reminds you:

- /model: press s to switch for this session only — Enter saves the choice into ~/.claude/settings.json, which plain claude then inherits
- /model lists only the lead set; another provider family asks first (Alt+P and /config block instead); a model outside the set is refused — relaunch with a profile whose lead is that model.

If an Enter in `/model` saved a claude-multi model as your global default,
claude-multi tells you in the session and doctor offers the revert.

`/cm` changes the agents and the profile. Its grammar:

`/cm show · /cm profiles · /cm profile <name> · /cm set <agent>=<model>[:<effort>] · /cm unset <agent> · /cm direct [<model>[:<effort>]] · /cm pin · /cm follow · /cm fallback <provider> [--preview] · /cm review [high-stakes] [<range>] · /cm quota`

- `/cm` alone, or `/cm show`, prints the lineup, each role's effective
  context window, and the profiles you can switch to (`*` marks the
  current one).
- Only you can run `/cm`; the model cannot invoke it.
- Everything after `/cm` is one argument: write it without quotes.
- `/cm` runs claude-multi's `lineup` command, so `claude-multi` must be on
  the session's PATH.
- Every change ends with one next step: `next: type /reload-plugins`, a
  line about the lead, recorded for the next resume, or no change.

## LIVE and RELAUNCH

Every change is classified before anything is written:

| Class | When | What happens |
| --- | --- | --- |
| LIVE | only agent bindings change, every new model is inside the fence the session launched with, and the lead stays in the lead set | the agents and the lineup are rewritten at once; type `/reload-plugins` in that session, then start agents fresh (running agents keep their model) |
| RELAUNCH | another lead class, a native-agent or workflow change, a model outside the fence, a context gap, or a model of your own on an agent | recorded as a pending change (↻) and applied at the next resume |

`/reload-plugins` is yours to type: no hook can observe it, so until you
do, new agents keep the previous bindings.

## From a terminal and the sessions screen

The same requests work from a terminal with the session's runtime id
(`runtime_session_id` in `claude-multi sessions show <id>`):

```bash
claude-multi lineup --session <runtime-id> show
claude-multi lineup --session <runtime-id> --preview fallback <provider>
claude-multi lineup --session <runtime-id> set <agent>=<model>:<effort>
claude-multi lineup --session <runtime-id> --relaunch profile <name>
claude-multi lineup --session <runtime-id> --relaunch --its-exited direct <model>
```

`--relaunch` applies a RELAUNCH change now: it asks you to confirm that
the session has exited (`--its-exited` confirms it up front). In the
launcher, **S** → **T** on a session opens the lineup dialog: another
profile, one agent, direct, keep the current lineup (which discards a
pending change) or a fallback provider, each with its effect shown first.
`claude-multi lineup --help` lists the requests.

## Switch profile inside a session

1. `/cm` (the `*` row is the current profile).
2. `/cm profile <name>`: the agents change now; if the new lead is in the
   lead set, switch it with `/model` (press `s`), otherwise it changes at
   the next resume.
3. `/reload-plugins`.

A resume refuses `--profile` with another profile: record the change
first (`/cm profile <name>`, or the `--relaunch profile` form above), then
resume.

## Follow and pin

A session started from a profile **follows** it: saving the profile offers
the change to its running sessions. `/cm set <agent>=<model>`, `/cm unset <agent>`,
`/cm direct`, `/cm pin` and a `/model` switch **pin** the session to its own lineup;
`/cm follow` or `/cm profile <name>` follows again (**F** in the sessions
screen toggles it, after a preview of what changes).

## Fallback during an outage

`/cm fallback <provider>` takes the lead and every bound role from the one
profile whose primary provider is `<provider>`: a ready-made way to move a
session off a provider that is down or out of quota. Unbound roles, native
agents, workflows and overrides stay; the session is pinned. It shows the
preview and its effect (LIVE or RELAUNCH) first; `--preview` writes
nothing. Without such a profile, or when it lacks a role the session
binds, it is refused: create one (the Profiles screen's **F** lists the
fallback profiles you have). For new launches, pick a fallback profile on
the card.

In the sessions screen, **T** opens the lineup dialog: Tab to Fallback,
then ← → or Space chooses among every provider some profile names as its
primary provider and the providers the session already uses (“choose the
provider to fall back to”). Each choice
shows the same preview as `/cm fallback <provider>`, or its refusal when
that provider has no fallback profile. With no fallback profile at all,
the dialog says how to make one: “no provider has a fallback profile yet —
in Profiles, E → General → primary provider makes one”.

`/cm show` helps you decide: below the gateway line it counts the failed
requests of the last 24 hours for each provider the session binds, with
the HTTP status codes and a next step: `/cm fallback <provider>`,
`/cm set <agent>=<model>`, or `/cm profiles` and then `/cm profile <name>`. The count is read from the
gateway's log, so it is “gateway-wide, not only this session”; when the
log cannot be read it says so and counts nothing.

## Direct sessions

```bash
claude-multi direct --model <model>   # or D on the card
claude-multi direct -c                # continue the last session in this directory
```

A direct session has a lead and no `cm-*` agents; Claude Code's own agents
stay on. **D** lists every lead-capable model (models of your own too);
← → picks the effort, and Tab saves the choice as a lead-only profile. A
direct session gains agents later with `/cm profile <name>`.
`--no-subagents` denies delegation for that session (recorded; resume
applies it again).

## Reviews

`/cm review [high-stakes] [<range>]` briefs the lead for a report-only
review routed by the lineup: the reviewer agents from another model family
review the change; high-stakes uses both reviewer grades and the lead
arbitrates. It writes nothing and exists only inside a session.

## Inside the session

The lead receives a **lineup notice** when the session starts, resumes,
compacts or clears, when it forks, and on your next prompt after the
lineup changed. A failing lineup or model-switch hook never blocks your
prompt: the session shows a one-line error naming `claude-multi doctor`,
and one metadata-only line goes to `~/.local/state/claude-multi/hook-errors.log`.

## Tasks at a glance

<!-- generated: lineup-tasks (tools/docs_gen.py) -->

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

<!-- end of generated: lineup-tasks -->
