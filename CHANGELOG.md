# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-10-07

More permissive model selection, OpenRouter stealth discovery, and fixes for
proxy-based updates, token-refresh shutdown and Mistral-style thinking responses.

### Added

- OpenRouter discovery preserves every public model and offers a separately
  confirmed account-filtered listing. The CLI and TUI share pricing, context,
  tool-support and source labels. Stealth models are distinguished from routers
  and carry maker-hidden, pre-release, prompt-retention and availability cautions.
- Known `stealth/<name>` models can be added through discovery or the Providers
  screen using anonymous metadata lookup. Missing facts remain unknown;
  suffixed ids such as `:free` stay visible but are not addable in this release.
  Discovery never enables, admits, binds or chooses a default automatically.

### Changed

- Admission and qualification are optional attestations, separate from permission
  to use a route. Valid model lines on enabled, approved routes remain selectable
  without those badges; missing, failed or stale evidence is shown as a warning.
  Admission and revocation change metadata only and make no inference request.
- Any bounded, printable model-family label is accepted. An unrecognized label
  produces an "independence unknown" review label rather than a binding refusal.
  Explicit lead, agent and workflow-default bindings can override capability
  and role recommendations, with warnings.
- Provider context-window mismatches are capacity warnings that report the
  actual agent class, shared window and compaction trigger. Route approval,
  secret and consent checks, exact launch fences, native selector and effort
  constraints, disabled providers and role isolation remain enforced.
- The managed Claude Code pin is 2.1.292, with refreshed offline client evidence,
  explicit Agent-effort precedence and hook-context checks, and a synthetic
  compaction/resume test back to the previous 2.1.286 client.

### Fixed

- Release checks and downloads honour the process HTTPS proxy and bypass
  environment, with sanitized connection errors. TLS, release-host, redirect
  and signature checks remain in place. This uses the updater process's
  environment, not the gateway's outbound proxy setting.
- Gateway shutdown waits, within its existing bound, for in-flight sign-in
  token refreshes and their saves. The 30-second shutdown grace period starts
  at exit; exceeding it leaves persistence unverified and reports that fact.
- Mistral-style text and thinking content chunks are decoded in streaming and
  non-streaming responses instead of appearing as raw JSON or losing thinking.
  Existing string-content responses retain their previous bytes and behavior.
- Sessions compiled by 1.0.0 do not report integrity drift merely because newer
  review labels and model warnings render differently. Verified old diagnostic
  presentation is retained until resume, with doctor Attention rather than a
  false integrity block.
- Narrow-screen model pickers keep warning remedies and provider approval
  commands visible. TUI Direct mode now offers a default-No "Launch anyway?"
  confirmation for a missing credential, matching the CLI's advisory behavior.
- Installation documentation states minimal Linux prerequisites, version-matched
  checksum and signature downloads, and the terminal requirement for setup and
  uninstall.

### Compatibility and limits

