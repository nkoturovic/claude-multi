# Security model

What claude-multi protects, how, and where its protection ends. To report
a vulnerability, follow the repository's
[security policy](https://github.com/nkoturovic/claude-multi/blob/main/SECURITY.md).

## Trust boundaries

| Component | Trusted for | Not trusted for |
| --- | --- | --- |
| the release you installed | the launcher, the gateway, the bundled runtime | (it is the root of trust: verify it before you install) |
| Claude Code (the pinned copy) | running your session | reading claude-multi's credentials |
| the local gateway | holding provider keys and sign-ins, sending each only to its approved address | anything beyond loopback: it never listens on the network |
| a provider you approved | the requests of the roles you bind to it | other providers' keys |
| a model | its answers | proof of which model actually answered (a provider may substitute) |

## Signed releases

Every release publishes `SHA256SUMS` and its signature
`SHA256SUMS.sshsig` (an `ssh-keygen -Y sign` signature in the
`claude-multi-release` namespace). The installer verifies a download in one
of two modes, exactly as `install.sh` implements them:

- **with `ssh-keygen`** (OpenSSH 8.1 or newer) installed, the signature
  over `SHA256SUMS` must verify against the release key; a failed or
  missing signature stops the install, and nothing replaces it;
- **only when `ssh-keygen` is absent**, the installer published with a
  release verifies that release's download against the checksums built
  into that installer.

`claude-multi update` always verifies the signature, with its built-in
verifier and the key the installed release carries. `--allowed-signers
FILE` changes only that one installation's check, never the trust the
installed updater uses afterwards.

Download the checksum list and signature for the same release as the files
you want to verify. Replace `VERSION` with its version number, without the
leading `v` (for example, `1.0.1`):

```sh
version=VERSION
curl -fsSLO "https://github.com/nkoturovic/claude-multi/releases/download/v${version}/SHA256SUMS"
curl -fsSLO "https://github.com/nkoturovic/claude-multi/releases/download/v${version}/SHA256SUMS.sshsig"
```

Run these commands in the directory containing the downloaded installer
or bundle. Use the same version for every file; do not mix a versioned
download with files from `latest`.

`SHA256SUMS` lists `MANIFEST.json`, every bundle and both installers, so
you can check a download, the installer included, before you run it. The
release key, as an installation carries it:

```text
release@claude-multi namespaces="claude-multi-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJmVeafPpudkejVTvnbP7S31hPCGEsXK6HrAilRzRZai
```

Its fingerprint is `SHA256:LN8n9Tr21eijMbM7Sk7AdQs1FBaQg//dfCc0zeZHD1E`, the
same as in the repository's [security policy](https://github.com/nkoturovic/claude-multi/blob/main/SECURITY.md).
Save that line as `allowed_signers` next to the downloaded files, then:

```sh
awk '{print $3, $4}' allowed_signers | ssh-keygen -l -f -     # prints the fingerprint above
ssh-keygen -Y verify -f allowed_signers -I release@claude-multi -n claude-multi-release \
  -s SHA256SUMS.sshsig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
```

On macOS, `shasum -a 256 -c SHA256SUMS` checks the same; it also reports
the listed files you did not download.

### Bootstrap limits

The installer script, and a command that pipes a downloaded script into a
shell, are inputs you trust: piped into a shell, it runs before anything
checks it. Download it, verify it as above, read it, then run it; on
Windows, `install.ps1` checks the Linux installer's sha256 against the
value published with the release.

## Claude Code integrity

Only claude-multi's own copy of the pinned Claude Code version runs, and
only after its size and full sha256 match the values the release pins
(taken from Anthropic's signed release manifest). There is no switch to
run an unverified client. The launch card checks the copy's size and
metadata only, so a same-size corruption can look ready there; doctor and
every launch check the full hash and refuse a damaged copy. A copy in use
by a running session is locked against removal.

## Credential containment

- Provider keys live in one 0600 key file; sign-ins in a private
  directory; the gateway's key and configuration under
  `~/.config/claude-multi/`. No key is ever written to a session's
  environment, scope, record or a log, or passed on a command line.
- Every managed session is compiled with rules that deny Claude Code's
  Read and Edit tools claude-multi's configuration and credential folders,
  your key file wherever it is, and Claude Code's own credentials file;
  editing claude-multi's state is denied too.
- Sessions get the gateway's key only from a token helper, which hands it
  over only to a gateway proven to be yours.
- Under WSL, the Windows-side Claude Code folders are denied as well; see
  [their limits](install/windows-wsl2.md#what-the-windows-side-protections-cover).

These rules are Claude Code permission rules for its file tools. They are
not a sandbox: a shell command run in a session runs with your user's
permissions.

## Endpoint approval and consent

A provider address receives a key only after you approved that route in a
terminal outside Claude Code. A preset is no exception: it fills in a
vendor's documented address and key name, and you still approve where the
key goes. Switching Anthropic or OpenAI to its API key is a route approval
of the same kind, for that provider's fixed address. Every request
claude-multi itself makes to a provider (a listing, a test, an admission
or qualification check) is listed and needs your yes; no flag waives
this, and these commands refuse inside a Claude Code session.

## Model and permission fences

A managed session's `/model` offers only its lead set; a hook refuses a
switch outside it and asks before a switch to another provider family.
Agents are bound by the compiled lineup. Managed sessions start in
Claude Code's default permission mode unless your own settings choose
another one.

## On-demand and supervised gateway

The on-demand gateway runs as you. The Linux user service runs it
confined: a private temporary home, read-only configuration, only its
credential, trace and working directories writable, and the systemd
hardening options the unit lists.

## Rotation

- `claude-multi doctor --rotate-token` replaces the gateway's local key
  without dropping sessions.
- Provider keys: replace them in the provider's console, then
  `claude-multi providers set-key <provider>`.
- Sign-ins: sign out and in again; revoke sessions in the account's own
  settings.
- The release signing key: updates verify against the keys the
  installed release carries, so a new signing key can reach you only in a
  release signed by a key you already trust. A rotation is stated in that
  trust file: a line for the new key with a `valid-after` date (the day it
  starts signing), and the old key's line given a `valid-before` date (the
  same day). The production key's line carries no dates: it stays valid
  until a rotation states otherwise.

## What claude-multi does not protect against

- a compromised user account or computer: everything here runs as you;
- a provider, or an endpoint you approved, misusing what you send it;
- model output: it can be wrong or harmful, whichever model produced it;
- Claude Code's own behaviour, including its background traffic;
- other programs reading your files, including backup and indexing tools
  copying credential files.

## Sharing diagnostics

Attach `claude-multi doctor --json` and `claude-multi --version` to
reports; see [privacy.md](privacy.md#diagnostics-you-share).
