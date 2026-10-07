# The local gateway

Claude Code speaks Anthropic's Messages protocol. To reach other
providers, every managed session sends its requests to a local gateway: a
patched build of CLIProxyAPI that claude-multi configures, starts and
checks. It listens on loopback (127.0.0.1) only. Plain `claude` never uses
it.

## On demand by default

The gateway starts when a launch or a resume needs it and keeps running.
Each new installation records a free port (in the range 18317–18336) in
`~/.config/claude-multi/endpoint.json` on its first start.

```bash
claude-multi gateway status     # backend, endpoint, instance, log, recent starts, persistence hold (sends no token)
claude-multi gateway start      # start it and wait until it is ready (up to 45 s)
claude-multi gateway restart    # stop, then start
claude-multi gateway stop
claude-multi gateway logs       # the newest log lines (-n N; --instance <id>)
claude-multi gateway ensure --quiet   # for scripts: exit 0 only when your gateway is ready
```

In the launcher, **H** (doctor) offers start, restart (**r**) and the log
(**l**); the Providers and Direct screens offer a start when the gateway
is down. Stopping stays a terminal command.

Exit statuses: 0 ok; 1 refused, paused, failed, not ready, busy, held or
inhibited (the message says which); 2 usage; 3 a declined confirmation;
130 cancelled.

### Only a gateway proven to be yours

`stop` and `restart` act only on a gateway proven to be yours: its lock,
its start record, the process and the listening socket must agree. A port
held by anything else is refused, and no token is sent to it.

### A bounded graceful stop

On an ordinary stop or restart, the gateway cancels queued credential refreshes
and waits for already-running refresh workers, including their credential saves
and save reports. Its 30-second shutdown budget starts when shutdown begins,
not when the gateway starts. HTTP requests still use graceful draining.

This reduces the chance of losing a newly rotated sign-in token; it is not an
unconditional token-survival guarantee. An active provider refresh can itself
use 30 seconds, leaving no time for persistence or other cleanup. A deadline
exit is reported as an incomplete refresh shutdown, not a confirmed save.
SIGKILL, a crash, power loss, a lost provider response, filesystem failure or a
stop-budget overrun can still lose a replacement token and require sign-in
again. The service manager's own stop limit also applies. Keep the persistence
hold checks; a clear hold is an observation, not a fence against a new refresh.

### After a crash

Sessions recover from a gateway that exited: each session's token helper
starts a stopped gateway (waiting at most 10 seconds) and gives the
session its token only when the gateway is proven yours and ready. Three
starts within ten minutes pause automatic starts until you start it
yourself (`claude-multi gateway start`), so a crashing gateway does not
loop; `claude-multi gateway logs` shows why it stopped.

## The supervised service (Linux)

On Linux with a user service manager, the gateway can run as a hardened
user service instead, restarted on failure and confined:

```bash
claude-multi gateway service install     # install or refresh the unit and hand the gateway to it
claude-multi gateway service status
claude-multi gateway service uninstall   # back to the on-demand start
```

The unit is `claude-multi-gateway` (`~/.config/systemd/user/claude-multi-gateway.service`,
written and marked by claude-multi; `--name` chooses another name). The
install stops an on-demand gateway only after the persistence hold
allows it, starts the unit, proves the gateway yours and records the
backend in `endpoint.json`; a failed step brings the on-demand gateway
back. Run it again after an update: it refreshes the unit and says whether
a restart is needed. It needs a release bundle or the Nix package, not a
source checkout. There is no service on macOS.

The hardened systemd user unit needs unprivileged user namespaces.
Ubuntu 23.10 and later restrict them by default through AppArmor. With
that restriction, the unit cannot start: `claude-multi gateway service install`
reports that the gateway exited during its start and keeps the on-demand
gateway. The on-demand gateway does not need unprivileged user namespaces.

An administrator can lift the restriction with
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, or persist
`kernel.apparmor_restrict_unprivileged_userns=0` in a file under
`/etc/sysctl.d/`. This is a system-wide security trade-off, not a
requirement for using claude-multi: keeping the on-demand gateway avoids
that change.

| | On demand | Supervised service |
| --- | --- | --- |
| started by | a launch, a resume, a session's token helper, `gateway start` | the user service manager |
| restarted on failure | no (the next launch starts it) | yes |
| confinement | runs as you | a private temporary home, read-only configuration; only its credential, trace and working directories are writable |
| logs | one file per start, `claude-multi gateway logs` | the service journal, `claude-multi gateway logs` |

## Reading the logs

