# Connect a provider with an API key

Several providers are built in: claude-multi already knows their address,
their key's name and their models, so connecting one is a matter of
saving its API key. API keys are not interchangeable: each provider takes
its own key from its own console, and a key only reaches that provider.

## Providers in this release

<!-- generated: provider-index (tools/docs_gen.py) -->

| Provider | Key name | Address the key is sent to | For this provider alone |
| --- | --- | --- | --- |
| Anthropic ([API key](#anthropic)) | `PLATFORM_ANTHROPIC_API_KEY` | `https://api.anthropic.com` | the shipped `claude` profile |
| DeepSeek | `DEEPSEEK_CLAUDE_API_KEY` | `https://api.deepseek.com/anthropic` | a starter (`claude-multi profile starter`) |
| Kimi | `KIMI_CLAUDE_API_KEY` | `https://api.kimi.com/coding` | a starter (`claude-multi profile starter`) |
| Meta | `META_CLAUDE_API_KEY` | `https://api.meta.ai` | a starter (`claude-multi profile starter`) |
| OpenAI ([API key](#openai)) | `PLATFORM_OPENAI_API_KEY` | `https://api.openai.com/v1` | the shipped `openai` profile |
| OpenRouter | `OPENROUTER_CLAUDE_API_KEY` | `https://openrouter.ai/api` ([its own page](openrouter.md)) | the shipped `openrouter` profile |
| Qwen | `QWEN_CLAUDE_API_KEY` | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic` | a starter (`claude-multi profile starter`) |

Presets are vendors' documented endpoints, filled in for you (`claude-multi providers add --preset <preset>`, or **G** → **N** and **W** → **A** in the launcher):

| Preset | Route | Key name | Address | Support | Available |
| --- | --- | --- | --- | --- | --- |
| `model-studio` (Alibaba Cloud Model Studio) | [anthropic-compatible](anthropic-compatible.md#presets) | `DASHSCOPE_API_KEY` | `https://dashscope-intl.aliyuncs.com/apps/anthropic` | `preset` | yes |
| `moonshot` (Moonshot AI (Kimi platform)) | [anthropic-compatible](anthropic-compatible.md#presets) | `MOONSHOT_API_KEY` | `https://api.moonshot.ai/anthropic` | `preset` | yes |
| `novita` (Novita AI) | [anthropic-compatible](anthropic-compatible.md#presets) | `NOVITA_API_KEY` | `https://api.novita.ai/anthropic` | `preset` | yes |
| `vercel` (Vercel AI Gateway) | [anthropic-compatible](anthropic-compatible.md#presets) | `AI_GATEWAY_API_KEY` | `https://ai-gateway.vercel.sh` | `preset` | yes |
| `zai` (Z.ai (GLM)) | [anthropic-compatible](anthropic-compatible.md#presets) | `ZAI_API_KEY` | `https://api.z.ai/api/anthropic` | `preset` | yes |
| `cerebras` (Cerebras) | [openai-compatible](openai-compatible.md#presets) | `CEREBRAS_API_KEY` | `https://api.cerebras.ai/v1` | `preset` | yes |
| `gemini` (Google Gemini) | [openai-compatible](openai-compatible.md#presets) | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta/openai` | `preset` | yes |
| `groq` (Groq) | [openai-compatible](openai-compatible.md#presets) | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` | `preset` | yes |
| `mistral` (Mistral AI) | [openai-compatible](openai-compatible.md#presets) | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` | `preset` | yes |
| `xai` (xAI (Grok)) | [openai-compatible](openai-compatible.md#presets) | `XAI_API_KEY` | `https://api.x.ai/v1` | `preset` | yes |
| `lan-openai-compatible` (LAN OpenAI-compatible server) | [openai-compatible-lan](lan.md#presets) | none | your server's address | `preset` | yes |
| `llama-cpp` (llama.cpp) | [openai-compatible-lan](lan.md#presets) | none | `http://localhost:8080/v1` | `preset` | yes |
| `lm-studio` (LM Studio) | [openai-compatible-lan](lan.md#presets) | none | `http://localhost:1234/v1` | `preset` | yes |
| `ollama` (Ollama) | [openai-compatible-lan](lan.md#presets) | none | `http://localhost:11434/v1` | `preset` | yes |
| `vllm` (vLLM) | [openai-compatible-lan](lan.md#presets) | none | `http://localhost:8000/v1` | `preset` | yes |

<!-- end of generated: provider-index -->

Every built-in provider except OpenAI speaks the Anthropic Messages
protocol that Claude Code uses, through the local gateway; the OpenAI key goes
through the gateway's route to OpenAI's Responses API
([below](#openai)). Each one alone is enough for a working setup.

A vendor not listed here can still be added: from a **preset** (a
vendor's documented endpoint, filled in for you; see
[anthropic-compatible.md](anthropic-compatible.md#presets) and
[openai-compatible.md](openai-compatible.md#presets)) or as
[your own endpoint](anthropic-compatible.md).

The key name is only the name under which claude-multi saves the key in
its own key file. A variable of that name in your shell is not read: save
the key with claude-multi.

## 1. What you need

An API key from the provider's own console, for an account with billing or
credit set up there. claude-multi never creates accounts or keys.

## 2. Connect it

In the launcher: **G** Providers, select the provider, **K** (or Enter),
then type the key; the input is hidden. Get started's providers step
(**W**) offers the same.

From a terminal outside Claude Code:

```bash
claude-multi providers set-key <provider>                  # hidden input
claude-multi providers set-key <provider> --secret-file <file>   # a private 0600 file
```

Setting a key re-renders the gateway's configuration and verifies that the
running gateway reloaded it. If the gateway refuses the change, or the save
cannot be confirmed on disk, the previous key is put back.

Anthropic and OpenAI also have an account sign-in: their key is chosen
with Enter on the provider, or with
`claude-multi providers transport <provider> api-key`
([Anthropic](#anthropic), [OpenAI](#openai)).

A provider whose key you save before it has any model continues into
adding a model (a listing you agree to, or by hand) and admitting it; see
[guides/models.md](../guides/models.md#add-a-model-of-your-own).

## 3. Where the key is kept

Keys are saved in a private key file (mode 0600, `NAME=value` lines).
The gateway also needs the keys in its private rendered
`~/.config/claude-multi/config.yaml`. Both files contain credentials;
never share either file, and follow the [backup precautions](../privacy.md#what-stays-on-your-computer)
and [diagnostic-sharing rules](../privacy.md#diagnostics-you-share).
Provider keys do not belong in a session's environment, its scope or logs.
Which key file:

1. the file `CLAUDE_MULTI_SECRET_ENV` names, when it is set (the
   supervised gateway service ignores it);
2. otherwise the file `claude-multi setup --keys-file <file>` selected
   (recorded in `~/.config/claude-multi/secret-file.json`);
3. otherwise `~/.config/claude-multi/secrets/provider-keys.env`.

The launcher, the gateway and every key command read the same file.
Managed sessions cannot read it. More: [reference/settings.md](../reference/settings.md#the-api-key-file).

## 4. Replace, remove, disconnect

- Replace: set the key again; claude-multi asks before it replaces a key.
  When other providers use the same key name (a preset added twice with
  its key shared, or a provider of the earlier custom registry), the
  question names every one of them: “<NAME> (<n> chars) is the API key of
  …. Replace it for each of them?” No keeps the key. `--yes` replaces it
  without the question and still names them. If one of those providers is
  removed, or its key changes, between the question and the save, nothing
  is written and the command says to run it again.
- Remove: `claude-multi providers remove-key <provider>` (asks first) or
  **G** → **X** (on Anthropic or OpenAI while its account is in use, **X**
  signs out instead). Profiles that use the provider then show it as not
  connected. A key the provider is using as its selected transport
  (Anthropic or OpenAI) is removed only after you switch back to the
  account.
- Turn a provider off for new sessions without removing its key:
  `claude-multi providers disable <provider>` or **G** → Space. Running
  sessions keep their routes.

If the key may have leaked, also revoke it in the provider's console.

## 5. Check it and use it

`claude-multi providers test <provider>` sends one small request after you
agree (it may be billed), and right before the request checks that the
gateway serves your current setup. Then pick or build a profile that uses
the provider: `claude-multi profile starter` previews a starter from what
is connected. See [guides/profiles.md](../guides/profiles.md).

## 6. Costs and limits

Each request is billed by the provider to your account, at its prices. A
profile runs several models at once, so a multi-agent session multiplies
the spend. claude-multi shows no prices and enforces no spending cap: set
limits and alerts in the provider's console. `claude-multi usage` counts
the requests a session made; that is not a token count or a bill.

Each provider's own pages, checked on 2026-10-04 (prices and limits
change; the provider's page is the authority):

| Provider | Prices | Spending controls and usage |
| --- | --- | --- |
| Anthropic | [Pricing](https://platform.claude.com/docs/en/about-claude/pricing) | [Spend limits](https://platform.claude.com/docs/en/api/rate-limits#spend-limits): each standard usage tier has a monthly spend cap, and you can set a lower limit of your own on the Claude Console's Billing page; usage is on the Console's Usage page |
| OpenAI | [API pricing](https://developers.openai.com/api/docs/pricing) | [Spend limits](https://developers.openai.com/api/docs/guides/spend-limits): a monthly limit for the organization or one project, as an alert or enforced as a hard limit |
| DeepSeek | [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) | usage is deducted from a balance you top up in advance; the page describes no other spending limit |
| Kimi | [Membership benefits](https://www.kimi.com/code/docs/en/kimi-code/membership.html) | a plan quota in a rolling five-hour window, shared by every device and key of the account; optional extra usage, with an optional monthly spending cap |
| Meta | [Pricing and rate limits](https://dev.meta.ai/docs/getting-started/pricing-rate-limits) | pay-as-you-go; the page describes no spending limit |
| OpenRouter | [OpenRouter's costs](openrouter.md#6-costs-and-limits) | account credit and per-key limits |
| Qwen | [Token Plan overview](https://www.alibabacloud.com/help/en/model-studio/token-plan-overview) | a monthly Credits quota per subscription; when it is used up, requests pause until the next cycle |

## 7. When it fails

| You see | Next step |
| --- | --- |
| doctor or the card: the provider's key is missing | `claude-multi providers set-key <provider>` |
| the provider answers 401 or 403 | the key is wrong, revoked or not entitled to the model: create a new key in the provider's console, then set it again |
| 402 or 429 in doctor's gateway lines | the account is out of credit or rate-limited: check the provider's console; move agents with `/cm fallback <provider>` meanwhile |
| requests fail saying a spending or usage limit was reached | a limit you set, or the account's monthly cap: raise it in the provider's console, or wait for the period to reset |
| a key command: “changed while you were deciding — nothing written” | something changed meanwhile; run the command again and check its new preview |

More in [troubleshooting.md](../troubleshooting.md).

## Provider notes

### Anthropic

Claude models can run on your Claude account ([claude-account.md](claude-account.md))
or on an Anthropic API key. Switch the Claude lines to the key (same
models, same selectors) with:

```bash
claude-multi providers transport anthropic api-key
```

or **G** → Enter on Anthropic. The key is sent as `x-api-key` to
`https://api.anthropic.com`. While the API key is selected, the account
serves nothing and there is no fallback between the two;
`claude-multi providers transport anthropic oauth-pool` switches back.
Both switches list the running sessions they move and ask first.

Create the key in the Claude Console, for an organization with billing
set up. A paid Claude plan does not include API usage
([Anthropic's note](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)).

### OpenAI

OpenAI's models run on your ChatGPT account ([chatgpt-account.md](chatgpt-account.md))
or on an OpenAI Platform API key. claude-multi uses one of them at a
time, never both, and never falls back from one to the other.

**The key.** Create an API key on the OpenAI Platform
(`platform.openai.com`), in a project of an organization with billing set
up. API usage is billed per token, apart from any ChatGPT plan
([OpenAI's billing help](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform)).
claude-multi saves it as `PLATFORM_OPENAI_API_KEY` and the gateway sends
it only to `https://api.openai.com/v1`, as `Authorization: Bearer`.

**Switch to it.** **G** Providers → Enter on OpenAI → the API key, or
Get started's picker (**W**), or from a terminal outside Claude Code:

```bash
claude-multi providers transport openai api-key      # asks, then the key (hidden) unless one is saved
claude-multi providers transport openai oauth-pool   # back to the ChatGPT account
claude-multi providers transport openai              # which one is in use
```

Each switch lists the running sessions it moves and asks first;
`--secret-file <file>` takes the key from a private file instead. With
the key in use, **K** on OpenAI (or `claude-multi providers set-key openai`)
replaces it, and switching back offers to remove the key you no longer
use. Your ChatGPT sign-in stays saved while the key is in use, but it
serves nothing.

**Only reviewed models.** The key route serves only the OpenAI lines
reviewed for the OpenAI API, each with the context window, output limit
and efforts from its API model page. This release reviews GPT-6.1 Sol
(`sol`), GPT-6 Astra (`astra`) and GPT-6 Luna (`luna`). A line that is
not reviewed stops being served while the key is in use (doctor names
it), and the switch is refused while no line is reviewed. With the key in
use, **M** Models shows a reviewed line's key-route evidence: its
documented bounds and the date they were checked.

**The output limit is the model's.** Each client turn is one request to
OpenAI's Responses API (with no retry when it succeeds). Claude Code's
per-request output cap is not applied on this route: the gateway does
not pass it on, so one answer can run up to the model's documented
maximum output, at the published price. Set a spend limit before long or
multi-agent sessions.

**Spending controls.** OpenAI's
[spend limits](https://developers.openai.com/api/docs/guides/spend-limits)
set a monthly limit for the organization or for one project, as an alert
or enforced as a hard limit. Over a hard limit, requests fail with HTTP
429 (`project_spend_limit_exceeded` or `organization_spend_limit_exceeded`);
enforcement is not instantaneous, so spend can pass the limit slightly. A
project of its own for claude-multi's key, with a hard limit, bounds what
it can spend. Prices: [OpenAI API pricing](https://developers.openai.com/api/docs/pricing).

**What the gateway sends.** The key goes out as a plain API client: no
ChatGPT client version or session header, and no hosted
image-generation tool the client did not ask for. Any error the provider
returns is replaced with fixed local text, so an error that quotes your
key never reaches a session.

**Not the generic route.** This is a reviewed route to the OpenAI
Platform only. The generic OpenAI-compatible route for other vendors
refuses `api.openai.com` and has its own availability
([openai-compatible.md](openai-compatible.md)).

### DeepSeek

- **Key:** an API key from DeepSeek's platform, with a topped-up
  balance. The key is sent as `x-api-key`.
- **Line:** DeepSeek V4.1 Flash (`deepseek-flash`), efforts high and
  max, for the lead and every agent role.
- **Alone:** `claude-multi profile starter` builds a profile with every
  role on it.
- **Limits:** a forced tool choice that names one tool is refused by
  DeepSeek's endpoint (HTTP 400); Claude Code's ordinary tool use works.

### Kimi

- **Key:** a Kimi Code API key from the Kimi Code console, on a Kimi
  membership plan that includes Kimi Code. The key is sent as
  `x-api-key`. The pay-per-token Kimi API platform is a different
  account and key: use the `moonshot` preset for it
  ([anthropic-compatible.md](anthropic-compatible.md#presets)).
- **Line:** Kimi for Coding (`kimi-code`), efforts low, high and max,
  for the lead and every agent role. Kimi upgrades the model behind this
  name in place.
- **Alone:** `claude-multi profile starter` builds a profile with every
  role on it.
- **Limits:** every device and key of your Kimi account shares one quota
  ([membership benefits](https://www.kimi.com/code/docs/en/kimi-code/membership.html)).

### Meta

- **Key:** an API key from the Meta Model API dashboard
  ([quickstart](https://dev.meta.ai/docs/quickstart)). The key is sent as
  `Authorization: Bearer`.
- **Lines:** Muse Spark 1.3 (`muse`) and Muse Spark 1.3 Contributor
  (`muse-contributor`), efforts low to xhigh (no max), for the lead and
  every agent role. The contributor tier costs less in exchange for
  Meta's permission to use your prompts and completions to train future
  Meta models ([Meta's pricing page](https://dev.meta.ai/docs/getting-started/pricing-rate-limits));
  bind it only to work you are willing to share that way.
- **Alone:** `claude-multi profile starter` builds a profile from these
  lines.
- **Limits:** thinking is always on; a forced tool choice that names one
  tool is refused (HTTP 400).

Meta's route uses strict tool schemas: a tool whose `required` list omits
one of its properties is refused with HTTP 400, and the card warns about
any profile slot on such a route.

### Qwen

- **Key:** a Model Studio Token Plan API key: the plan's own key, from
  the subscription page of the Model Studio console (Singapore region).
  A pay-as-you-go Model Studio key or a Coding Plan key does not work at
  this address; for pay-as-you-go use the `model-studio` preset
  ([anthropic-compatible.md](anthropic-compatible.md#presets)). The key
  is sent as `Authorization: Bearer`.
- **Lines:** Qwen3.8 Max (`qwen-max`) and Qwen3.8 Flash (`qwen-flash`),
  efforts low, medium and xhigh, for the lead and every agent role.
- **Alone:** `claude-multi profile starter` builds a profile from these
  lines.

### Lines read from documentation

Some shipped lines come from the vendor's documentation and have not yet
been checked with a live request through the gateway (the Kimi and Qwen
lines, Meta's, and the OpenAI key route's): their context windows and
efforts are the documented ones. `claude-multi models show <line>` shows
each line's evidence and where its figures come from. A provider that
ships no line for the model you want takes one you add yourself
([guides/models.md](../guides/models.md#add-a-model-of-your-own)).

## 8. Terms

Use of each provider is governed by its own terms and your account there.
A key that lists a model is not proof that your account may use it.
