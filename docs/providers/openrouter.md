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

A model OpenRouter offers that is not a reviewed line can be added as a
model of your own: list OpenRouter's models (one request you agree to;
the listing needs no key), declare one, admit it, and qualify it before
an agent role uses it. That is the advanced journey in
[guides/models.md](../guides/models.md#add-a-model-of-your-own); the
reviewed lines do not need it.

## 5. Replace or remove the key

Set the key again to replace it (claude-multi asks first);
`claude-multi providers remove-key openrouter` removes it. Profiles that
use OpenRouter then show the provider as not connected until you set a
key again.

## 6. Costs and limits

OpenRouter bills per token from your account's credit, at each model's
listed price, and every agent of a profile bills separately: a profile
that runs nine agents on OpenRouter can spend several times what a
single-model session does. claude-multi shows no prices and enforces no
cap. Use OpenRouter's own credit limits and per-key limits, and watch
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
