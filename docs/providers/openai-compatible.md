# Connect your own OpenAI-compatible endpoint

Some vendors document only an OpenAI-style chat endpoint
(`/chat/completions`), not Anthropic Messages. claude-multi has a route for
such an endpoint with an API key: the gateway translates Claude Code's
Messages requests into chat requests. Prefer an
[Anthropic-compatible endpoint](anthropic-compatible.md) whenever the
vendor offers one; translation loses some features (below).

## Availability in this build

The keyed OpenAI-compatible route is available in this release. Its complete
compatibility proof covers the gateway, the pinned Claude Code client and
setup journeys against synthetic endpoints; it is not a test of every vendor.
The audit state is part of the release (`audits.openai_compat_keyed` in the
packaged gateway catalog), not a setting you can change or an approval to send
your key anywhere.

In the launcher: **G** Providers → **N** (or **W** Get started → **A**) →
OpenAI-compatible endpoint or a preset below. Give its name and `https`
address, the models' family (`unknown` if unsure), and optionally the model
list's address. The key is always sent as `Authorization: Bearer`. Approve
where the key goes, then type the key; adding models follows. In **M** Models,
Enter changes the optional admission badge and **Q** offers optional
diagnostics; bind it in the profile editor without either step. A valid
model on an enabled, approved route with its key can lead or run agents.
Declaring a keyed endpoint alone never approves its credential destination.

From a terminal outside Claude Code (an invented vendor and model with a
200,000-token context limit; use your real model's documented limit, never
inflate it to silence a context warning):

```bash
claude-multi providers add acme --kind openai-compatible --base-url https://api.acme.example/v1 --auth bearer --secret-ref env:ACME_API_KEY --family unknown
claude-multi providers set-key acme
claude-multi models add acme acme-large-1 --context 200000 --source docs --source-ref "https://docs.acme.example/models" --as custom-acme-large
claude-multi profile edit <name>         # bind custom-acme-large as a lead or agent
```

`models add` defaults to recommending lead use. An explicit agent binding
overrides that recommendation with a warning; you do not have to edit the
capabilities or declare agent roles first. If you want to record your own
recommendations, optionally run:

```bash
claude-multi models edit custom-acme-large
```

Keep its existing fields and, for example, add:

```json
{"capabilities": ["lead", "agents"], "roles": ["cm-analyst", "cm-reviewer"]}
```

These are recommendations, not an agent allowlist. Definition changes can
make existing admission and qualification stale. Optional actions afterwards:

```bash
claude-multi models admit custom-acme-large
claude-multi models qualify custom-acme-large --agents
```

Admission records a local badge with zero inference requests. Qualification
requires explicit human consent, default **No**. It lists every bounded
request, may be billed, and requires a
terminal outside Claude Code. Missing, failed or stale evidence warns; it
never becomes a pass just because the model is bindable. Diagnostics never
run automatically during selection, launch, doctor or import.

`providers add` asks you to approve the credential route; `--declare-only`
writes an inert declaration instead. Changing the address or key name needs
fresh approval. Admission does not approve a route; `models revoke` removes
only its badge, leaving usability and qualification evidence unchanged.
See [models](../guides/models.md) and [profiles](../guides/profiles.md).

A model may have any nonempty, single-line printable family label up to 64
characters, including `mistral`; controls and secrets are refused. The line's
label overrides the provider default. Unknown or unrecognized labels mean
**independence unknown** for reviews, not an agent-use restriction.

Other options:

- the vendor's Anthropic-compatible endpoint, if it has one:
  [anthropic-compatible.md](anthropic-compatible.md);
- the same models through [OpenRouter](openrouter.md), when OpenRouter
  offers them;
- a keyless server on your own network that speaks the OpenAI chat
  protocol: [lan.md](lan.md) (a different route, available in every
  build).

## Presets

The release carries presets (each labelled `preset`, filled in from the
vendor's documentation and not tested by claude-multi) for vendors that
document an OpenAI-compatible endpoint with an API key. All five are
available in Get started and Providers → **N**; the label remains `preset`,
not vendor-tested.

| Preset | Vendor | Address | Key name |
| --- | --- | --- | --- |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEY` |
| `gemini` | Google Gemini (OpenAI compatibility, which Google calls beta) | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| `xai` | xAI (Grok) | `https://api.x.ai/v1` | `XAI_API_KEY` |

They work like the [Anthropic-compatible presets](anthropic-compatible.md#presets).
From a terminal, `claude-multi providers add --preset groq` declares and
approves one, then `claude-multi providers set-key groq` saves its key.
The same options for [a second copy or shared key](anthropic-compatible.md#the-same-preset-twice)
apply. Each has a model-list address: listing models is a separate, consented
request, not automatic admission or qualification. Add the model ids you
choose and bind them as leads or agents; the badge and diagnostics are optional.

## What the route does

- It is an addition, never the default: one provider, one bearer key,
  HTTPS only, to the address you approve.
- Claude Code's `/v1/messages` requests become upstream
  `/chat/completions` requests, including manual and automatic compaction.
- Declared efforts are passed as the chat API's reasoning effort, clamped
  to the levels you declared.
- Its valid models can lead or run agents on usable routes without admission
  or qualification ([guides/models.md](../guides/models.md)). Exact selector
  and effort mappings, endpoint/auth safety and the release's keyed-route
  audit remain required. There is no redirect or protocol fallback.
- `api.openai.com` is refused for this kind: use the dedicated
  [OpenAI Platform API-key route](api-keys.md#openai), not a generic endpoint.

## Translation limits

- Thinking signatures and redacted thinking are not preserved; returned
  unsigned reasoning can disappear when a conversation is replayed.
- Tools lose `is_error` and `strict`, and some schema patterns; invalid
  tool arguments a model returns can become `{}`.
- System prompt `cache_control` is lost; usage and token counts include
  local estimates, not exact billing or context figures.
- A vendor that requires unsigned reasoning replay or
  `max_completion_tokens` is not supported.
- The route's redirect and error safety covers the Messages-to-chat path
  only; image requests and Responses-API requests are outside that
  promise.

Qualification checks forced and automatic tool use separately; a failure
is never retried with a weaker tool contract.

## Costs

Every request is billed by the vendor. claude-multi enforces no cap; set
limits with the vendor.
