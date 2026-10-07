# Connect your own Anthropic-compatible endpoint

Any vendor that documents an Anthropic Messages endpoint can be added as a
provider of your own, without a claude-multi release. This is the
recommended kind for your own endpoints: Claude Code speaks Messages
natively, so nothing is translated.

## 1. What you need

The vendor's documented HTTPS base address for its Messages API, an API
key from the vendor, and the exact model ids and context sizes the vendor
documents. For the vendors below, a preset fills in the address and the
key's name for you.

## Presets

A **preset** is a vendor's documented Anthropic-compatible endpoint,
filled in from the vendor's documentation: its address and the key's
name. Each is labelled `preset`: claude-multi has not tested it, and
you must approve where the key goes before using the route. Model admission
and qualification are optional and do not test or approve the vendor for you.
These are available in every build:

| Preset | Vendor | Address | Key name |
| --- | --- | --- | --- |
| `zai` | Z.ai (GLM) | `https://api.z.ai/api/anthropic` | `ZAI_API_KEY` |
| `moonshot` | Moonshot AI (the Kimi API platform, pay per token) | `https://api.moonshot.ai/anthropic` | `MOONSHOT_API_KEY` |
| `model-studio` | Alibaba Cloud Model Studio (pay-as-you-go, Singapore) | `https://dashscope-intl.aliyuncs.com/apps/anthropic` | `DASHSCOPE_API_KEY` |
| `novita` | Novita AI | `https://api.novita.ai/anthropic` | `NOVITA_API_KEY` |
| `vercel` | Vercel AI Gateway | `https://ai-gateway.vercel.sh` | `AI_GATEWAY_API_KEY` |

Notes:

