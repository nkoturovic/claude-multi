## Session lineup (generated)

- Your agent lineup — every bound `cm-*` agent with its model and effort, the review routing table and the review round cap — is in `/state/scopes/11111111-1111-4111-8111-111111111111/lineup.md` and reaches you as the lineup notice. Read that file whenever no lineup notice is in your context. Agent descriptions never name a model.
- A quiet Sonnet agent is not necessarily hung: Sonnet can move its progress notes into thinking whose summary is omitted, so a productive agent may show no text for a while. For a Sonnet agent, silence alone is neither a death nor a rate-limited provider (this qualifies the silence clause under Failure handling): recover or reroute it only on actual failure evidence (an API error, a 429, a crash or a timeout), and otherwise wait for its result.

## Native-agent policy (generated)

- Explore: native, inheriting your model.
- Plan: native.
- general-purpose: on.
- Workflows: native.

## Context policy (generated)

- Lead class `large`: process compaction window 800000 tokens; deterministic reactive trigger 702000 (90% of the prompt budget). Proactive summary preparation is runtime-controlled and may occur earlier. These numbers hold for every model in your lead set.
- Operating ceiling: the process window is capped below the lead set's smallest provider bound by local operating policy; it is not a route-capability claim.
- Context qualification: at least one lead-set provider bound is not near-limit benchmark-verified; it follows explicit route or operator attestation.

## Standing rules (generated)

- One writer owns an overlapping file scope at a time.
- Invoke `cm-*` agents by exact id; never pass a per-invocation model override.
- `/model` switches only within your lead set; tell the user to press `s` (this session only) so the choice is not saved into their global settings.

## Session sentinel (generated)

- Managed session: 11111111-1111-4111-8111-111111111111.
- If a selected cm-* type is unavailable, stop delegation. Never substitute a native or generic agent.
- Exact relaunch after interruption: ask the user to run `claude-multi -r 11111111-1111-4111-8111-111111111111`.
