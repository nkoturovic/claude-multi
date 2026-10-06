# claude-multi documentation

claude-multi starts Claude Code sessions with a **lineup**: a lead model
plus up to nine `cm-*` agents that can run on models from other providers.
Every session is recorded and resumable, and its lineup can change while it
runs. Your own Claude Code installation is not replaced or reconfigured
([STANDALONE.md](STANDALONE.md) explains where the two meet).

This page is the map. `claude-multi --help` prints where these pages are
installed; [CHEATSHEET.md](CHEATSHEET.md) is the one-page lookup of tasks,
keys, files and symptoms.

## Terms

The words the launcher uses, as its help (`?` on the card) defines them:

- **profile**: a saved lineup: the lead model and the `cm-*` agents bound
  to roles.
- **lead**: the model you talk to; **agents**: the `cm-*` subagents it
  delegates to.
- **provider**: where a model runs: an API key or an account sign-in,
  through the local gateway. A provider is **connected** when its selected
  route is locally configured (key/sign-in where required, credential route
  approved) and the gateway serves its models; keyless LAN routes need no key.
  This is not upstream verification.
- **admission**: an optional definition-bound local badge, not route permission.
  `models admit` makes zero inference requests; `models revoke` removes only
  the badge, not availability or qualification evidence.
- **qualification**: optional diagnostics, with explicit human consent to the
  request plan (default No). Missing, failed or stale evidence warns, but does
  not prohibit a valid binding on a usable route.
- **follow / pin**: a following session takes its profile's edits; a
  pinned one keeps its lineup.
- **lead set**: the models `/model` offers in a session: the lead's class,
  narrowed by the profile.
- **LIVE / RELAUNCH**: a lineup change that applies in the running session
  (after `/reload-plugins`), or one recorded for its next resume.
- **GP**: Claude Code's own general-purpose agent.

## Start here

- [Quickstart](quickstart.md): install, connect one provider, pick a
  profile and launch a first session.
- Install: [Linux](install/linux.md) · [macOS](install/macos.md) ·
  [Windows (WSL2)](install/windows-wsl2.md) · [Nix](install/nix.md).
- `claude-multi` with no command opens the launcher: the launch card and
  its screens (**W** Get started, **P** Profiles, **D** Direct, **S**
  Sessions, **G** Providers, **M** Models, **O** Settings, **H** doctor).
  `claude-multi setup` runs the same first steps as plain lines.

## Connect a provider

Any one provider is enough for a working setup; connect only the ones you
want.

- [API keys](providers/api-keys.md): the built-in providers that take an
  API key, Anthropic and OpenAI included.
- [OpenRouter](providers/openrouter.md): one key for models from many
  vendors.
- [Claude account](providers/claude-account.md) and
  [ChatGPT account](providers/chatgpt-account.md) sign-ins, for personal
  use.
- Your own endpoint, or a vendor's from a preset:
  [Anthropic-compatible](providers/anthropic-compatible.md)
  (recommended), [OpenAI-compatible](providers/openai-compatible.md), or a
  [server on your network](providers/lan.md).

## Profiles and models

- [Profiles](guides/profiles.md): the lead and agent roles, shipped and
  starter profiles, named bindings and the default profile.
- [Models](guides/models.md): add and bind without optional admission or
  qualification, family labels versus review independence, and actual shared
  context windows. Supported LAN and legacy custom models can be agents;
  explicit bindings override role recommendations, not route or native limits.

## Sessions and lineup changes

- [Sessions](guides/sessions.md): resume, inspect, stop, forks, adoption
  and repair.
- [Changing a lineup](guides/lineup.md): `/cm` and `/model` inside a
  session, LIVE and RELAUNCH changes, follow and pin.

## The local gateway

- [Gateway](guides/gateway.md): how requests reach other providers, its
  on-demand start, the optional user service and recovery.
- [Networking](guides/networking.md): proxies and custom certificate
  authorities.

## Recovery

- [Troubleshooting](troubleshooting.md): symptom, doctor finding, fix.
- `claude-multi doctor` reports Ready, Attention or BLOCKED with one fix
  for each finding.

## Update, move, uninstall

- [Update](update.md) · [Move to another computer](guides/move-machines.md)
  · [Uninstall](uninstall.md).
- Import never approves a credential destination or imports trusted passing
  evidence. No storage migration is needed for permissive bindings, but older
  releases may refuse broader family labels or newly allowed profiles;
  [rollback limits](guides/move-machines.md#moving-to-an-older-release-or-rolling-back).

## Trust and privacy

- [Privacy](privacy.md): what leaves your computer, and where.
- [Security model](security.md): trust boundaries, signed releases and
  what claude-multi does not protect against.

## Reference

- [Command line](reference/cli.md) · [Tasks by surface](reference/tasks.md)
  · [Settings and files](reference/settings.md) ·
  [Compatibility](reference/compatibility.md) ·
  [Licences and notices](reference/licenses.md).
- Contributing, the architecture and the development workflow live in the
  source repository: [CONTRIBUTING.md](https://github.com/nkoturovic/claude-multi/blob/main/CONTRIBUTING.md)
  and [AGENTS.md](https://github.com/nkoturovic/claude-multi/blob/main/AGENTS.md).
