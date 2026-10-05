# Quickstart

The shortest path to a first managed session: install claude-multi,
connect one provider, choose a profile, launch, change one agent, resume.
You do not need to read about model admission or the gateway to get here.

## 1. Install

| You use | Install with |
| --- | --- |
| Linux | the release installer: [install/linux.md](install/linux.md) |
| macOS | the release installer: [install/macos.md](install/macos.md) |
| Windows | the Linux release inside WSL2: [install/windows-wsl2.md](install/windows-wsl2.md) |
| Nix (optional) | the flake package: [install/nix.md](install/nix.md) |

A release bundle carries its own Python runtime and the gateway: you need
no Python and no preinstalled Claude Code. The installer verifies the
signed release before it installs anything, and at the end it starts
`claude-multi setup` (unless you pass `--no-setup`).

## 2. Get claude-multi's copy of Claude Code

Each claude-multi release runs one Claude Code version, and only its own
verified copy of it. The `claude` step of setup puts that copy in place:

- it copies a matching file from your own Claude Code installation when
  one has the exact size and sha256 the release pins (your installation is
  only read, never moved or changed);
- otherwise it shows the one download it would make (the address, the
  size, no credentials sent) and asks first. Answering no sends nothing.

`claude-multi setup --step claude` runs only this step. Details:
[reference/compatibility.md](reference/compatibility.md).

## 3. Open Get started

In a terminal run `claude-multi`. On a new installation the launch card
offers **W — Get started**: this computer, Claude Code, the local gateway,
providers, an optional connection test, a profile and a final check. Enter
runs the step under the cursor; nothing is saved until you confirm.

Without the full-screen interface, `claude-multi setup` runs the same
steps as plain lines, and `claude-multi setup --status` shows which are
done.

### A prepared setup

`claude-multi setup --answers <file>` applies a prepared setup after one
preview of everything it would do. API keys are named by file only (an
absolute path to a private, one-line key file); sign-ins and connection
tests are refused in an answers file because they need you at the
terminal. Every entry is planned against the state before the run, so an
entry that depends on a provider an earlier entry of the same file
declares is refused, with the whole file, before anything is shown. For
example, OpenRouter alone with a starter profile:

```json
{
  "version": 1,
  "claude": {"source": "auto"},
  "gateway": {"start": true},
  "providers": [{"id": "openrouter", "api_key_file": "/home/user/keys/openrouter.key"}],
  "profile": "starter"
}
```

## 4. Connect one provider

Every route is optional; connect only what you want, and any single
provider gives you a working setup. Get started's providers step (**W**,
or **A** from any step) lists them all: the accounts and the built-in
providers first, then the presets, the providers you added, and last your
own endpoints and the servers on your network;
`claude-multi setup --step providers` lists the same, numbering the ones
you can choose.

