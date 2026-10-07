# Install with Nix (optional)

The release bundle is the primary way to install claude-multi; Nix is an
optional channel. Its default package contains the launcher, its
documents and the gateway it ships.

## Supported systems

`x86_64-linux`, `aarch64-linux` and `aarch64-darwin`. Intel Macs are not a
Nix system for claude-multi: use the [release bundle](macos.md) there.

## The package

| Flake output | What |
| --- | --- |
| `packages.<system>.default`, `packages.<system>.claude-multi` | the launcher with its gateway |
| `apps.<system>.default`, `apps.<system>.claude-multi` | the `claude-multi` command |
| `apps.<system>.claude-multi-proxy` | the `claude-multi-proxy` command |
| `devShells.<system>.default` | the contributor shell (see CONTRIBUTING in the repository) |

Run it once, or install it into your profile:

```bash
nix run github:nkoturovic/claude-multi
nix profile install github:nkoturovic/claude-multi
```

Those references follow the repository's default branch. To select the
1.1.0 release instead, use its tag:

```bash
nix run github:nkoturovic/claude-multi/v1.1.0
nix profile install github:nkoturovic/claude-multi/v1.1.0
```

The package puts `claude-multi` and `claude-multi-proxy` in `bin/`, the
documents in `share/claude-multi/` and the gateway in
`libexec/claude-multi/`. The flake also exposes gateway build packages
and contributor checks; it has no configuration-module output to import.
Configure claude-multi with its own commands, as on any other channel.

## Claude Code and the gateway

Setup's `claude` step puts claude-multi's own copy of the pinned Claude
Code in place, as with a bundle. The gateway starts on demand; on Linux
`claude-multi gateway service install` hands it to a supervised user
service, which runs the package through
`~/.local/share/claude-multi/nix/current` (a garbage-collector root to the
package you installed it from). Run `claude-multi gateway service install`
again after you switch to a newer package: doctor says "restart pending"
until you do.

## Channel and state

The state directory records the installation that owns it (`nix` here).
A release bundle's launcher refuses that state with a hint; the bundle
installer's `--migrate-from-nix` moves the account to the bundle. Running
both channels for one account is not supported.

## Updating

Update through Nix: `claude-multi update` and the card's **U** say so for
a Nix installation instead of installing anything. State is only ever
migrated forward: after a newer version upgraded the state, an older
package refuses to change it and names the fix. Rolling the Nix package
back does not roll the state back; see [update.md](../update.md#state-only-moves-forward).

## Next

- [Quickstart](../quickstart.md).
- Licences and notices: [reference/licenses.md](../reference/licenses.md).