- The supervised Linux gateway still needs unprivileged user namespaces,
  restricted by AppArmor on Ubuntu 23.10 and later; the on-demand gateway
  does not. See [service requirements](docs/guides/gateway.md#the-supervised-service-linux).
- Catalog version is 38; record and state format remain 4. Existing qualification
  records retain their original evidence and may become stale under the new pin;
  no automatic requalification or provider call is made.
- Rolling back the installation does not translate new model declarations or
  bindings into older policy. Family labels or unattested bindings newly allowed
  by this release may be rejected by 1.0.0; retain a compatible profile if you
  need to resume work there.
- Mistral thinking is unsigned. Faithful replay of Mistral reasoning history is
  not included; the existing [translation limits](docs/providers/openai-compatible.md#translation-limits)
  still apply. Provider behavior is tested with fixtures, not live accounts.
- Whether OpenRouter's account-filtered listing includes a particular stealth
  model depends on what the service returns. Add-by-id remains available for
  known supported ids; missing listing facts are not invented.
- A 1.0.0 installation behind a mandatory proxy must reach this release once
  through the installer or an offline update, because its old updater does not
  yet contain the proxy fix. See [Updating](docs/update.md).
- Platform-support claims are unchanged; see [Compatibility](docs/reference/compatibility.md#platforms).

## [1.0.0] — 2026-10-05

The first release.

### Compatibility and limits

- The supervised Linux gateway needs unprivileged user namespaces,
  restricted by AppArmor on Ubuntu 23.10 and later; the on-demand gateway
  does not. See [service requirements](docs/guides/gateway.md#the-supervised-service-linux).
- Platform support requires a native install journey; the channel and
  platform labels are in [Compatibility](docs/reference/compatibility.md#platforms).
- Keyed OpenAI-compatible support is open. Its proof uses synthetic
  endpoints, not vendor accounts; the [translation limits](docs/providers/openai-compatible.md#translation-limits)
  still apply, and presets are not vendor-tested.
- Quota observations are available only in the Nix channel, not in release
  bundles or source checkouts. They are not provider balances or billed
  token totals.
- Managed sessions run the hash-verified pinned Claude Code. Adoption by a
  newer client's background supervisor and resuming a newer client's
  transcript are not verified compatibility guarantees; see
  [client skew](docs/reference/compatibility.md#client-skew).
- State format is 4. Updates never downgrade state, and rollback refuses
  when the previous release cannot read the state on disk. Nix packages
  update through Nix; release bundles use `claude-multi update`. See
  [Updating](docs/update.md).

Release-bundle platform status:

- Linux x86_64: supported; native release-bundle journeys and the Linux
  release battery.
- Linux aarch64: supported; native release-bundle journey on Ubuntu 24.04
  arm64.
- Apple silicon: supported; native release-bundle journey on macOS 15;
  on-demand gateway only, with no launchd service.
- Intel macOS: built and reproduced, no native journey; no support claim.
- Windows through WSL2: supported inside WSL2; Windows Server 2025 with
  Ubuntu 24.04. Native Windows is not supported.

The native journeys exercise release bundles, not Nix installations.

### Command-line names

Use these public spellings; the alternative spellings remain accepted
aliases, not separate workflows:

| Alias | Public spelling |
| --- | --- |
| `--composition`, `--composition-file` (aliases) | `--profile`, `--profile-file`, respectively |
| `compose` | `profile` |
| `compose delete` | `profile rm` |
| `compose restore-default` | `profile reseed balanced` |
| root `show` | `profile show` |
| `sessions link --composition` / `--model` (aliases) | `sessions link --profile` / `--direct` |
| `sessions transition` | `lineup --session <runtime-id> --relaunch profile <name>` |

The [command reference](docs/reference/cli.md) gives the arguments for
each public command.

### Added

- Keyed OpenAI-compatible endpoints and the Cerebras, Gemini, Groq, Mistral
  and xAI presets, after the complete source-bound compatibility proof.
  Messages-to-chat translation is HTTPS and bearer-only; it still requires
  route approval, a key, model admission and agent qualification. Presets are
  documented configurations, not vendor-tested guarantees. Endpoint forms
  offer only supported authentication, and generic OpenAI Platform host
  refusals point to its separate supported API-key route.
- Launch Claude Code with a lineup — a lead model plus up to nine `cm-*`
  agents on other models — from a saved profile, an unsaved profile file or
  a single direct lead (`claude-multi`, `claude-multi direct`).
- A per-session compiled scope: agent files, the lead set `/model` offers,
  the `/cm` skill for live lineup changes and lifecycle hooks; sessions are
  recorded and resumable, and native forks resolve in one step.
- claude-multi's own hash-verified copy of the pinned Claude Code version,
  copied from an existing installation or downloaded with consent
  (`claude-multi setup --step claude`).
- A local gateway (CLIProxyAPI with local patches, loopback only), started
  on demand or installed as a supervised user service
  (`claude-multi gateway …`, `claude-multi gateway service install`).
- Profiles, seeds and named bindings; providers, models and qualification
  for models you declare; a terminal UI for every task, with a line
  mode without curses.
- `claude-multi doctor` with a remedy for every finding.
- Agents run at full context: one compaction window, the smaller of the
  window ceiling (800K) and the lead set's smallest provider bound, applies
  to the lead and to every agent on a 1M-class line whose provider bound
  is at least that window. An agent line whose bound is below it runs in
  the 200K class, so it never outgrows its provider. A session recorded
  with agents in another class keeps them until its next resume, which
  shows each move ("agent class 200K → 1M"); doctor reports pending moves.
- One window ceiling for the lead and every agent, 200K to 800K (default
  800K): `claude-multi window-ceiling 400K` sets it,
  `claude-multi window-ceiling --reset` restores the default, and the
  Settings row edits it; it applies at the next launch or resume. The card, the lineup
  summary (`/cm`, `profile show`) and `--print-launch` show each role's
  effective window.
- Setting up from the terminal: `claude-multi setup` runs the steps that
  are not done yet, `claude-multi setup --answers FILE` applies a prepared
  setup after showing its whole plan for one confirmation (a starter
  profile with its slots and files, the default profile; a file whose
  entries depend on each other is refused with the fix), and
  `claude-multi doctor --first-run [--json]` checks a new installation
  without changing anything.
- Connecting providers from the terminal or the UI: API keys
  (`claude-multi providers set-key|remove-key`), Claude and ChatGPT account
  sign-ins in the terminal after a personal-use confirmation
  (`claude-multi providers sign-in|sign-out`), a consented connection test
  (`claude-multi providers test`) and a key file you choose
  (`claude-multi setup --keys-file`).
- OpenAI's models over an OpenAI API key
  (`claude-multi providers transport openai api-key`), through the
  gateway's Responses route: only the models reviewed for the OpenAI API
  are served on it, each with its documented context window and efforts;
  never through a saved ChatGPT account and never as an empty model list.
  The gateway sends the key as a plain API client (no ChatGPT client
  version or session header) and replaces any provider error with fixed
  local text, so an error that quotes the key never reaches a client.
  GPT-6.1 Sol, GPT-6 Astra and GPT-6 Luna are reviewed for it. In
  Providers, Enter on OpenAI chooses between the ChatGPT account and the
  API key, as it does for Anthropic, and K replaces the key in use. While
  the key is selected the gateway adds no hosted image-generation tool the
  client did not ask for, and a model's details in Models show the key
  route's own evidence (its documented bounds and the conservative
  validated floor), never the account route's measurement. Each request
  is one Responses request with no retry when it succeeds; the client's
  output cap is not passed on, so the model's own output limit applies.
- Presets in the provider picker for vendors with a documented
  Anthropic-compatible endpoint (Z.ai, Moonshot AI's Kimi platform,
  Alibaba Cloud Model Studio, Novita AI, Vercel AI Gateway), with an
  OpenAI-compatible endpoint (Groq, Mistral AI, xAI, Cerebras, Google
  Gemini) and for servers on
  your computer (Ollama, LM Studio, vLLM, llama.cpp). A preset fills in the
  address, the key's name and the model list from the vendor's
  documentation and is labelled a preset: nothing is tested until you add
  and admit a model (`claude-multi providers add --preset NAME`, or Enter
  on it in Get started: you name it, approve where its key goes and type
  the key). One more provider from a preset whose key another provider
  already uses gets a key of its own under its own name; sharing the saved
  key (`--reuse-key`) or replacing it for every provider that uses it
  (`--replace-key`, after a confirmation that names them) is your explicit
  choice. Providers N lists the same presets as Get started, and the preset
  form takes another address the vendor documents, such as a workspace
  endpoint (the preset's own by default; checked like
  `providers add --preset NAME --base-url URL`).
- Replacing a saved API key that other providers share
  (`claude-multi providers set-key`, or K on Providers) names every
  provider using it and asks first (default No, Cancel on the screen);
  `--yes` replaces it without the question. A provider of a `custom.json`
  that is not migrated yet counts like any other (none once it is
  migrated, nor one claude-multi ignores), here and when a preset's key
  name is already in use. The key is saved only while the provider it was
  planned for still exists and still uses that key; a provider removed or
  changed meanwhile refuses the change with nothing written.
- For an Anthropic or OpenAI API-key transport that is chosen and approved
  but whose key is not saved, doctor names
  `claude-multi providers set-key <provider>` and the screens name G → K,
  instead of a switch to the transport already in use; the launch card's H
  report names G (providers) → K on the provider (a resume card its way
  back first).
- Models for the shipped Kimi and Qwen providers, read from their vendors'
  documentation: Kimi for Coding on the Kimi Code plan, and Qwen3.8 Max and
  Qwen3.8 Flash on the Model Studio Token Plan, so either key alone starts
  a profile.
- Account sign-ins are described as data (`account-pools.json`): each
  account's provider, sign-in method (a browser page or a device code),
  personal-use text, sign-in record names and registry sections. The
  Claude and ChatGPT sign-ins behave as before and earlier confirmations
  stay valid. Sign-in records are told apart by the gateway's full file
  name prefixes, so one service's records never count as another's
  (`kimi-ai-…` is never a `kimi` record), and a confirmation a later
  release stores for an account it adds is kept and ignored.
- A provider whose key you save before it has any model continues into
  adding one (a listing you consent to, or by hand) and admitting it, so a
  profile can use it; the command line and the profile step name the same
  steps. A server on your network is lead-only.
- Providers E edits a provider you added in a form filled with what it
  has now, through the same checks, preview and confirmation as
  `claude-multi providers edit`. Saved Claude and ChatGPT accounts stay
  listed while their provider uses its API key.
- `claude-multi setup --answers` plans a starter profile against the
  transport the same file selects.
- The launch card keeps its notices readable at 80×24: an ignored
  `CLAUDE_CONFIG_DIR`, new model lines (M → Enter admits) and implementers
  outside a Git repository (git init) each keep a row in a shorter form, and
  a long notice loses its middle, never its remedy. Slots missing the same
  sign-in share one row; a sign-in remedy names G → L on the card (a resume
  card names its way back first: Esc once for each screen above the launch
  card, or `run claude-multi` again when Esc ends it), while the command
  line keeps the sign-in commands. V shows the terms and the `/cm` guidance the help
  shows.
- The lineup dialog offers a fallback to every provider that has a
  fallback profile, not only the providers the session already uses, with
  `/cm fallback`'s own preview.
- The binding picker and the selected profile-editor slot describe the
  slot's role and grade; V shows the whole description.
- After a forget, the Sessions screen shows the result in a view that keeps
  the full runtime id and both ways to manage the conversation again.
- A resume gate whose transcript sits under a directory that cannot be
  told apart, or no longer exists, offers the directory chooser; moving the
  transcript stays a command you run.
- A colour-query reply that arrives in pieces, before and after the card is
  drawn and with pauses between them, no longer quits the card and none of
  its bytes reaches it as a key. Esc still cancels the card while such a
  reply is unfinished, also when Enter follows it at once.
- The new-provider form never draws a key typed or pasted into the
  secret-name field, not even in part, and refuses it at the field, before
  any preview. A candidate declared from the
  pinned registry keeps the registry as its context source.
- `claude-multi sessions resolve-fork` asks before it discards a fork's
  marker (y/N, default No); without a terminal it needs `--yes`.
- `/cm show` lists the last day's failed requests per provider the session
  uses, with how far the gateway's log was read and the remedy
  (`/cm fallback <provider>` with `/cm profiles`, `/cm set <agent>=<model>`; one size however
  many profiles there are); when the log cannot be read it says so instead of counting zero.
- A storage or permission failure of `claude-multi lineup` (no space, no
  permission, a read-only file system) names the fix like every other
  command; `/cm` keeps answering with status 0.
- `claude-multi doctor --rotate-token` asks its questions on the terminal
  and writes its result to stdout and a pause to stderr, so both can be
  redirected.
- Ctrl-C in `claude-multi-proxy` prints one cancellation line and exits
  130.
- doctor shows every `SSL_CERT_FILE`/`SSL_CERT_DIR` value home-relative, a
  relative one included.
- `claude-multi gateway logs`, the gateway log view and the log tail a
  failed start shows print every line redacted: keys and tokens,
  `Authorization`, API-key and management-key headers, cookies,
  secret-named fields, credentials in addresses and account identifiers
  (`auth=` sign-in record names, JSON account fields, e-mail addresses)
  read `<redacted>`. Terminal escape sequences are removed before anything
  is matched; a quoted value, a list or an object is redacted whole, and so
  is a value that runs over several lines, such as a private key, also when
  the lines shown start inside it, it follows a field name
  (`private_key: …`) or the log ends before its last line. A carriage
  return, a vertical tab or a Unicode line separator inside a journal
  message never splits a header from its value.
- The supervised gateway service trusts the certificates the launcher
  trusts: `claude-multi gateway service install` (and its refresh) writes
  `SSL_CERT_FILE`/`SSL_CERT_DIR` into the unit, bound read-only where the
  service would not see them, and refuses, changing nothing, when one is
  not a readable file or folder, its path cannot be written into a unit
  (a `:` included, in your home folder too), or a certificate folder is
  your home folder, a folder above it or a private system folder (or links
  to one). A unit missing a certificate bind it needs reads as stale.
- The gateway dialog and the transition screen's note on a session still
  running wrap, so their remedies are never cut off.
- The key file you select (`claude-multi setup --keys-file`) may sit in any
  private folder of your own (no group or other access), besides
  `~/.config/claude-multi`; managed sessions are denied that file by name.
  No other folder is assumed to hold keys.
- `SHA256SUMS` lists `install.sh` and `install.ps1` besides the manifest and
  the bundles, so the release signature covers the installers;
  `install.sh` carries the bundles' checksums only. `tools/release.py`
  `build`, `verify-draft` and `publish` refuse a release whose
  `SHA256SUMS` leaves either installer out.
- `claude-multi window-ceiling --reset` names the ceiling it replaced and
  the command that sets it again.
- The preset key chooser shows short choices, with the full name of a new
  key in the text above them, readable at 80×24.
- The help names `USAGE.md` as the documentation map; doctor's
  managed-policy remedy reads "ask the policy owner, or use plain claude".
- A retained OpenRouter model alias reports its model's owner, as the
  current ones do.
- The Nix package installs `claude-multi` and `claude-multi-proxy` only;
  `claude-multi-dev` stays a source checkout's command.
- Kimi or Qwen alone reaches a usable profile: adding a model the listing
  states no effort for, or one entered by hand, no longer fails on a
  provider without a high-effort contract. The command line's next steps
  name the whole path (list, add, admit, then `claude-multi setup --step
  profile`).
- `claude-multi quota`, `/cm quota` and the Providers details name the
  quota's condition (available, stale, exhausted, management-disabled,
  unavailable, not-ours); the command exits 1 exactly when no reading was
  made.
- `claude-multi uninstall` (also `install.sh --uninstall`): the plan first,
  credentials only after a typed phrase; it refuses while a session may
  still run, another command writes session state or a store it removes,
  a sign-in save is unconfirmed, a transaction holds the gateway
  inhibition or the gateway service cannot be removed safely, never
  follows a link out of its folders, removes the installer's launchers and
  PATH line only as its receipt proves them, and keeps every key file you
  did not agree to delete, the previous release and a launcher changed
  meanwhile.
- One list of credential locations: managed sessions cannot read any of
  them, `claude-multi export --out` never writes into one, and uninstall
  deletes what lies there only after the typed phrase.
- One inhibition per state root for transactions that change the gateway
  or its installation (the service hand-off, installers, updates, machine
  moves): while one is recorded, every other start, stop, restart, service
  change, configuration change, install and update refuses with one message
  naming the owner and the remedy (exit 1); a gateway already proven yours
  keeps serving and doctor reports an interrupted one. Shell owners use
  `python3 -m claude_multi.gateway_inhibition`. A change of a home's
  gateway configuration or keys also waits for the transaction of the
  state root that manages that home, refuses when that root changed while
  it waited, and refuses while `continuity.json`, which names it, cannot be
  read.
- A new home records its own gateway port before anything renders, reloads
  or starts a gateway there; until then those commands say the gateway is
  not set up and name `claude-multi setup --step gateway`.
- A Python package with three console entry points (`claude-multi`,
  `claude-multi-proxy`, `claude-multi-dev`) and a Nix
  flake.
- Release bundles for Linux and macOS on x86-64 and arm64, each with its
  own Python runtime and gateway, built reproducibly by
  `tools/build.py release`, with licences (the bundled runtime's library
  licences and notices included), SBOMs and checksums; releases
  are checked, signed and published with `tools/release.py`.
- An installer for Linux and macOS (`install.sh`; `install.ps1` sets up
  WSL 2 on Windows and runs it there). It verifies the release signature
  (a failed signature always stops it), installs into
  `~/.local/share/claude-multi/install` keeping the previous version,
  never touches another installation's state without `--migrate-from-nix`,
  and records the launchers and PATH line it wrote in `installer.json`.
  It pauses gateway changes while it works; an install interrupted after
  it started changing the installation keeps them paused until
  `sh install.sh --repair` finishes it (given the release again when a
  first install stopped before any version was in place).
- `claude-multi update`: checks for a newer signed release, shows what it
  changes (size, Claude Code, state format, gateway restart), asks, and
  installs it together with the Claude Code version it needs; `--check`
  only checks, `--rollback` returns to the previous version, `--from-dir`
  uses downloaded release files. The card's U runs the same steps; doctor
  and the card show how old the installed release is. A running gateway
  keeps the version it was started from until it restarts. An interrupted
  update or rollback is finished by the next `claude-multi update`; a
  switch stopped between the `current` and `previous` links is put back as
  it was. A rollback goes only to the version it showed.
- The installer and `update` remove old versions and the Claude Code
  copies no installed release needs; a copy a running session uses is
  kept and reported.
- `claude-multi --version` names the catalog, gateway and Claude Code
  versions it ships.
- State is only ever migrated forward: a rollback refuses once a newer
  version has upgraded the state, and an older version refuses to change
  newer state and names the fix.
