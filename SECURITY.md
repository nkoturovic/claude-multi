# Security policy

## Reporting a vulnerability

Please report a vulnerability privately, not in a public issue: use
GitHub's private vulnerability reporting on
[nkoturovic/claude-multi](https://github.com/nkoturovic/claude-multi)
("Report a vulnerability" under the Security tab). Include what you
found, how to reproduce it, and the output of `claude-multi --version`
and your platform.

Leave out anything secret: API keys, sign-in files, the gateway's
`config.yaml` or `api-key`, transcripts, environment dumps and raw gateway
logs (they can name account files). `claude-multi doctor --json` is safe
to attach: it is built to leave those out.

You get an acknowledgement within a week. Once a fix is ready we agree on
a disclosure date with you; credit is given unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Release signatures

Every release publishes `SHA256SUMS` and its signature
`SHA256SUMS.sshsig` (an `ssh-keygen -Y sign` signature, namespace
`claude-multi-release`, principal `release@claude-multi`). `SHA256SUMS`
lists `MANIFEST.json`, every bundle and both installers (`install.sh`,
`install.ps1`). The signing key is an Ed25519 key held offline; it never
enters CI.

Release signing key fingerprint: `SHA256:LN8n9Tr21eijMbM7Sk7AdQs1FBaQg//dfCc0zeZHD1E`

It is the key of the `release@claude-multi` line of
[`src/claude_multi/data/release-trust/allowed_signers`](src/claude_multi/data/release-trust/allowed_signers),
the trust every installation carries. A key rotation is stated there: a
line for the new key with a `valid-after` date, and the old key's line
given a `valid-before` date.

How a download is verified, exactly as `packaging/install.sh` implements
it:

- **With `ssh-keygen` (OpenSSH 8.1 or newer) installed,** the signature
  over `SHA256SUMS` must verify against the release key; a failed or
  missing signature stops the install, and nothing replaces it.
- **Only when `ssh-keygen` is not installed,** the installer published
  with a release verifies that release's download against the checksums
  built into that installer.
- `claude-multi update` always verifies the signature, with its built-in
  verifier and the key the installed release carries.
- `install.sh --allowed-signers FILE` changes only that installation's
  check; it never changes the keys the installed updater trusts.

`SHA256SUMS` covers the installers too, but nothing checks an installer
before it runs: the downloaded `install.sh`, and any command that pipes a
download into a shell, are inputs you trust. Download the installer,
verify it against `SHA256SUMS` and its signature
([how](docs/security.md#signed-releases)), and read it before you run it.

## Scope

In scope: the launcher, its installers and self-update, the local gateway
configuration it renders and the patches it applies to the gateway.
Upstream issues in Claude Code or CLIProxyAPI belong to those projects;
tell us too when claude-multi's use of them is affected.

## More

- [The security model](docs/security.md): trust boundaries and what
  claude-multi does not protect against.
- [Privacy](docs/privacy.md): what leaves your computer.
- [Licences and notices](docs/reference/licenses.md).
