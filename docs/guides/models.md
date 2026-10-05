# Models

A **model line** is one model claude-multi can bind to a role, under a
stable key (`opus`, `sol`, …). The release's catalog ships reviewed lines;
you can add your own. `claude-multi models` (or **M** in the launcher)
lists them all.

## Kinds of lines

| Kind | Where it comes from | Before you can bind it |
| --- | --- | --- |
| catalog line | the release, reviewed | connect its provider |
| your line | `claude-multi models add <provider> <wire> <options>`, or **A** in Providers | admit it; qualify it for agent roles |
| candidate | the pinned gateway's registry or a provider's listing | nothing: a candidate is advice, never a grant; declare it to make it your line |

A line belongs to a **provider** (where it runs: OpenRouter, Anthropic, a
server of yours) and to a model **family** (who makes it: Anthropic,
OpenAI, DeepSeek, …). On an aggregator such as OpenRouter the two differ;
reviews are routed by family, and an `unknown` family never counts as an
independent reviewer.

## The release's lines

<!-- generated: model-lines (tools/docs_gen.py) -->

| Key | Model | Provider | Family | Efforts (default) | Context | Roles | `/model` selectors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `astra` | GPT-6 Astra | OpenAI | openai | low, medium, high, xhigh, max (high) | 1M class, provider window 872K | lead and agents | `gpt-multi-astra-<effort>[1m]` |
| `deepseek-flash` | DeepSeek V4.1 Flash | DeepSeek | deepseek | high, max (high) | 1M class, provider window 1M | lead and agents | `claude-multi-deepseek-flash-<effort>[1m]` |
| `fable` | Fable 5.1 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-fable-5-1[1m]` (the effort is chosen in Claude Code) |
| `kimi-code` | Kimi for Coding | Kimi | moonshot | low, high, max (max) | 1M class, provider window 1M | lead and agents | `claude-multi-kimi-code-<effort>[1m]` |
| `luna` | GPT-6 Luna | OpenAI | openai | low, medium, high, xhigh, max (max) | 1M class, provider window 872K | lead and agents | `gpt-multi-luna-<effort>[1m]` |
| `muse` | Muse Spark 1.3 | Meta | meta | low, medium, high, xhigh (xhigh) | 1M class, provider window 1M | lead and agents | `claude-multi-muse-<effort>[1m]` |
| `muse-contributor` | Muse Spark 1.3 Contributor | Meta | meta | low, medium, high, xhigh (high) | 1M class, provider window 1M | lead and agents | `claude-multi-muse-contributor-<effort>[1m]` |
| `opus` | Opus 5.5 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-opus-5-5[1m]` (the effort is chosen in Claude Code) |
| `or-deepseek` | DeepSeek V4.1 Flash · OpenRouter | OpenRouter | deepseek | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `claude-multi-or-deepseek-<effort>[1m]` |
| `or-gemini` | Gemini 3.8 Flash · OpenRouter | OpenRouter | google | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `claude-multi-or-gemini-<effort>[1m]` |
| `or-gpt` | GPT-6.1 Sol · OpenRouter | OpenRouter | openai | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `claude-multi-or-gpt-<effort>[1m]` |
| `or-grok` | Grok 4.7 · OpenRouter | OpenRouter | x-ai | high, xhigh (high) | 200K class, provider window 500K | lead and agents | `claude-multi-or-grok-<effort>` |
| `or-kimi` | Kimi K3 · OpenRouter | OpenRouter | moonshot | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `claude-multi-or-kimi-<effort>[1m]` |
| `or-opus` | Opus 5.5 · OpenRouter | OpenRouter | anthropic | high, xhigh (xhigh) | 1M class, provider window 1M | lead and agents | `claude-multi-or-opus-<effort>[1m]` |
| `qwen-flash` | Qwen3.8 Flash | Qwen | alibaba | low, medium, xhigh (xhigh) | 1M class, provider window 983,616 | lead and agents | `claude-multi-qwen-flash-<effort>[1m]` |
| `qwen-max` | Qwen3.8 Max | Qwen | alibaba | low, medium, xhigh (xhigh) | 1M class, provider window 983,616 | lead and agents | `claude-multi-qwen-max-<effort>[1m]` |
| `sol` | GPT-6.1 Sol | OpenAI | openai | low, medium, high, xhigh, max (high) | 1M class, provider window 872K | lead and agents | `gpt-multi-sol-6-1-<effort>[1m]` |
| `sonnet` | Sonnet 5.5 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-sonnet-5-5[1m]` (the effort is chosen in Claude Code) |

17 retired keys resolve to their successors; `claude-multi models` lists them.

<!-- end of generated: model-lines -->

## Inspect

```bash
claude-multi models                    # every line: generation, provider, class, efforts, status
claude-multi models show <line>        # one line: origin, selectors, efforts, context, admission, evidence
claude-multi models --candidates       # advisory candidates (nothing declared or admitted)
```

The listing shows each line's generation, provider, context class,
efforts and status, its typed `/model` selectors, and the retired keys
with their successors. A line you add starts **New · Off**: declared and
served by the gateway, but offered nowhere until you admit it. A line
whose provider has no key or sign-in is shown as not connected on the
card and in **M**; a line the running gateway does not serve is a doctor
finding with its fix.

## Add a model of your own

No step happens by itself; every request to a provider is listed and
needs your yes, in a terminal outside Claude Code (no flag waives this).

In the launcher: **G** Providers → select the provider → **A** add models
(a listing you agree to, with checkboxes, or entry by hand) → **M** Models
→ Enter admits → **Q** qualifies → **E** binds it in the profile editor.

From a terminal:

```bash
claude-multi discover <provider>                          # one listing request, after you agree
claude-multi discover <provider> --add <wire> --as custom-example
claude-multi models add <provider> <wire> --context <n> --source docs --source-ref "URL, date" --as custom-example
claude-multi models edit custom-example                   # stage roles and family before admission
claude-multi models admit custom-example
claude-multi models qualify custom-example --agents
claude-multi profile edit <name>
```

- **Declare.** A declaration records the wire id, a sourced context size
  (`--source docs|registry` needs `--source-ref` with a URL and date), the
  efforts and the roles you intend. A listing without a context needs one
  you source; a context above the listed one needs `--over-listed REASON`.
- **Admit.** Admission runs a checklist (valid declaration, usable route,
  key present by name, every alias served) and at most one small request
  you agree to. It is bound to the line's definition: changing its wire,
  context or efforts needs a new admission; its display name does not.
- **Qualify.** `claude-multi models qualify <line>` runs bounded checks
  through the gateway after one confirmation listing every request: a
  minimal request (`--smoke`), one per effort (`--efforts`), a tool round
  trip (`--tools`, with `--tool-choice forced` or `auto`), a streamed
  request (`--stream`); `--agents` runs all four. It writes evidence only:
  it never admits or edits a line. An agent role needs passing evidence; a
  network failure is inconclusive and never erases an earlier pass.
- **Bind** the line in a profile ([profiles.md](profiles.md)).

When the client or gateway pin changes in a release, earlier passing
evidence becomes stale: it still counts, and doctor shows Attention
suggesting a new qualification.

A provider whose key you save before it has any model continues into this
journey by itself (G → K on a provider with no models).

### A new model on the Claude or ChatGPT account

A new first-party model on a signed-in account can be added the same way:
select Anthropic or OpenAI in **G**, **A** → enter it by hand with its
documented context and efforts. It needs no new route approval. Its agent
qualification also runs an offline check of the pinned Claude Code with
that model id; where that check cannot run on your platform, the line
stays ineligible for agents with that reason.

## Revoke, remove, replace

```bash
claude-multi models revoke <line>                      # running sessions keep their fence until they relaunch
claude-multi models rm <line>                          # a line you added
claude-multi models rm <line> --successor <line>       # also rewrite the profiles and bindings that use it
```

A line a live session uses is not removed out from under it: the gateway
keeps serving the session's selector (a continuity alias) until no live
session uses it; `claude-multi doctor --prune-aliases` removes unused
ones.

## Context windows

One **window ceiling** governs the lead and every agent: 200K to 800K
tokens, 800K by default.

```bash
claude-multi window-ceiling            # show it, and whether it is set or the default
claude-multi window-ceiling 400K       # set it (applies at the next launch or resume)
claude-multi window-ceiling --reset    # back to 800K
```

Settings (**O**) has the same row. A session's window is the smaller of
the ceiling and the smallest provider bound among its lead set. The lead
and every agent on a 1M-class line whose provider bound reaches that
window run at it; an agent line whose bound is below it runs in the 200K
class with its own smaller window, so it never outgrows its provider. The
card, the lineup summary (`/cm show`, `claude-multi profile show <name>`)
and `--print-launch` show each role's effective window.

A session recorded with agents in another class keeps them until its next
resume, which shows each move (for example "agent class 200K → 1M");
doctor lists pending moves as Attention.

Third-party providers' token counts are estimated locally by the gateway,
so context figures on those models are approximate.
