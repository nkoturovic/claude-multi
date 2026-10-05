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
Enter admits a model and **Q** qualifies it for agents; bind it in the profile
editor. Declaration, route approval, a key, admission, qualification and
binding are separate steps: declaring an endpoint alone grants nothing.

From a terminal outside Claude Code (an invented vendor and model with a
200,000-token context limit; use your real model's documented limit, never
inflate it to make it eligible):

```bash
claude-multi providers add acme --kind openai-compatible --base-url https://api.acme.example/v1 --auth bearer --secret-ref env:ACME_API_KEY --family unknown
claude-multi providers set-key acme
claude-multi models add acme acme-large-1 --context 200000 --source docs --source-ref "https://docs.acme.example/models" --as custom-acme-large
claude-multi models edit custom-acme-large
```

`models add` declares a lead-only model. In the editor, keep the existing
fields under `custom-acme-large` and add these fields to request agent use:

```json
{"capabilities": ["lead", "agents"], "roles": ["cm-analyst", "cm-reviewer"]}
```

Save and confirm the edit **before** admission, since changing the declaration
invalidates earlier admission and qualification. Then run:

```bash
claude-multi models admit custom-acme-large
claude-multi models qualify custom-acme-large --agents
```

`providers add` asks you to approve the credential route; `--declare-only`
writes an inert declaration instead. Changing the address or key name needs
fresh approval. After admission, choose the model as a lead; after successful
agent qualification, bind this example to `cm-analyst` or `cm-reviewer` too
([models](../guides/models.md), [profiles](../guides/profiles.md)).

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
request, not automatic admission or qualification. Add and admit the model ids
you choose; qualify any you want to use as agents.

## What the route does

- It is an addition, never the default: one provider, one bearer key,
  HTTPS only, to the address you approve.
- Claude Code's `/v1/messages` requests become upstream
  `/chat/completions` requests, including manual and automatic compaction.
- Declared efforts are passed as the chat API's reasoning effort, clamped
  to the levels you declared.
- Its models still need admission, and agent roles need qualification, as
  for any model of your own ([guides/models.md](../guides/models.md)).
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
