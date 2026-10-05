# Install on Windows (WSL2)

On Windows, claude-multi runs inside WSL2: the Linux release is installed
into a Linux distribution and runs there. Native Windows is not supported.

Release-bundle status for Windows through WSL2:
**supported inside WSL2; Windows Server 2025 with Ubuntu 24.04**.
The support label requires the Windows installer and the subsequent
installed-session journey to complete; passing installer unit tests alone
does not establish support.

## Install

Download `install.ps1` from the release and run it in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

It is published next to the Linux installer, at
`https://github.com/nkoturovic/claude-multi/releases/latest/download/install.ps1`.
The script:

1. checks that WSL2 is available and, when you agree, installs it;
2. picks the distribution (`-Distribution NAME`, default: your WSL default
   distribution), or creates one when you agree;
3. downloads the Linux installer, checks its sha256 against the value
   published with the release, and runs it inside the distribution;
4. starts `claude-multi setup` there (unless you pass `-NoSetup`).

Exit status 3 means WSL2 or a distribution was just installed: restart
Windows, or finish creating the Linux user as the message says, then run
the script again. `-Yes` answers yes to installing WSL2 or a distribution.

The embedded checksum applies only to the exact installer URL embedded in
`install.ps1`. If `-InstallerUrl` (or a templated URL changed by `-Version`)
selects another URL, supply `-InstallerSha256` from an authenticated
release. The bootstrap refuses to download or run an installer without a
checksum. See [signature verification](../security.md#signed-releases).

From then on, open the distribution (for example from the Start menu or
with `wsl`) and use `claude-multi` there, exactly as on
[Linux](linux.md).

## Windows and WSL are separate

Everything claude-multi uses lives inside the distribution: its own copy
of the Linux Claude Code, your API keys and sign-ins, profiles and
sessions. A Claude Code installed on Windows is not used and is not
changed. Doctor says so under WSL, in these words:

> “WSL: the Windows-side and WSL-side Claude configurations are separate — sign-ins, settings and sessions of Claude Code on Windows are not seen here, and the reverse”

So connect providers and sign in from inside the distribution, even if you
already did so in Claude Code on Windows. Under WSL the Claude account
sign-in opens its address in your Windows browser (through `wslview` or
`explorer.exe`), or prints the address for you to open when neither is
available; the ChatGPT sign-in uses a device code, which works anywhere.

## Model servers and the network

- A model server running on Windows (for example Ollama or LM Studio for
  Windows) is not at `localhost` from inside WSL in WSL's default
  networking mode: add it with the Windows host's address, and let the
  server accept connections from it, or turn on WSL's mirrored
  networking mode, where `127.0.0.1` reaches Windows
  ([Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking);
  [servers on your network](../providers/lan.md)).
- Proxies and certificate authorities: the distribution's own settings
  apply, not Windows' ([networking](../guides/networking.md#under-wsl2)).

## Keep state and projects on the Linux file system

- claude-multi's state must be on the Linux file system: a state directory
  on a Windows drive (`/mnt/c/…`) is refused, because locks and private
  file modes do not work there. Leave `XDG_STATE_HOME` unset, or point it
  under your Linux home.
- Keep projects in the Linux file system too: doctor notes a working
  directory under `/mnt/` because file access is slow there and file modes
  are not kept.

## When WSL shuts down

The WSL virtual machine stops when every WSL terminal is closed, and the
gateway stops with it. Nothing is lost: the next launch or resume starts
the gateway again. A session that was running reconnects through its token
helper, which starts a stopped gateway (waiting at most 10 seconds) and
hands the session its token only when that gateway is proven to be yours
and ready. If a session reports that it cannot reach the gateway, run
`claude-multi gateway status`, then `claude-multi gateway start`; see
[troubleshooting.md](../troubleshooting.md#the-gateway).

## What the Windows-side protections cover

Under WSL, every managed session is compiled with fixed rules that deny
Claude Code's Read and Edit tools these Windows-side folders, for every
Windows user:

- `//mnt/c/Users/*/.claude/**` (Claude Code's configuration on Windows);
- `//mnt/c/Users/*/AppData/Roaming/Anthropic/**`.

Their limits:

- they are Claude Code permission rules for its file tools, not a
  sandbox: a shell command run in the session is not restricted by them;
- they cover drive C mounted at `/mnt/c` only: another drive, or a
  different WSL mount root, is not covered;
- other Windows-side files (other programs' credentials, browser data,
  files under your Windows profile outside those two folders) stay
  readable from WSL as they are for any Linux program.

claude-multi's own credentials inside the distribution are denied to
sessions on every platform; see [security.md](../security.md#credential-containment).

## Uninstall

`claude-multi uninstall` inside the distribution removes claude-multi as
on Linux ([uninstall.md](../uninstall.md)). It leaves WSL and the
distribution in place and never runs `wsl --unregister`; remove the
distribution yourself if you no longer need it.

## Next

- [Quickstart](../quickstart.md), from step 3.
- [Gateway](../guides/gateway.md) and [troubleshooting](../troubleshooting.md).
- Licences and notices: [reference/licenses.md](../reference/licenses.md).
