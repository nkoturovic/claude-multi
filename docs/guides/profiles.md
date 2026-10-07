# Profiles

A **profile** is a saved lineup: the lead model you talk to, the `cm-*`
agents it can delegate to (each bound to a model and an effort), which of
Claude Code's own agents stay on, and a few session policies. Every
managed session starts from a profile, or from a single direct lead.

## Roles

The agent ids are fixed: a function, then a grade.

| Function | Agent ids | What it does |
| --- | --- | --- |
| explorer | `cm-explorer` | finds facts with cited paths; read-only |
| analyst | `cm-analyst`, `cm-analyst-strong` | judgment: options, root causes, plans; reports only |
| implementer | `cm-implementer-light`, `cm-implementer`, `cm-implementer-strong` | edits, each in its own Git worktree |
| reviewer | `cm-reviewer`, `cm-reviewer-strong` | verified findings and a verdict; read-only |
| designer | `cm-designer` | interface and documentation structure; reports first |

`-light` is for work you can specify exactly and check; `-strong` is for
work you cannot specify exactly or where a mistake is costly. Binding a
light or strong grade without its recommended plain companion warns but
is allowed. No role prompt names a model: the lineup tells the lead which
model each agent runs. Implementers need a Git
repository: in a directory that is not one they are unavailable (`git
init` enables them).

## Shipped profiles, starters and yours

- **Shipped profiles** come with the release, each a reviewed lineup for
  one set of providers. A shipped profile is read from the release until
  you change it; you cannot rename or remove one, but you can copy it.
- **Starter**: `claude-multi profile starter` previews a profile built
  from the lines that are connected right now; `--apply` saves it (as
  `starter`, or `--name NAME`) after you confirm. Get started's profile
  step offers the same.
- **Yours**: profiles you create or copy, in
  `~/.config/claude-multi/profiles/<name>.json` (mode 0600).

### A profile for the providers you have

