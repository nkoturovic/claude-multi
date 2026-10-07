# Connect OpenRouter

OpenRouter routes one API key to models from many vendors. claude-multi
ships OpenRouter as a built-in provider with reviewed model lines and a
shipped `openrouter` profile, so OpenRouter alone gives you a working
setup: connect the key, choose the profile, launch.

## 1. What you need

An OpenRouter account with credit and an API key from it. The key is
saved as `OPENROUTER_CLAUDE_API_KEY` and sent only to
`https://openrouter.ai/api`.

## 2. Connect, then choose a profile

1. **G** Providers → OpenRouter → **K**, type the key (hidden). Or:

   ```bash
   claude-multi providers set-key openrouter
   ```

2. Choose a profile. The shipped `openrouter` profile runs every role on
   OpenRouter's reviewed lines; `claude-multi profile starter` previews a
   starter built from what is connected. On the card, **P** lists the
   profiles and **D** makes one the default.

   ```bash
   claude-multi profile show openrouter
   claude-multi --profile openrouter
   ```

The reviewed OpenRouter lines need no admission or qualification from
you: they are catalog lines, checked for the release.

<!-- generated: openrouter-lines (tools/docs_gen.py) -->

| Key | Model | OpenRouter model | Family | Efforts | Context |
| --- | --- | --- | --- | --- | --- |
| `or-deepseek` | DeepSeek V4.1 Flash · OpenRouter | `deepseek/deepseek-v4.1-flash` | deepseek | high, xhigh | 1M class, provider window 1M |
| `or-gemini` | Gemini 3.8 Flash · OpenRouter | `google/gemini-3.8-flash` | google | high, xhigh | 1M class, provider window 1M |
| `or-gpt` | GPT-6.1 Sol · OpenRouter | `openai/gpt-6.1-sol` | openai | high, xhigh | 1M class, provider window 1M |
| `or-grok` | Grok 4.7 · OpenRouter | `x-ai/grok-4.7` | x-ai | high, xhigh | 200K class, provider window 500K |
| `or-kimi` | Kimi K3 · OpenRouter | `moonshotai/kimi-k3` | moonshot | high, xhigh | 1M class, provider window 1M |
| `or-opus` | Opus 5.5 · OpenRouter | `anthropic/claude-opus-5.5` | anthropic | high, xhigh | 1M class, provider window 1M |

The shipped `openrouter` profile: OpenRouter only (billed per token): Opus lead and strong writer, GPT analysts and writer, DeepSeek explorer and light writer, Gemini and Grok reviewers.

| Role | Line | Effort |
| --- | --- | --- |
| lead | `or-opus` | ultracode |
| `cm-analyst` | `or-gpt` | high |
| `cm-analyst-strong` | `or-gpt` | xhigh |
| `cm-explorer` | `or-deepseek` | high |
| `cm-implementer` | `or-gpt` | high |
| `cm-implementer-light` | `or-deepseek` | high |
| `cm-implementer-strong` | `or-opus` | xhigh |
| `cm-reviewer` | `or-gemini` | high |
| `cm-reviewer-strong` | `or-grok` | xhigh |

<!-- end of generated: openrouter-lines -->

## 3. Provider and model family

On OpenRouter, the provider is OpenRouter but each model belongs to its
own vendor's family (Anthropic, OpenAI, DeepSeek, Google, xAI, Moonshot
and so on). Reviews are routed by family, so an OpenRouter profile can
still get an independent review from another family. The card and
`claude-multi profile show <name>` show each slot's family.

## 4. Other OpenRouter models

List models with **G** Providers → OpenRouter → **A** → **List models**, or
run this in a terminal outside Claude Code:

```bash
claude-multi discover openrouter
```

The public listing makes one anonymous request to `/api/v1/models`, after
confirmation. Every regular model stays visible, even with missing facts.
With a configured key, a **second, separately confirmed** request to
`/api/v1/models/user` adds account-only rows. It uses the same key, in the
Bearer header only. This listing is filtered by the account's provider
preferences, privacy settings and guardrails; it is not a complete inventory.
Declining or a failed account listing keeps the public rows. Without a key,
only the public listing is requested. Each request is bounded to 20 seconds
and 4 MiB, with no redirects or retries.

