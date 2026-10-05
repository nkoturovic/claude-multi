# Proxies and custom certificate authorities

Four processes make network connections, and each takes its proxy and its
trusted certificates from a different place. Behind a corporate proxy or a
TLS-inspecting firewall, configure each one you use.

## Who connects where

| Process | Connects to | Proxy | Trusted certificates |
| --- | --- | --- | --- |
| installer (`install.sh`) | the release download location | `curl`'s or `wget`'s own proxy settings | `curl`'s or `wget`'s own CA settings |
| launcher: Claude Code download | `downloads.claude.ai` | the environment's HTTPS proxy settings | `SSL_CERT_FILE` / `SSL_CERT_DIR`, else the Python interpreter's defaults, else a well-known system bundle |
| launcher: `claude-multi update` | the release download location | none: it disables environment proxies for these requests | the launcher's trust, as above |
| launcher: model discovery and public feed | the listing endpoint shown in the request plan, or the public model feed | the environment's proxy settings | the launcher's trust, as above |
| launcher: LAN reachability | a declared keyless server's host and port | direct DNS resolution and TCP connection, not the gateway proxy | no TLS handshake or HTTP request |
| Claude Code (each managed session) | the gateway on loopback; other endpoints upstream Claude Code uses | inherited proxy settings; the launcher adds a loopback bypass when a proxy is set | Claude Code's own trust settings; `NODE_EXTRA_CA_CERTS` is passed through untouched |
| gateway, started on demand | each provider you connect | only the proxy you set in setup, never inherited proxy variables | `SSL_CERT_FILE` / `SSL_CERT_DIR` from the environment that started it |
| gateway, as a user service (Linux) | each provider you connect | only the proxy you set in setup | configured `SSL_CERT_FILE` / `SSL_CERT_DIR` values from the installing terminal are copied into the unit |

The table describes how claude-multi configures each process; it is not
a cross-platform proxy or certificate compatibility guarantee. Isolated
Linux x86_64 client testing exercised a local TLS fixture with a custom
CA, but the platform installation journeys do not verify the client's
loopback proxy bypass or custom-CA loading on every platform. The Linux
service journey does not verify certificate-file or directory visibility,
or certificate variables supplied by the user manager.

Notes:

- **The launcher's trust source.** `claude-multi doctor` names the source
  of the certificates the launcher trusts. A configured location that is
  missing or unreadable is treated as empty: the request fails its
  certificate check rather than falling back to other certificates.
- **The gateway's proxy** is set with
  `claude-multi setup --step gateway --proxy <url>` (an unauthenticated
  `http`, `https` or `socks5` address, no credentials in it) and removed
  with `--no-proxy`. A running gateway reloads with it; a refused render
  puts the old setting back. It is stored in `endpoint.json`.
- **The user service's certificates.** `claude-multi gateway service
  install` (also when it refreshes the unit) carries `SSL_CERT_FILE` and
  `SSL_CERT_DIR` from the terminal it runs in into the unit, bound
  read-only where the unit's view would hide them. It refuses, changing
  nothing, a location that is not a readable certificate file or folder,
  one that is your home folder, a folder above it or a private system
  folder (“the service may see certificates only”), and a path the unit
  file cannot carry (spaces, quotes, `%`, `$`, `:` or backslashes). Point
  the variables at a bundle or a folder of certificates only, then install
  again.
  With neither variable configured by the installer, the unit adds no
  certificate override. Certificate paths supplied only by the user
  manager have not been verified inside the service's restricted
  filesystem view.
- **Sessions configure loopback as direct.** When a proxy is set, every
  managed session is compiled with `127.0.0.1,localhost,::1` in `NO_PROXY`
  and `no_proxy`. These entries configure a bypass for local gateway
  traffic; the platform installation journeys do not verify every
  client's handling of it. Doctor flags a Claude Code settings file whose
  own proxy settings would leave loopback out.

## Under WSL2

Inside WSL2, claude-multi is a Linux program: the proxy variables and the
certificate bundle of your Linux distribution apply, not Windows'
settings. A certificate authority installed in Windows is not trusted in
the distribution until you add it there (or point `SSL_CERT_FILE` and
`NODE_EXTRA_CA_CERTS` at it). WSL's `autoProxy` setting (Windows 11 22H2
and later) can copy Windows' proxy into WSL
([Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking));
the gateway still uses only the proxy you set in setup.

## Certificate errors

1. Find which process failed: the installer prints `curl` or `wget`
   errors; the launcher's errors name the download; a session's provider
   errors come from the gateway (`claude-multi gateway logs`).
2. Give that process your organisation's CA bundle as the table says,
   then retry. For the on-demand gateway, restart it from a terminal that
   has the variables set (`claude-multi gateway restart`).
3. Never turn TLS verification off; claude-multi has no switch for it.

## Endpoint approvals

A proxy does not change which providers sessions may reach: the gateway
sends keys only to the addresses you approved
([providers/anthropic-compatible.md](../providers/anthropic-compatible.md)).
A server on your network ([providers/lan.md](../providers/lan.md)) is an
upstream of the gateway like any other provider.

Keyless requests use the same proxy-aware HTTP client as other
OpenAI-compatible routes. The launcher's LAN reachability check is
separate: it resolves and connects directly to that server, without using
the gateway's proxy.

Configured-proxy delivery of keyless requests is not verified by this
release's end-to-end checks, for either normal or streaming replies.
Those checks do not establish credential absence or the effect of
inherited proxy variables on that proxied path. A successful direct LAN
reachability check does not validate proxy delivery.
