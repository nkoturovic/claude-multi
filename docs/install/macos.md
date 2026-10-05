# Install on macOS

claude-multi installs for your user from a signed release bundle, the same
way as on Linux. The bundle carries the launcher, its own Python runtime
and the gateway; you need no system Python and no preinstalled Claude
Code.

## Architectures and evidence

| Mac | Channels | Status |
| --- | --- | --- |
| Apple silicon (arm64) | release bundle, Nix package | supported; native release-bundle journey on macOS 15 |
| Intel (x86_64) | release bundle only (not a Nix system) | built and reproduced, no native journey; no support claim |

The native Apple silicon journey exercises the release bundle on macOS 15.
The gateway runs on demand only; there is no launchd service.

Requirements: `curl` for the downloads and `ssh-keygen` (it ships with
macOS) to verify the release signature.

## Install

```bash
curl -fsSLO https://github.com/nkoturovic/claude-multi/releases/latest/download/install.sh
sh install.sh
```

The installer picks the bundle for your Mac, verifies it, installs it
under `~/.local/share/claude-multi/install/`, writes the launchers into
`~/.local/bin` and starts `claude-multi setup`. The options, the install
root, the receipt and the repair of an interrupted install are the same as
on Linux: [install/linux.md](linux.md#options).

The release signature covers the installer script through its checksum in
`SHA256SUMS`. Downloading and running the script does not authenticate it
first: it is still an input you trust. Read it before you run it; see
[signature verification](../security.md#signed-releases).

The macOS installation checks use command-line installation from a local
release bundle. They do not verify browser-added quarantine attributes or
Gatekeeper prompts. A successful command-line installation does not
guarantee that a browser-downloaded copy will open without a warning.
Release-signature verification is separate from Gatekeeper. If macOS
blocks a download, verify its source and signature before proceeding;
these instructions do not require disabling Gatekeeper or removing
quarantine attributes.

## Claude Code

Setup's `claude` step copies the pinned macOS Claude Code from your own
installation when an exact match exists, or downloads that exact build
after asking. Your own `claude` is never moved, linked or updated.

## The gateway on macOS

The gateway runs on demand only: a launch starts it when needed and leaves
it running, and `claude-multi gateway stop` stops it. There is no
supervised service on macOS (no launchd unit): `claude-multi gateway
service install` is for Linux. Each start writes its own log, and
`claude-multi gateway logs` prints the newest one. On macOS the gateway's
ownership check and session liveness read bounded `lsof` and `ps` output.
See [guides/gateway.md](../guides/gateway.md).

## Next

- [Quickstart](../quickstart.md), from step 3.
- [Update](../update.md) and [uninstall](../uninstall.md).
- Licences and notices: [reference/licenses.md](../reference/licenses.md).