Both surfaces show advertised context, modality, tool support and
prompt/completion prices per token; missing or malformed facts are **unknown**.
Zero prompt and completion prices are marked **free (advertised)**, not a
promise that every feature or request is free. **?** in the TUI listing shows
the full facts when a row is wider than the terminal.

### Stealth models and add by id

A `stealth/` id, such as `stealth/space-bunny-alpha`, is marked **stealth**:
**maker hidden; pre-release; may retain prompts; may disappear**. Its family
is `unknown`, so it cannot establish review independence. The `openrouter/`
namespace instead contains **routers**, not stealth models. Stealth models
use the same OpenRouter API and key, but the public listing may omit them.
The account listing may also omit them; whether an account lists a stealth
model is not established by the offline fixtures.

Add a known stealth id directly, even when neither listing contains it:

```bash
claude-multi discover openrouter --add stealth/space-bunny-alpha
```

This asks to fetch the anonymous
`/api/v1/models/stealth/space-bunny-alpha/endpoints` metadata, then explicitly
declares that id. Its advertised name, modality, context and tools are used
when present, otherwise left unknown. Facts stated per upstream endpoint
are used only when every endpoint agrees; disagreements stay unknown.
If the endpoint states no context,
repeat with `--context N` using a bound you have verified in the provider's
documentation; a declined or failed lookup also permits that manual bound.
No inference request is made. In the TUI use **A** → **Add stealth id**;
review the facts and the prefilled declaration before saving. **Manual entry**
(and `models add … --context N --source docs --source-ref URL`) remains a
no-lookup path.

Ids with suffixes such as `vendor/model:free` stay visible with **not addable in
this release**: the wire grammar does not accept `:`. Do not strip the
suffix to declare a different model by accident.

Listing alone never declares, enables, admits, binds or chooses a default.
Only `--add` or the TUI declaration preview writes a model of your own;
it changes no profile or provider enablement. Admission and qualification
are optional badges, not prerequisites to explicit use on an available
route. See [guides/models.md](../guides/models.md#add-a-model-of-your-own).

## 5. Replace or remove the key

Set the key again to replace it (claude-multi asks first);
`claude-multi providers remove-key openrouter` removes it. Profiles that
use OpenRouter then show the provider as not connected until you set a
key again.

## 6. Costs and limits

OpenRouter bills per token from your account's credit, at each model's
listed price, and every agent of a profile bills separately: a profile
that runs nine agents on OpenRouter can spend several times what a
single-model session does. claude-multi shows listing prices as observations
and enforces no spending cap. Use OpenRouter's own credit limits and per-key limits, and watch
usage in its dashboard. `claude-multi usage` counts requests, not tokens
or cost.

OpenRouter's own pages, checked on 2026-10-04:

- [Pricing](https://openrouter.ai/pricing): how OpenRouter charges;
  each model's per-token price is on its page in
  [the model list](https://openrouter.ai/models).
- [Limits](https://openrouter.ai/docs/api_reference/limits): the
  account's credit balance and the optional credit limit of each API key.
  A key at its limit, or a balance that cannot cover a request, returns
  HTTP 402; the response says which.
- [Activity](https://openrouter.ai/docs/guides/administration/activity-export):
  the activity page shows spend, tokens and requests, grouped by model or
  by API key.

A key of its own for claude-multi, with a credit limit, bounds what its
sessions can spend.

## 7. When it fails

| You see | Next step |
| --- | --- |
| 401 from OpenRouter | the key is wrong or revoked: set a new one |
| 402 in doctor's gateway lines | the account is out of credit: add credit, or move agents with `/cm fallback <provider>` |
| one model refuses requests while others work | that model's upstream vendor may be unavailable on OpenRouter; bind another line with `/cm set <agent>=<model>` |
| a model of your own fails qualification | the check names the failing part; choose another model, never a weaker tool contract |

## 8. Terms

OpenRouter's terms and each upstream vendor's terms apply to your
requests. A model OpenRouter lists is not proof that your account may use
it at the price or limits you expect.
