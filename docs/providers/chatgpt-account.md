# Sign in with a ChatGPT account

You can run OpenAI's models on your own ChatGPT account. This route is for
your own, personal use, and you confirm that before the first sign-in.

## 1. What you need

A ChatGPT account of your own on a plan that includes the models you want.
The sign-in is a device-code flow: it works from any terminal, also over
SSH.

## 2. The personal-use confirmation

Before the first sign-in claude-multi shows its personal-use text once and
asks you to type `personal`. In short: claude-multi is an independent
tool, not affiliated with OpenAI; the sign-in stores your own account's
tokens on this computer for your own sessions only; your plan's limits
apply; and OpenAI has not published terms for this use. Your confirmation
is recorded in `~/.config/claude-multi/choices.json`.

## 3. Sign in

In the launcher: **G** Providers → OpenAI → **L**, or Get started's
providers step. From a terminal outside Claude Code:

```bash
claude-multi providers sign-in openai
```

An address and a one-time code are printed. Open the address on any
device, enter the code and approve; the sign-in finishes in the terminal.
Ctrl-C cancels and changes nothing. This sign-in is separate from any
other ChatGPT or Codex login on your computer.

## 4. Accounts, sign-out and backups

- **G** → **L** lists the saved accounts; with more than one, requests may
  use any of them.
- `claude-multi providers sign-out openai` (or **G** → **L**) moves the
  sign-in records into a kept backup,
  `~/.local/share/claude-multi/auth.signed-out.codex.<date>`, and reloads
  the gateway.
- Sign-in records are plaintext token files under
  `~/.local/share/claude-multi/auth/` (mode 0600); managed sessions cannot read
  them.

## 5. Use it

With the account signed in, the shipped `openai` profile and the OpenAI
slots of other profiles are connected. `claude-multi providers test
openai` sends one small request after you agree. Which models your plan
serves is up to OpenAI; a model the account cannot use fails its requests.

## 6. Limits and quota

Your plan's usage limits apply and vary by plan and workload; claude-multi
cannot tell you your allowance. This sign-in reaches OpenAI's Codex
service, so the relevant limits are your plan's Codex usage limits, which
it shares with your other Codex use. `claude-multi quota`
shows what the gateway observed from recent traffic, with its age; no
reading is not zero use. A profile that runs several agents on one
account spends its limits faster.

OpenAI's own pages, checked on 2026-10-04:

- [Codex pricing](https://developers.openai.com/codex/pricing): which plans
  include Codex, the usage estimates per plan (a five-hour window on some
  plans, with weekly limits that may also apply), what uses limits
  faster, and the usage dashboard that shows what is left;
- [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan).

## 7. OpenAI API keys

An OpenAI Platform API key is a separate route to OpenAI's models, not a
part of this sign-in: it is billed per token to your API organization,
apart from any ChatGPT plan, and it serves only the models reviewed for
the OpenAI API. claude-multi uses either the ChatGPT account or the key,
never both, and never falls back from one to the other. As the Providers
help puts it: “Enter chooses between your account and an API key (one at
a time; an OpenAI API key serves only the models reviewed for it)”. From a
terminal:

```bash
claude-multi providers transport openai api-key
```

While the key is in use, your saved ChatGPT sign-in stays on this
computer but serves nothing. The key's models, its output limit and
OpenAI's project spending limits: [api-keys.md](api-keys.md#openai).

The generic OpenAI-compatible route for your own endpoints is a different
route with its own availability: see [openai-compatible.md](openai-compatible.md).

## 8. When it fails

| You see | Next step |
| --- | --- |
| doctor: the codex OAuth pool has no credential record | sign in: `claude-multi providers sign-in openai` |
| doctor counts `invalid_grant` lines since the sign-in | sign in again |
| requests fail with a usage-limit message | wait for the reset, or move agents with `/cm fallback <provider>` |
