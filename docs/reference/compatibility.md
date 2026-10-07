# Compatibility

## Claude Code

Each claude-multi release runs exactly one Claude Code version: the one
its release pins, verified for every platform the release builds. This
release pins **Claude Code 2.1.292**. `claude-multi --version` names the
pinned version, and doctor shows the pin, when it was verified, and the
evidence class for your platform.

### How the copy is acquired

Only claude-multi's own copy runs: `~/.local/share/claude-multi/claude/<version>/claude`.
Setup's `claude` step (`claude-multi setup --step claude`) puts it in
place, trying in this order:

1. `--claude-from <path>`, a file you name;
2. a copy an earlier release retained;
3. your Claude Code versions directory (`~/.local/share/claude/versions`,
   or under `XDG_DATA_HOME`) and the `claude` on your PATH;

taking a file only when its size and sha256 match the pin for your
platform (the values come from Anthropic's signed release manifest). When
none matches, it shows the one download it would make from
`downloads.claude.ai` (the address and size; no credentials are sent) and
asks; no sends nothing. An interrupted download resumes, three tries at
most. Anthropic's installer is never run, and your own installation is
never moved, linked or changed.

### Refusal and checks

- Every launch checks the copy's size and full sha256 before anything
  else happens, and refuses a missing or changed copy with the fix:
  `claude-multi setup --step claude`.
- The launch card checks only the copy's size and metadata, so a damaged
  copy of the right size can look ready there; doctor and every launch
  check the full hash. This difference is deliberate.
- There is no way to run a client that does not match the pin.
- A copy a running session uses is locked; setup and updates keep it.

### Newer Claude Code versions

A newer Claude Code reaches managed sessions with a claude-multi release
that verifies it: releases repin as patch releases, on a regular cadence
and when a client change requires it. Until then doctor shows Attention
when the pin was verified more than 30 days ago or your own Claude Code
is newer; there is nothing to run. Managed sessions disable Claude Code's
own updater, so the managed copy never changes by itself, while your own
`claude` keeps updating as usual.

### Client skew

Claude Code's background supervisor can keep a backgrounded managed
session running on your own, newer `claude`. Its hooks then tell you on
every prompt to `/exit` and resume it with `claude-multi -r <id>`. One
limit: when a session already carries its lineup marker, the per-prompt
check can stay quiet after an upgrade (or while detection is unsure)
until the session's next start; doctor and the next resume report it.

A Claude Code settings file with keys the pinned version does not know (a
newer Claude Code wrote them) makes a launch use the default permission
mode, naming the keys.

### Verified behavior and limits

The Linux client probes use isolated fixture providers, not real accounts.
They verify agent discovery and reload, lifecycle notices, model-switch
fences, effort transmission, helper-token rotation, and compaction. They do
not establish supervisor takeover or a real provider's acceptance of large
contexts or signed reasoning history.

- Agent frontmatter effort is a default: an explicit `effort` on an Agent
  call overrides it and the session effort. Low through max frontmatter
  values reach the wire; `ultracode` in frontmatter is silently ignored by
  the client, so claude-multi rejects it there. A lead's `ultracode` sends
  `xhigh` on the wire.
- Canonical Opus 5.5 with or without `[1m]` books a 1M client window and
  sends a 128K output cap on the first turn, both at startup and after a
  model switch. Only `[1m]` sends the long-context beta. The fixture's
  advertised 300K/16K limits are not fetched through the custom endpoint;
  this is not proof of server-limit negotiation or provider acceptance.
- Hook lineup notices survive startup, resume, compaction and prompts,
  including markup-looking text. The client escapes embedded
  `system-reminder` tag openings; it does not discard the notice.
- After a lineup change, spawn fresh agents. Continuing an old agent with
  SendMessage can use its new model with its old prompt and history.
- A synthetic session made by 2.1.292, including a tool turn and manual
  compaction, resumes on the previous pin, 2.1.286, from its summary without
  replaying the old tool turn. This does not verify every operator session
  or background-supervisor rollback.
- Qualification evidence recorded against an older client remains stored
  with that identity and becomes stale; a re-pin does not rerun paid
  qualification automatically.

## Agent context

The lead and every agent on a 1M-class line run at one window: the
smaller of the window ceiling (800K by default, 200K–800K) and the
smallest provider bound among the lead set. An agent line whose provider
bound is below that window runs in the 200K class. See
[guides/models.md](../guides/models.md#context-windows).

## Platforms

| Target | Channel | Status |
| --- | --- | --- |
| Linux x86_64 | release bundle, Nix | supported; native release-bundle journeys and the Linux release battery |
| Linux aarch64 | release bundle, Nix | supported; native release-bundle journey on Ubuntu 24.04 arm64 |
| macOS, Apple silicon | release bundle, Nix | supported; native release-bundle journey on macOS 15; on-demand gateway only |
| macOS, Intel | release bundle only | built and reproduced, no native journey; no support claim |
| Windows | the Linux bundle inside WSL2 | supported inside WSL2; Windows Server 2025 with Ubuntu 24.04; native Windows is not supported |

A platform is supported only with a completed native install journey in
the release's evidence. A cross-build or a test on another platform does
not establish support. These native journeys exercise release bundles;
listing Nix as a channel does not claim a Nix installation journey.

## Transports

| Route | Status |
| --- | --- |
| Anthropic Messages to the built-in keyed providers (Anthropic API key, DeepSeek, Kimi, Meta, OpenRouter, Qwen) | supported |
| the OpenAI Platform API key, through the gateway's route to OpenAI's Responses API | supported for the models reviewed for it; the client's per-request output cap is not applied ([providers/api-keys.md](../providers/api-keys.md#openai)) |
| Claude and ChatGPT account sign-ins | supported, for personal use |
| Anthropic-compatible endpoints of your own, and the Anthropic-compatible presets | supported; a preset is the vendor's documented endpoint, not tested by claude-multi |
| keyless OpenAI-compatible servers on your network, and their presets | supported, lead only |
| keyed OpenAI-compatible endpoints, and the presets that use them | supported in this release after the complete compatibility proof; HTTPS and bearer-only, with [translation limits](../providers/openai-compatible.md#translation-limits); presets are not vendor-tested |

Anthropic and OpenAI use either their account sign-in or their API key,
never both at once, and never fall back from one to the other.

## Managed policy

A Claude Code managed policy (`/etc/claude-code`, `/Library/Application
Support/ClaudeCode` or `C:\Program Files\ClaudeCode`) can make managed
sessions impossible: a launch refuses when the policy sets version bounds
that exclude the pin, restricts providers without the gateway as a
pinned custom endpoint, sets its own `apiKeyHelper` or a forced login
organization, turns hooks off, or denies a bound model. Ask the policy
owner; claude-multi never edits policy.
