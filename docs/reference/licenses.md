# Licences and notices

claude-multi is released under the MIT licence
([LICENSE](https://github.com/nkoturovic/claude-multi/blob/main/LICENSE)).
This page tells you where the licences and notices of everything a release
ships are; it is not a second copy of them.

## In an installed release bundle

Relative to the bundle's installation directory
(`~/.local/share/claude-multi/install/current/`):

| Path | What |
| --- | --- |
| `share/licenses/claude-multi/` | claude-multi's licence |
| `share/licenses/cli-proxy-api/` | the gateway (CLIProxyAPI, MIT) and its notices: `THIRD_PARTY_NOTICES.txt`, `modules.json` (the Go modules it links, with their licence texts), the Go toolchain's licence, and `CLIProxyAPI/MODIFICATIONS.txt` (the patches claude-multi applies) |
| `share/licenses/python/` | the bundled CPython runtime's licence and the licences of the libraries it includes |
| `share/sbom/` | CycloneDX software bills of materials: `claude-multi.cdx.json`, `cli-proxy-api.cdx.json` and `python.cdx.json` |

The 1.0.0 bundles for `darwin-arm64`, `darwin-x86_64`, `linux-aarch64` and
`linux-x86_64` all contain the notice directories and the three SBOM files
listed above. The Go toolchain licence is
`share/licenses/cli-proxy-api/go/LICENSE`; the CPython licence is
`share/licenses/python/LICENSE.txt`, alongside `INVENTORY.json` and
`THIRD_PARTY_NOTICES.txt`.

## In the Nix package

The gateway at `libexec/claude-multi/cli-proxy-api` is a link into the
separate gateway store output. In that output, the notices are under
`share/cli-proxy-api/licenses/` (including `THIRD_PARTY_NOTICES.txt` and
`modules.json`), and its SBOM is `share/cli-proxy-api/sbom.cdx.json`.

## In the source repository

| Path | What |
| --- | --- |
| `LICENSE` | claude-multi's licence |
| `gateway/licenses/` | the gateway's upstream licence, its Go modules' and toolchain's licences, `modules.json` and `MODIFICATIONS.txt` |
| `gateway/sbom/` | the gateway's CycloneDX SBOM per target |
| `packaging/licenses/python/` | the bundled Python runtime's library licences and their inventory |

Releases carry individually reviewed gateway vulnerability dispositions in [`gateway/vulnerability-dispositions.json`](https://github.com/nkoturovic/claude-multi/blob/main/gateway/vulnerability-dispositions.json), bound to exact gateway binaries, module versions and reported traces, with advisory revisions recorded.

## Claude Code is not redistributed

No release contains Claude Code. claude-multi acquires its own copy of the
pinned version on your computer, from your installation or by a download
from Anthropic that you agree to; Anthropic's terms govern Claude Code.