| Route | Credential | In the launcher | From a terminal | Alone gives you |
| --- | --- | --- | --- | --- |
| Claude account, ChatGPT account | an account sign-in, for personal use | **G** → **L** on Anthropic or OpenAI | `claude-multi providers sign-in anthropic` (or `openai`) | the shipped `claude` or `openai` profile |
| Anthropic or OpenAI API key | the provider's API key | **G** → Enter on Anthropic or OpenAI → the API key | `claude-multi providers transport anthropic api-key` (or `openai`) | the same Claude lines; for OpenAI, the models reviewed for its API (see the note below) |
| a built-in keyed provider: DeepSeek, Kimi, Meta, Qwen | the provider's API key | **G** → **K** on the provider | `claude-multi providers set-key <provider>` | its shipped lines, through a starter profile |
| OpenRouter | an OpenRouter API key | **G** → **K** on OpenRouter | `claude-multi providers set-key openrouter` | the shipped `openrouter` profile |
| a preset of a vendor's Anthropic-compatible endpoint (Z.ai, Moonshot AI, Model Studio, Novita AI, Vercel AI Gateway) | the vendor's API key | **W** → **A** → the preset | `claude-multi providers add --preset <name>` | the models you add and admit |
| a preset of an OpenAI-compatible endpoint (Groq, Mistral AI, xAI, Cerebras, Google Gemini) | the vendor's API key | **W** → **A** → the preset | `claude-multi providers add --preset <name>` | the models you add and admit |
| your own Anthropic-compatible endpoint (recommended for anything not listed) | the vendor's API key | **G** → **N** → Anthropic-compatible | `claude-multi providers add <name> --kind anthropic-compatible <options>` | the models you add and admit |
| your own OpenAI-compatible endpoint | the vendor's API key | **G** → **N** → OpenAI-compatible | `claude-multi providers add <name> --kind openai-compatible <options>` | the models you add and admit, through Messages-to-chat translation ([limits](providers/openai-compatible.md#translation-limits)) |
| a server on your network (Ollama, LM Studio, vLLM, llama.cpp, any OpenAI-compatible server) | none | **G** → **N** → the server | `claude-multi providers add --preset ollama` | a lead model (for a direct session or a profile's lead) once you add and admit one; no agents |

On the OpenAI API key, Claude Code's per-request output cap is not
applied: one answer can run up to the model's own output limit, billed
per token. Set a hard spend limit on the key's OpenAI project first
([OpenAI's spend limits](https://developers.openai.com/api/docs/guides/spend-limits);
[details](providers/api-keys.md#openai)).

Pages: [API keys](providers/api-keys.md) (the built-in providers, the
Anthropic and OpenAI keys), [OpenRouter](providers/openrouter.md),
[Claude account](providers/claude-account.md),
[ChatGPT account](providers/chatgpt-account.md),
[Anthropic-compatible endpoints and presets](providers/anthropic-compatible.md),
[OpenAI-compatible endpoints](providers/openai-compatible.md),
[servers on your network](providers/lan.md).

"Connected" means a sign-in is saved, a key is saved (for a provider you
added, with its route approved), or the provider is a server on your
network that needs no key; whether the gateway serves its models is shown
beside it. It does not prove that your account is
entitled to a model or that a request will succeed; the optional test
step sends one small request per provider after you agree, and it may be
billed.

### One provider, start to finish

Each of these is a complete setup on its own (after steps 1 and 2).

**A keyed provider alone** (DeepSeek here; Kimi, Meta and Qwen work the
same way):

```bash
claude-multi providers set-key deepseek     # the key, hidden
claude-multi profile starter --apply        # a starter from what is connected, after its preview
claude-multi doctor --first-run
claude-multi
```

**An account sign-in alone** (your Claude account):

```bash
claude-multi providers sign-in anthropic    # type personal once, then sign in in the browser
claude-multi --profile claude
```

**OpenRouter alone**, from a prepared setup: the answers file in
[A prepared setup](#a-prepared-setup) above, then

```bash
claude-multi setup --answers <file>
claude-multi doctor --first-run
```

The same file with `{"id": "openai", "transport": "api-key", "api_key_file": "…"}`
as its provider entry sets up the OpenAI API key alone.

**A custom endpoint alone** (the Z.ai preset; your own endpoint works the
same way after `claude-multi providers add`):

```bash
claude-multi providers add --preset zai     # approve where the key goes
claude-multi providers set-key zai
claude-multi models add zai <wire> --context <n> --source docs --source-ref "https://docs.z.ai/ 2026-10" --as custom-example
claude-multi models admit custom-example    # one small request, after you agree
claude-multi profile starter --apply
```

An admitted model can lead at once; agent roles also need it qualified
(`claude-multi models qualify custom-example --agents`), see
[guides/models.md](guides/models.md#add-a-model-of-your-own).

## 5. Know what a session costs

A profile runs several models at once: the lead and every agent you bind
bill separately on pay-per-token providers. The card, Get started and
`claude-multi profile starter` show a spend note for the providers a
profile uses. Set spending limits with each provider before you start
long multi-agent work; see the provider pages.

## 6. Choose a profile

A **profile** is a saved lineup: the lead model and the agents bound to
roles. Get started's profile step (or `claude-multi profile starter`)
builds a **starter** from what is connected, or you pick a shipped profile
whose lines are all connected. The card shows the profile it will launch:
the one you used last in this directory, else your chosen default, else a
connected profile. Make one the default with **P** → **D** or
`claude-multi profile default <name>`. More: [guides/profiles.md](guides/profiles.md).

## 7. Check, then launch

```bash
claude-multi doctor --first-run   # the first-run checks in order, one fix each
claude-multi                      # the launch card: Enter launches
```

Claude Code may show its own first-run and folder-trust prompts. These
belong to Claude Code, not to claude-multi, and depend on its existing
configuration. Read and answer any prompts before continuing. The release
installation checks use print-mode sessions and do not verify a particular
interactive first-run screen sequence on Linux, macOS or Windows through
WSL2.

## 8. Change one agent

Inside the session, type:

```text
/cm show
/cm set implementer=<model>
/reload-plugins
```

`/cm show` lists the lineup and the profiles you can switch to;
`/cm set <agent>=<model>` binds one agent to another model; the change takes effect for new agents
after you type `/reload-plugins` in that session. Only you can run `/cm`,
not the model. More: [guides/lineup.md](guides/lineup.md).

## 9. Exit and resume

Exit Claude Code as usual. Later:

```bash
claude-multi -c          # continue the last session in this directory
claude-multi -r          # pick a session on the sessions screen
```

The session keeps its lineup across exits, updates and restarts. More:
[guides/sessions.md](guides/sessions.md).

## When something is wrong

`claude-multi doctor` names one fix for each finding;
[troubleshooting.md](troubleshooting.md) maps symptoms to fixes and
[CHEATSHEET.md](CHEATSHEET.md) is the one-page lookup.