`claude-multi gateway logs` prints the gateway's own log lines, redacted:
no key, token, cookie or account identifier the gateway logged is shown
(`<redacted>` stands in), including a private key that spans several
lines and a value split by terminal control characters. It reads up to
200 lines above the ones it shows, so a value that begins earlier is
still recognized; a failed start's log tail is redacted the same way. The
log files themselves (one per start, or the service journal) are not
redacted: they can name account files and credential indexes, so read
them locally and never paste them into a public issue.
`claude-multi doctor` summarizes what matters from them (counts per model
and error code, refresh failures) without account details, and
`claude-multi doctor --json` is the report to attach. See
[privacy.md](../privacy.md#diagnostics-you-share).

## The persistence hold

When a signed-in account's refreshed credentials may not have been saved
(a failed or unconfirmed save, or a log not fully read), claude-multi
holds the gateway: stop, restart, an update or an uninstall that would
replace it are refused, because restarting could lose the only good copy.
Keep it running, free disk space if that was the cause, and wait for a
confirmed save; sign in again only if none comes. Once you have checked
the credentials (`claude-multi providers list`),
`claude-multi gateway clear-hold` lifts the hold (a terminal outside
Claude Code, typed confirmation). A generic restart is never the fix for a
held gateway.

## When gateway changes are paused

An installer, an update, a service install or uninstall, or a move to
another computer records an **inhibition** while it changes the gateway or
its installation. Until it ends, every other command that would start,
stop, restart, reconfigure or install the gateway refuses with one
message naming the owner and the fix: “the gateway is inhibited by …”. A
gateway already proven yours keeps serving running sessions. If the
owner was interrupted, its own command finishes it (for example
`claude-multi gateway service install` again for an interrupted hand-off,
or `sh install.sh --repair` for an interrupted install).

## Applying changes

Provider, key and model changes re-render the gateway's configuration and
verify that the running gateway reloaded it (a render sentinel): no
restart is needed. When a reload cannot be verified, the command says
`gateway did not reload — restart required`; restart between turns with
`claude-multi gateway restart`. `claude-multi providers apply` re-renders
on request; `claude-multi plan` previews what the gateway would serve
after pending changes and writes nothing.

## What the gateway changes in a request

- It adds the provider's key, or the signed-in account's credentials;
  sessions never hold either.
- It sends a key only to its provider's address: the one the release
  ships, or the one you approved. A redirect that would carry the
  credential elsewhere is not followed: that request fails with a local
  HTTP 502, `upstream redirect refused`.
- On the OpenAI API key it sends the key as a plain API client (no ChatGPT
  client version or session header), adds no hosted tool the client did
  not ask for, and replaces any error the provider returns with fixed
  local text, so an error that quotes the key never reaches a session.
- On both OpenAI routes (the API key and the ChatGPT account), Claude
  Code's per-request output cap is not passed on: the model's own output
  limit applies ([providers/api-keys.md](../providers/api-keys.md#openai)).

## Thinking from content chunks

For OpenAI-compatible upstreams such as Mistral, responses can carry arrays of
`text` and `thinking` chunks rather than one text string. The gateway decodes
these into text and unsigned Claude thinking in their original order, for
streaming and non-streaming responses. Repeated `closed: true` chunks do not
prematurely close a thinking block, and the gateway does not invent signatures.
String responses and the existing reasoning fields keep their previous behavior;
Claude and the Z.ai preset use the separate Anthropic-compatible route.

This fixes display of Mistral thinking, not faithful replay of Mistral reasoning
history. The request translator drops unsigned thinking on replay; Mistral's
signed history format is not supported by this fix. Unsupported or malformed
chunks retain each translator path's existing fallback, including omission of
unknown chunk types by the registered non-streaming converter.

## The gateway token

Sessions get the gateway's local key only from their token helper, never
from their environment or files. `claude-multi doctor --rotate-token` (or
Settings → gateway token) replaces it without dropping sessions: the
gateway accepts the old and the new key for about five minutes, then only
the new one. It asks first, verifies every reload, and resumes if
interrupted. The key never appears in output.

## Retired models stay served

When a release retires a model line, sessions started earlier still name
its selector. The gateway keeps serving each such selector on its original
model as a continuity alias (listed in `~/.config/claude-multi/continuity.json`)
until no live session uses it; `claude-multi doctor --prune-aliases`
removes the unused ones.

## Quota readings

`claude-multi quota` (and `/cm quota`, and the Providers screen's quota
column for signed-in accounts) reports what the gateway observed in recent
responses: one local read, no provider call, nothing stored. No reading
means no data yet, not zero use; a reset time is shown only when the
provider sent one. The command exits 1 when no reading was made. Quota
readings need the gateway's read-only management access, which only the
Nix package enables in this release; a release bundle or a source
checkout reports the reading as unavailable in this build, and there is
nothing to fix.

This depends on the installation channel, not on whether the gateway is
on demand or supervised. Stale or exhausted readings still exit 0;
`/cm quota` reports the same condition with status 0.

A quota reading is an observation of recent traffic, not a balance, and
the request counts claude-multi shows are not billed tokens.

## Outbound proxy and certificates

The gateway uses the outbound proxy you set in setup, never proxy
variables it inherits: `claude-multi setup --step gateway --proxy <url>`
(no credentials in the address), `--no-proxy` to remove it. Certificate
settings: [networking.md](networking.md).
