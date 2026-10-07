# claude-multi

claude-multi starts Claude Code sessions with a **lineup**: a lead model
plus up to nine `cm-*` agents, each on the model you choose, from
Anthropic or from other providers. A session's lineup comes from a
**profile** or a single **direct** lead, is recorded with the session, and
can change while the session runs (`/cm`). Requests to other providers go
through a local gateway on loopback that claude-multi configures and
runs. Your own Claude Code installation is not replaced or reconfigured.

claude-multi is an independent project, not affiliated with or endorsed
by Anthropic or OpenAI.

**Latest release:** 1.1.0 (the first release was 1.0.0). Downloads are on
[GitHub Releases](https://github.com/nkoturovic/claude-multi/releases/latest)
([CHANGELOG.md](CHANGELOG.md)).

## Install

Linux and macOS:

```bash
curl -fsSLO https://github.com/nkoturovic/claude-multi/releases/latest/download/install.sh
sh install.sh
```

Run this in a terminal outside Claude Code. Interactive setup needs
terminal input and output; piped answers are not enough. To install without
starting setup, use `sh install.sh --no-setup`, then run
`claude-multi setup` in a terminal later.

The installer verifies the release's signed checksums before it installs
anything, then starts `claude-multi setup`. The installer script itself
is what you trust: read it before you run it, and see
[SECURITY.md](SECURITY.md) for the release key and the verification
modes. Windows: run `install.ps1` from the same release to install into
WSL2 ([docs/install/windows-wsl2.md](docs/install/windows-wsl2.md)). Nix
is optional ([docs/install/nix.md](docs/install/nix.md)).

You need neither a Python installation nor a preinstalled Claude Code:
the release carries its own runtime and gateway, and claude-multi gets its
own verified copy of the Claude Code version it pins, copying a matching
build you already have or downloading it after asking.

## Quickstart

```bash
claude-multi                 # the launcher: W Get started, then Enter launches
claude-multi setup           # the same first steps as plain lines
claude-multi -c              # continue the last session in this directory
claude-multi doctor          # what is wrong, with one fix for each finding
```

Connect any one provider (an API key, OpenRouter, or your Claude or
ChatGPT account for personal use) and you have a working setup. The
profile the card launches is yours to choose: by default the one you used
last in this directory, else the default you set, else a profile whose
providers are all connected. The full path:
[docs/quickstart.md](docs/quickstart.md).

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

## Good to know

- Only claude-multi's own, hash-verified copy of the pinned Claude Code
  runs; there is no way to run an unverified client.
- `/model` inside a managed session offers the lead set only; agents
  change with `/cm`. Pressing Enter in `/model` saves the model into your
  global Claude Code settings; press `s` to switch for the session only.
- Models from other providers reach Claude Code through the local
  gateway; they are routes claude-multi checks, not something Anthropic
  supports.
- Provider keys never enter a session's environment or files, and
  claude-multi does not guarantee suppressing Claude Code's own background
  traffic ([docs/privacy.md](docs/privacy.md)).
- Every agent bills separately on pay-per-token providers: set spending
  limits with your providers.

## Documentation

| Page | For |
| --- | --- |
| [docs/USAGE.md](docs/USAGE.md) | the documentation map |
| [docs/quickstart.md](docs/quickstart.md) | a first session |
| [docs/CHEATSHEET.md](docs/CHEATSHEET.md) | tasks, keys, files and symptom → command on one page |
| [docs/STANDALONE.md](docs/STANDALONE.md) | plain Claude Code alongside claude-multi |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptom, doctor finding, fix |
| [CONTRIBUTING.md](CONTRIBUTING.md) | setting up a checkout, testing, building, commits |
| [AGENTS.md](AGENTS.md) | the development guide: architecture, invariants, workflow |
| [SECURITY.md](SECURITY.md) | reporting a vulnerability, release signatures |
| [CHANGELOG.md](CHANGELOG.md) | changes per release |

## Licence

MIT ([LICENSE](LICENSE)). The gateway is CLIProxyAPI (MIT) with patches;
the licences and notices of everything a release ships are listed in
[docs/reference/licenses.md](docs/reference/licenses.md). Claude Code is
not redistributed.