Any one connected provider is enough. The shipped `claude`, `openai` and
`openrouter` profiles each need only their own provider (Anthropic by
account or API key, OpenAI by account or API key, OpenRouter); the
others mix providers. For any other single provider (DeepSeek, Kimi,
Meta, Qwen, a preset or an endpoint of your own), the starter is the
way: it takes the roles of the shipped `balanced` profile and fills each
one with a model that is ready now, leaving unbound a role that no ready
model can take. A valid model you added can lead or take agent roles on a
usable route without admission or qualification
([models.md](models.md#add-a-model-of-your-own)). Starter selection prefers
local readiness, not provider-verified success; it can choose unqualified
lines and shows their warnings. Manual choice remains available.
With OpenAI on its API key, only the models reviewed for that key are
ready ([providers/api-keys.md](../providers/api-keys.md#openai)).

### The shipped profiles

<!-- generated: shipped-profiles (tools/docs_gen.py) -->

| Profile | Lead | Agents bound | Needs | What it is |
| --- | --- | --- | --- | --- |
| `balanced` | `opus` ultracode | 8 | Anthropic, OpenAI | Opus lead; Sol high explorer, xhigh analyst/writer and max strong writer/reviewer; Luna max light writer; Opus strong checks |
| `claude` | `opus` ultracode | 8 | Anthropic | Anthropic only: Opus lead, Sonnet writers and volume, Opus strong writer, Opus+Fable reviewers |
| `direct` | `opus` ultracode | none | Anthropic | Opus lead only; no agents |
| `economy` | `opus` ultracode | 8 | Anthropic, OpenAI | Opus lead; Luna xhigh explorer/max light writer; Sol xhigh analyst/writer/reviewer; Astra strong grades; Opus high strong review |
| `max` | `fable` ultracode | 8 | Anthropic, OpenAI | Fable lead and strong analyst; Sol high explorer and xhigh writers; Astra max strong writer/reviewer; Opus xhigh strong review |
| `openai` | `astra` ultracode | 8 | OpenAI | Astra lead and strong grades; Sol high explorer, xhigh analyst/writer, max light writer/reviewer; one OpenAI pool |
| `openrouter` | `or-opus` ultracode | 8 | OpenRouter | OpenRouter only (billed per token): Opus lead and strong writer, GPT analysts and writer, DeepSeek explorer and light writer, Gemini and Grok reviewers |
| `quality` | `opus` ultracode | 8 | Anthropic, OpenAI | Opus lead; Sol high explorer and xhigh writers, Sonnet xhigh strong writer; Astra max and Opus xhigh reviewers |

<!-- end of generated: shipped-profiles -->

## The default profile

The card launches, in this order: the profile you used last in this
directory; otherwise your chosen default, when it is connected; otherwise
the most recently used connected profile; otherwise the first connected
shipped profile, then the first connected profile of yours. Nothing is
fixed: choose a default with **P** → **D**, Settings (**O**) → default
profile, or

```bash
claude-multi profile default <name>     # set it
claude-multi profile default            # show it
claude-multi profile default --clear    # choose automatically again
```

The choice is stored as `default_profile` in
`~/.config/claude-multi/choices.json`. Renaming the default profile moves
the default with it.

## Manage profiles

The **P** Profiles screen lists every profile with its origin and whether
it is connected. Enter uses one; **N** creates (also a starter), **C**
copies, **E** edits, **R** renames, **X** removes one of yours, **U**
restores or updates a shipped one, **D** makes one the default, **B**
edits named bindings, **F** shows only fallback profiles (one provider
each, for an outage) and **W** opens Get started. From a terminal:

```bash
claude-multi profile list
claude-multi profile show <name>              # the evaluated lineup, warnings and review routing
claude-multi profile new <name>
claude-multi profile edit <name>
claude-multi profile duplicate <name> <new>
claude-multi profile rename <name> <new>
claude-multi profile rm <name>
claude-multi profile reseed <name>
```

### The profile editor

**E** on the card or on the Profiles screen opens the editor. ↑/↓ move;
Enter changes a row: the name, the primary provider and lead providers,
the lead and each agent (a model picker where ← → picks the effort), the
native agents (Explore, Plan, general-purpose, workflows) and the review
routing. **U** unbinds an agent, **N** edits named bindings, **R** shows
where reviews route, `^S` saves (`^O` too; only a valid profile saves),
`^G` edits the raw JSON in `$EDITOR`, **?** explains and Esc goes back
(asking when there are unsaved edits). Checks read ✓ valid, ! warning
(never blocks) and ✗ error (Enter jumps to its field).

### Recommendations and technical limits

An explicit binding overrides a model's capability and role recommendations
with a warning. This applies to leads, every `cm-*` grade and named bindings,
including legacy custom lines (no migration or selector rename is needed)
and supported keyless LAN models. Admission badges and qualification are
optional, not eligibility grants; absent, failed or stale evidence stays
visible. Unrecognized family labels remain bindable.

Technical errors still refuse: an unknown role or model, malformed declaration,
disabled provider, unapproved credential route, absent required credential,
unusable selected transport or missing selector/effort mapping. A lead needs
usable lead/context fields. Agents never receive a model's lead-only environment.
Read-only tools, role prompts, `--no-subagents` and writer worktree isolation
remain enforced; unavailable isolation is not permission to write unisolated.

For a client-effort line with one selector, you may choose a native-supported
effort it does not declare, with an **effort unverified for this line** warning.
The client's effort vocabulary is still the limit. Gateway-effort bindings
need the exact declared selector/contract mapping; no effort is invented.

### Backups and conflicts

- Removing one of your profiles, or reseeding a shipped profile you
  changed, keeps a private copy of your version first:
  `~/.config/claude-multi/profiles/.<name>.removed-<time>.json` or
  `.<name>.pre-reseed-<time>.json`. claude-multi never deletes these.
- If the profile changed on disk while you edited it (another window or
  command saved it), saving is refused and nothing is written; reload it
  and make your change again.

### Reseeding

A newer release can ship a newer version of a shipped profile. Doctor then
says so as Attention, and **P** → **U** or
`claude-multi profile reseed <name>` takes it; your changed copy is kept
as a backup. An update never reseeds by itself.

## Sessions that follow a profile

A session started from a profile **follows** it: when you save the
profile, claude-multi offers to apply the change to its running sessions,
LIVE where it can and otherwise recorded for their next resume. A session
that is **pinned** keeps its own lineup. See [lineup.md](lineup.md).

## Named bindings

A named binding gives a model and effort pair a name (`fast`, for example)
that profile slots can use instead of a model; editing the binding changes
every profile that uses it. Manage them with **P** → **B** or in the
editor (**N**). A name starts with a lower-case letter and has at most 32
lower-case letters, digits and `-`; it may not equal a model key. A
binding still in use cannot be deleted, and an edit that would break a
profile using it is refused. They are stored in
`~/.config/claude-multi/bindings.json`. There is no command-line verb for
bindings: they are edited in their validated editor.

## Claude Code's own agents and workflows

A profile decides which of Claude Code's native agents stay on (Explore,
Plan, general-purpose) and keeps native workflows on. What claude-multi
guarantees about workflows (the card's `?` panel):

- workflow agents run as the SESSION model by default (lead family; Settings workflow default binding changes that default), always acceptEdits — scripts may route stages to other models, so unobserved workflow output is family-unknown/mixed and never counts as an independent review verdict
- no cm-role contract; ≤16 concurrent / 1000 per run
- a cm-* agentType keeps its model and effort, but isolation is per call: a workflow writer must pass isolation:'worktree' itself

A workflow default uses the same permissive model rules, but a one-selector
client-effort line can supply only its default effort: a separately chosen
nondefault effort cannot be represented there. Missing forced-tool or
exact-client evidence warns; it does not fix or waive that limit. If you
configure an Explore replacement without binding `cm-explorer`, the profile
warns that Explore remains disabled; it is never silently re-enabled.

## Warnings on the card

A profile's card warns about missing/stale admission, missing/failed/stale
qualification, capability or role recommendations, missing companion grades,
provider concentration, weak strong grades and strict tool schemas. It also
warns when the actual shared client window or compaction trigger can exceed
a model's provider bound. A small provider limit does not create a smaller
per-agent window ([context windows](models.md#context-windows)).

Review routing prefers **recognized different families**. For a recognized
equal pair it reports **same-family**; when either family is unknown or
unrecognized it reports **independence unknown** and uses the usual preferred
bound reviewer. Merely choosing different labels cannot certify independence.
Without a bound reviewer, review remains the lead-only path.

## Cost

On pay-per-token providers the lead and every agent bill separately: a
profile with many agents can cost several times a single-model session.
The card and the starter preview show a spend note per provider; set
limits with each provider. On the OpenAI API key, Claude Code's
per-request output cap does not apply, so each answer can run up to the
model's own output limit. Prices and spending controls per provider:
[providers/api-keys.md](../providers/api-keys.md#6-costs-and-limits).
