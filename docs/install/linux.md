# Install on Linux

claude-multi installs for your user from a signed release bundle. The
bundle carries the launcher, its own Python runtime and the gateway; you
need no system Python and no preinstalled Claude Code.

## Architectures and evidence

| Architecture | Channels | Status |
| --- | --- | --- |
| x86_64 | release bundle, Nix package | supported; native release-bundle journeys and the Linux release battery |
| aarch64 | release bundle, Nix package | supported; native release-bundle journey on Ubuntu 24.04 arm64 |

The native release-bundle journeys cover Ubuntu 22.04 and 24.04, Debian 12
and Fedora 44 on x86_64, and Ubuntu 24.04 on arm64. A completed journey is
required before an architecture is claimed as supported. These are not
Nix installation journeys.

Requirements:

- glibc 2.17 or newer;
- `curl` or `wget` for the downloads;
- `ssh-keygen` (OpenSSH 8.1 or newer) to verify the release signature.
  Without it, the installer of a release checks the download against the
  checksums built into that installer instead (see
  [the release trust modes](../security.md#signed-releases)).

## Install

```bash
curl -fsSLO https://github.com/nkoturovic/claude-multi/releases/latest/download/install.sh
sh install.sh
```

The installer downloads the bundle for your architecture, verifies it,
installs it, writes the launchers `claude-multi` and `claude-multi-proxy`
into `~/.local/bin`, and starts `claude-multi setup`. A specific release:
`sh install.sh --version VERSION`, or the installer published with that
release, `https://github.com/nkoturovic/claude-multi/releases/download/vVERSION/install.sh`.

The release signature covers the installer script through its checksum in
`SHA256SUMS`. Downloading and running the script does not authenticate it
first: it is still an input you trust. Read it before you run it, prefer
downloading it to piping it into a shell, and see
[signature verification](../security.md#signed-releases).

### Options

`sh install.sh --help` lists them:

| Option | Effect |
| --- | --- |
| `--version VERSION` | install this release instead of the installer's own |
| `--from-dir DIR` | install from release files you downloaded into DIR |
| `--base-url URL` | download from another https location (`{version}` is substituted) |
| `--allowed-signers FILE` | verify this download against your own allowed-signers file; the installed release keeps verifying its updates with its built-in key |
| `--modify-path` / `--no-modify-path` | add `~/.local/bin` to PATH in your shell profile, or only print the line |
| `--no-setup` | do not start `claude-multi setup` afterwards |
| `--migrate-from-nix` | take over a state directory a Nix installation used |
| `--repair` | finish an interrupted install, repair or rollback |
| `--rollback` | switch back to the previously installed version |
| `--uninstall` | run `claude-multi uninstall` |
| `-y`, `--yes` | accept the default answer to every question |

## Where things go

| Path | What |
| --- | --- |
| `~/.local/share/claude-multi/install/versions/<version>/` | each installed release |
| `~/.local/share/claude-multi/install/current`, `previous` | the release in use and the one before it |
| `~/.local/share/claude-multi/install/installer.json` | the receipt: the launchers and the PATH line the installer wrote, with their sha256 |
| `~/.local/bin/claude-multi`, `~/.local/bin/claude-multi-proxy` | the launchers |
| `~/.local/share/claude-multi/claude/<version>/claude` | claude-multi's own copy of the pinned Claude Code |

Your configuration is in `~/.config/claude-multi/` and session state in
`~/.local/state/claude-multi/`; [reference/settings.md](../reference/settings.md)
lists every file.

If `~/.local/bin` is not on your PATH, the installer prints the line to
add (or adds it with `--modify-path`).

## Claude Code

Setup's `claude` step copies the pinned Claude Code from your own
installation when an exact match exists, or downloads that exact build
after asking. Your own `claude` is never moved, linked or updated.
`claude-multi setup --step claude --claude-from <path>` uses a file you
name. See [reference/compatibility.md](../reference/compatibility.md).

## The gateway

The gateway starts on demand: a launch starts it when needed and leaves it
running. On Linux with a user service manager you can hand it to a
supervised user service instead:

```bash
claude-multi gateway service install
```

The unit is named `claude-multi-gateway`. Its hardened systemd user unit
needs unprivileged user namespaces, which Ubuntu 23.10 and later restrict
by default through AppArmor. Under that restriction, service install
reports that the gateway exited during its start and keeps the on-demand
gateway, which needs no user namespaces. An administrator can lift the
restriction, but that is a system-wide security trade-off; see
[the service requirements](../guides/gateway.md#the-supervised-service-linux).

## One installation per account

Each installation channel (release bundle, Nix, source checkout) keeps its
own files, and the state directory records which one owns it. Another
channel's `claude-multi` refuses with a one-line hint, and
`claude-multi doctor` lists every installation it finds. To move an
account from a Nix installation to the release bundle, run the installer
with `--migrate-from-nix`.

## Interrupted installs

If an install stops after it started changing the installation, gateway
changes stay paused until it is finished: run `sh install.sh --repair`
(with `--from-dir DIR` when you installed from a local directory). A first
install that stopped before any version was in place is installed again.

## Next

- [Quickstart](../quickstart.md), from step 3.
- [Update](../update.md) and [uninstall](../uninstall.md).
- Licences and notices shipped in the bundle: [reference/licenses.md](../reference/licenses.md).
