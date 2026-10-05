# Uninstall claude-multi

One command removes claude-multi; the installer's `--uninstall` runs the
same command.

```bash
claude-multi uninstall --dry-run    # show the plan and remove nothing
claude-multi uninstall
```

It is command-line only, because it removes the program the launcher
runs. It prints its whole plan first and removes nothing before you answer
y/N. Credentials are removed only after you type `delete credentials`.

## Options

| Option | Effect |
| --- | --- |
| `--dry-run` | show the plan and remove nothing |
| `--keep-setup` | keep your setup and session state: profiles, settings, records |
| `--keep-credentials` | keep your API keys, sign-ins and the local gateway key, without asking |
| `--yes` | skip the y/N question (never the typed one for credentials) |
| `--force` | remove even when sessions may still be running (they are named) |

## What it removes, and what it keeps

- **Removed:** the installed releases, the launchers and the PATH line the
  installer wrote (only as its receipt proves them: each launcher's
  sha256 and the exact line, nothing else in the file), claude-multi's
  copies of Claude Code, the gateway service it installed, and, unless you
  keep them, your claude-multi configuration and session state.
- **Credentials:** every API-key file (the one an environment variable
  selects, the one the service reads, the default one), sign-in records
  and the gateway key count as credentials. They go only after the typed
  phrase; with `--keep-credentials` they stay without a question. A key
  file is never removed without the typed phrase.
- **Always kept:** your own Claude Code and its configuration
  (`~/.claude`), every transcript, backups claude-multi made (sign-out
  backups, profile backups), the previous release when it is needed for a
  rollback, and a launcher you changed after the plan was shown.
- A folder that is a link stays with what it points to. A Nix-owned path
  is left to Nix.

## When it refuses

It refuses, removing nothing of the affected store, while:

- a session may still be running (no recorded end, a background or
  running Claude Code naming it, or anything that cannot be read);
  `--force` overrides and names them;
- another claude-multi command is writing session state or one of the
  stores it would remove (it waits a moment first);
- a sign-in save is unconfirmed (also with the gateway stopped);
- an installer, update, machine move or service hand-off holds the
  gateway (the message names the owner and its fix);
- the gateway service cannot be checked, or was not written by
  claude-multi;
- the pointer to your key file cannot be read (fix or delete
  `~/.config/claude-multi/secret-file.json` first).

Fix what the message names, then run it again.

## On Windows (WSL2)

Uninstall runs inside the distribution and leaves WSL and the
distribution in place; it never runs `wsl --unregister`.

## The Nix package

Remove the package with Nix. `claude-multi uninstall` from a Nix
installation still removes claude-multi's configuration, state and
credentials as above, and leaves the Nix-owned files to Nix.

The plan's program row says
“managed by Nix — remove claude-multi from your Nix configuration”.
The Nix service link is kept too; removing the package is a separate
Nix operation.
