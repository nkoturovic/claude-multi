# Update claude-multi

## A release bundle

```bash
claude-multi update            # check, show the plan, ask, install
claude-multi update --check    # only check whether a newer release exists
claude-multi update --from-dir <path>   # use release files you downloaded
claude-multi update --rollback # switch back to the previous version
```

On the launch card, **U** runs the same steps. The card's release row is
local information, not a background check for a newer version: it shows
the installed version and release date. At 30 days it shows the release's
age and suggests `claude-multi update --check`.

`update` downloads the latest release's signed checksums and verifies
their signature with the release key this installation carries (it never
falls back to unsigned checksums). The same version is "up to date". A
newer one shows `Update plan: <installed-version> -> <new-version>` first:

- the bundle name, download size and signed-checksum verification;
- installation next to the current release, keeping it for rollback;
- the Claude Code pin and state format, only when they change: a matching
  client is copied from this computer or downloaded and verified before
  the switch; a state-format upgrade on first run prevents rollback to
  the old format;
- whether the gateway changes and needs a restart, or only the launcher
  changes and new sessions use it while the running gateway keeps running.

It asks before it installs; without a terminal it needs `--yes`, and
inside a Claude Code session it refuses. Exit statuses: 0 done or nothing
to do, 1 refused or failed, 2 usage, 3 declined, 130 cancelled. Doctor and
the card show how old the installed release is.

A running gateway keeps the version it started from until you restart it
between turns (`claude-multi gateway restart`); while its persistence hold
is active, a switch that changes the gateway waits.

## What happens to old files

Updates keep `current` and `previous` under
`~/.local/share/claude-multi/install/` and remove other versions (except
one a running gateway still executes), and they remove Claude Code copies
no installed release needs; a copy a running session uses is kept and
reported.

While an update or the installer works, gateway changes are paused (see
[the gateway guide](guides/gateway.md#when-gateway-changes-are-paused)).
If one is interrupted after it started changing the installation, the
next `claude-multi update` finishes it before doing anything else; an
interrupted install or installer rollback is finished by
`sh install.sh --repair`. A switch stopped between `current` and
`previous` is put back as it was.

## Rolling back

`claude-multi update --rollback` (and `sh install.sh --rollback`) switch
back to the previous version. They are command-line only, because they
replace the program the launcher runs. A rollback goes only to the
version it showed you: if the installation changed while it waited for
your answer, it refuses. Afterwards `previous` is the version you left,
so a second rollback undoes the first.

## State only moves forward

A newer release may upgrade the format of claude-multi's state. After
that, an older version refuses to change the state and names the fix
(run the newer version), and a rollback that would go behind the state
format is refused. Rollbacks restore the program, never the state.

## Claude Code with an update

A newer claude-multi release may pin a newer Claude Code. `update` puts
that copy in place before it switches, so a launch never finds a missing
client. Your own Claude Code installation is untouched.

## The Nix package

A Nix installation updates through Nix (your flake or profile);
`claude-multi update` and **U** say so instead of installing anything.
The card says “updated through your Nix flake”. The state rule above
applies to the Nix package too.

## Offline and behind a proxy

`claude-multi update` and the card's **U** use the invoking process's
`https_proxy` / `HTTPS_PROXY` and `no_proxy` / `NO_PROXY` environment
variables, not the gateway's proxy setting. Lowercase wins, including an
empty lowercase override. See [guides/networking.md](guides/networking.md)
for proxy and certificate configuration and its limits.

The installed **1.0.0** updater disables environment proxies. If your
network requires a proxy, use the [installer](install/linux.md#install) or
`claude-multi update --from-dir <path>` once to reach a release with this
fix. The installer uses `curl`'s or `wget`'s proxy settings.

`--from-dir <path>` installs from release files you downloaded yourself
(the signature is still checked), and remains available offline. How old
the installed release is (on the card and in doctor) is the release's date,
also when you are offline.