- Moonshot's preset is the pay-per-token Kimi API platform; the built-in
  Kimi provider is the separate Kimi Code subscription
  ([api-keys.md](api-keys.md#kimi)).
- Model Studio's documentation prefers your workspace's own address: give
  it with `--base-url` when you add the preset from a terminal, or change
  the address afterwards with **G** → **E** (a new address is approved
  again).
- Novita, Vercel and Model Studio serve models from several makers: set
  each model's family with `claude-multi models edit <line>`. Any nonempty,
  single-line printable label up to 64 characters is accepted, except
  controls or secret-bearing values. An unknown or unrecognized family
  does not establish review independence; it does not prevent use.
- None of these presets ships models or a model list: after the key, add
  the model ids the vendor documents by hand, then bind them
  ([guides/models.md](../guides/models.md#add-a-model-of-your-own)).

Add one in the launcher with **G** Providers → **N**, or **W** Get
started → **A** (or Enter on its providers step), → the preset: you name
the provider (the preset's name by default), keep or change its address
(Enter keeps the preset's; another address must be `https`), see where
the key goes and approve it, then type the key; adding a model follows. From a terminal outside Claude Code:

```bash
claude-multi providers add --preset zai               # declare it and approve where its key goes
claude-multi providers set-key zai                    # the key, hidden
```

`--as <name>` gives the provider another name, `--base-url <url>` another
address, and `--secret-file <file>` imports the key in the same step.

### The same preset twice

A second provider from a preset whose key another provider already uses
(two Model Studio workspaces, say) gets a key of its own by default: the
preset's key name with the provider's name appended
(`DASHSCOPE_API_KEY_STUDIO_2` for `--as studio-2`), and the first
provider's key stays as it was. Two other choices are explicit:

- `--reuse-key` shares the key already saved (nothing is typed);
- `--replace-key` with `--secret-file <file>` replaces the shared key for
  every provider that uses it, after a question that names them.

Get started's picker asks the same question, with a key of its own as the
default. Once two providers share a key, setting the key on either one
changes it for both.

## 2. What is supported

- One provider has one key, sent as a bearer token or an `x-api-key`
  header to the address you approve.
- Its models start **New · not admitted**: valid declarations on usable
  routes can be bound as leads or agents without admission, qualification
  or declared agent roles. Capability and role recommendations warn rather
  than prohibit an explicit binding.
- Not supported: automatic protocol detection, probing, or falling back to
  another protocol at run time.

## 3. Add it

In the launcher: **G** Providers → **N** → the Anthropic-compatible
endpoint (or **W** Get started → **A**; both list the presets before
the endpoint entry); give the name, the base URL, how the key is sent
(an `x-api-key` header or `Authorization: Bearer`), who makes the models
(its family; `unknown` if unsure) and optionally the model list's
address, approve where the key goes, then type the key. The key is saved
as `<NAME>_API_KEY` after the provider's name (`ACME_API_KEY` for
`acme`). **A** adds models; bind them in the profile editor. In **M** Models,
Enter changes the optional admission badge and **Q** offers optional diagnostics.

The same steps from a terminal outside Claude Code (an invented vendor):

```bash
claude-multi providers add acme --kind anthropic-compatible --base-url https://api.acme.example/anthropic --auth bearer --secret-ref env:ACME_API_KEY --family acme
claude-multi providers set-key acme
claude-multi models add acme acme-large-1 --context 131072 --source docs --source-ref "https://docs.acme.example/models 2026-09" --as custom-acme-large
claude-multi profile edit <name>         # bind custom-acme-large as a lead or agent
```

`providers add` declares the provider and then asks you to approve its
credential route; `--declare-only` writes an inert declaration without
approval. `providers add --preset <name>` starts from a reviewed preset.

### Declaration and approval

Writing a declaration grants nothing. Approving the route
(`claude-multi providers approve <provider>`, or Enter in **G**) is the
step that lets the gateway send your key to that address, and it needs
you at a terminal outside Claude Code. The approval is bound to the
address and key name you saw: changing either needs a new approval.

## 4. Trust: your key and your data

Approving a route means trusting that endpoint with your key and with
everything a session sends to the models you bind there: prompts, file
contents the agents read, tool results. Approve only endpoints you would
trust with that. The address check is a lexical check of the address you
approved, not DNS isolation.

The key is kept in the key file like any other API key
([api-keys.md](api-keys.md#3-where-the-key-is-kept)).

## 5. Admit and qualify

Both actions are optional; neither grants route permission.

- **Admission** (`claude-multi models admit <line>`) records a local badge
  for the valid definition after confirmation, with zero inference requests.
  It needs no passing smoke or live gateway. Changing its wire id, context
  or efforts makes the badge stale, not the model unavailable.
- **Qualification** (`claude-multi models qualify <line> --agents`) runs
  bounded smoke, effort, tools and streaming checks through the gateway.
  It lists every request for a human's explicit approval at a terminal
  outside Claude Code, default **No**. It records evidence only; selection,
  launch, doctor and import never run it automatically. Missing, failed or
  stale evidence remains visible as a warning, not a lead or agent ban.

See [guides/models.md](../guides/models.md).

## 6. Costs

Every request is billed by the vendor at its prices. claude-multi enforces
no cap; set limits with the vendor.

## 7. Apply, verify, remove

- `claude-multi providers apply` (or **G** → **P**, offered when the
  gateway does not serve your current setup) re-renders the gateway's
  configuration and verifies the reload; an invalid change is
  refused and the old configuration keeps serving.
- `claude-multi models revoke <line>` removes only the admission badge:
  the line stays usable and qualification evidence is unchanged. Disable
  the provider or remove a binding/declaration to stop using it.
- `claude-multi models rm <line>` removes a line; `--successor <line>`
  rewrites the profiles and named bindings that use it.
- `claude-multi providers rm <provider>` removes the provider; it is
  refused while its lines are admitted, bound or used by a live session.
  In a terminal it then asks whether to remove the provider's API key too;
  with `--yes`, without a terminal or inside a session it keeps the key
  and prints the command that removes it:
  `claude-multi providers remove-key <provider> --name <key-name>`.

## 8. Compatibility limits

A vendor's "Anthropic-compatible" endpoint may differ from Anthropic's in
tools, thinking, caching or token counting; qualification checks the parts
an agent needs, not everything. Token counts for third-party endpoints are
estimated locally by the gateway, so context figures and compaction
thresholds on those models are approximate.
