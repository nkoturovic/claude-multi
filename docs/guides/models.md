# Models

A **model line** is one model claude-multi can bind to a role, under a
stable key (`opus`, `sol`, …). The release's catalog ships reviewed lines;
you can add your own. `claude-multi models` (or **M** in the launcher)
lists them all.

## Kinds of lines

| Kind | Where it comes from | Before you can bind it |
| --- | --- | --- |
| catalog line | the release, reviewed | connect its provider |
| your line | `claude-multi models add <provider> <wire> <options>`, or **A** in Providers | a valid declaration and a usable provider route; admission and qualification are optional |
| candidate | the pinned gateway's registry or a provider's listing | nothing: a candidate is advice, never a grant; declare it to make it your line |

A line belongs to a **provider** (where it runs: OpenRouter, Anthropic, a
server of yours) and has a model **family** label (who makes it: Anthropic,
OpenAI, DeepSeek, …). You may supply any nonempty, single-line printable
label up to 64 characters, including `mistral`; controls and secret-bearing
values are refused. A line's own label overrides its provider's default;
an omitted aggregator family is `unknown`.

A label is not a certificate of review independence. Review routing prefers
recognized different families, calls recognized equal families **same-family**,
and otherwise reports **independence unknown**. Unrecognized families remain
usable for leads, agents and reviews.

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
with their successors. Three separate facts matter:

- **Use availability:** a valid declaration, an enabled provider and a usable
  route. Route approval, a required credential and a supported selected
  transport still matter; an unavailable line stays visible with its remedy.
- **Admission:** an optional, definition-bound local badge. A new line is
  **New · not admitted**, not off. Missing or stale admission is Attention,
  not a reason to hide it or refuse a binding.
- **Qualification:** optional diagnostic evidence, shown as not run, passing,
  failed or stale. Failure stays visible; being usable never turns it into
  a pass or promises that a provider request will work.

A line whose provider has no key or sign-in is shown as not connected on the
card and in **M**; a line the running gateway does not serve is a doctor
finding with its fix. Local readiness means locally configured and served,
not upstream verified.

**Direct-mode exception:** `claude-multi direct --model <line>` treats a
missing credential as advisory ("requests may fail") and still launches.
The TUI Direct screen warns and asks **Launch anyway?**, default **No**;
answering Yes continues despite the missing credential. Profile and binding
paths retain their credential refusals. This narrow Direct exception does
not bypass route approval, an explicitly disabled provider or an unusable
selected transport; no credential or alternate transport is supplied implicitly.

## Add a model of your own

No step happens by itself; every request to a provider is listed and
needs your yes, in a terminal outside Claude Code (no flag waives this).

In the launcher: **G** Providers → select the provider → **A** add models
(a listing you agree to, with checkboxes, or entry by hand), then return to
**E** on the launch card to bind the line in the profile editor. Skipping
admission does not prevent selection. In **M** Models, Enter changes the
optional admission badge and **Q** offers optional diagnostics.

With the provider already connected and its route usable, add and bind
without either optional step:

```bash
claude-multi models add <provider> <wire> --context <n> --source docs --source-ref "URL, date" --as custom-example
claude-multi profile edit <name>         # choose custom-example for the lead or a cm-* agent
```

A declaration records the wire id, a sourced context size
(`--source docs|registry` needs `--source-ref` with a URL and date),
efforts and role recommendations. A listing without a context needs one
you source; a context above the listed one needs `--over-listed REASON`.
An explicit binding overrides capability and role recommendations with a
warning, including on supported LAN routes and legacy custom lines. It
does not create a missing selector, effort mapping or route contract;
an agent does not inherit a model's lead-only environment.

### Optional admission and diagnostics

```bash
claude-multi models admit custom-example                 # optional local badge; zero inference requests
claude-multi models qualify custom-example --smoke        # optional minimal request, after explicit consent
claude-multi models qualify custom-example --agents       # optional effort, tools and streaming checks too
```

- **Admit** validates the local definition and asks before recording your
  attestation. It sends no inference request and needs no smoke pass,
  running gateway or provider credential just to record the badge. Changing
  the wire, context or efforts makes the badge stale; the display name does
  not. Admission never approves a credential destination.
- **Qualify** lists every bounded request and requires a human's explicit
  yes at a terminal outside Claude Code; the default is **No**, and no flag
  waives it. Checks include `--smoke`, `--efforts`, `--tools` (with
  `--tool-choice forced` or `auto`), `--stream` and `--context`; `--agents`
  combines smoke, efforts, tools and streaming. Qualification writes evidence
  only: it never admits or edits a line. A network failure is inconclusive
  and never erases an earlier pass. Failed tool checks remain failures;
  automatic-only tool evidence is not a forced-tool pass.
- **Use is your choice.** Missing, failed or stale evidence warns for leads,
  agents and workflow defaults; it does not prohibit the binding. Selection,
  launch, doctor and import never run qualification automatically. Declining
  diagnostics sends no request.

When the client or gateway pin changes in a release, earlier evidence stays
recorded but is shown as stale, not a current verification. Doctor shows
Attention suggesting optional requalification.

A provider whose key you save before it has any model continues into this
journey by itself (G → K on a provider with no models).

### A new model on the Claude or ChatGPT account

A new first-party model on a signed-in account can be added the same way:
select Anthropic or OpenAI in **G**, **A** → enter it by hand with its
documented context and efforts. It needs no new route approval. Its agent
qualification also runs an offline check of the pinned Claude Code with
that model id. Missing, failed or unavailable exact-client evidence is a
warning, not an agent-use prohibition. The selected account or API-key
transport must still support the route; there is no implicit fallback.

## Revoke, remove, replace

**Revoke removes only the admission badge. The line remains usable, and
qualification evidence is unchanged.** It sends no inference request and
does not disable the route, remove a binding or change a session's fence.
To stop using a model, remove its binding or declaration; to stop using a
provider, disable that provider in Settings.

```bash
claude-multi models revoke <line>                      # optional badge only; not an availability control
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

Settings (**O**) has the same row. A session's process window is the smaller
of the ceiling and the smallest provider bound among its lead set. A
1M-class agent whose provider bound reaches that window keeps the 1M class
and shares that window; one whose bound falls below it uses the existing
200K fallback. In scalar-window sessions, agents share that process policy.
A new binding using a legacy suffixless selector uses the actual scalar or
200K client class, not 1M merely because its declaration says 1M. There is
no separately adjustable per-agent provider-sized window.

For example, with an 800K process window a line bounded at 872K can keep
its 1M class, while a 500K line falls back to 200K. A 128K provider bound
cannot make a 200K-class agent's window 128K: a request can overflow the
provider before the client compacts. The card, lineup summary (`/cm show`,
`claude-multi profile show <name>`) and `--print-launch` show the actual
client class and effective window; warnings name the process window,
compaction trigger and provider bound where they mismatch. Such predictions
warn rather than block. Never inflate declared capacity to hide a warning.
Concrete invalid-window or native selector limits still refuse.

A session recorded with agents in another class keeps them until its next
resume, which shows each move (for example "agent class 200K → 1M");
doctor lists pending moves as Attention.

Third-party providers' token counts are estimated locally by the gateway,
so context figures on those models are approximate.
