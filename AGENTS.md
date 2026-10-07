# AGENTS.md — claude-multi development guide

This is the guide for anyone changing claude-multi, human or agent: the
architecture, the invariants that keep the system correct, the development
workflow and the rules of engagement. Read it fully before changing code.
Setting up a checkout, running the tests, building the gateway and the
commit conventions are in [`CONTRIBUTING.md`](CONTRIBUTING.md); user
documentation is in `docs/`.

## 1. What this is

claude-multi is a **stdlib-only Python launcher** that compiles a session's
**lineup** (a lead model plus up to nine `cm-*` agents, evaluated from a
profile or a single direct lead) into a **per-session durable scope** and
execs a hash-verified Claude Code binary pointed at it. Users start from
`docs/USAGE.md` and the symptom index `docs/CHEATSHEET.md`. Layout: the
package in `src/claude_multi/` with its runtime resources in
`src/claude_multi/data/` (`version.json`, `settings.json`, `catalog/`,
`schemas/`, `presets/`, `examples/`, `gateway-unit.json`, `account-pools.json`, the generated and
checked `gateway-contract.json` and `registry/`, and `release-trust/`, the
release signing keys); its packaging in
`pyproject.toml` (three console entry points through
`claude_multi.entrypoints`) and `MANIFEST.in`; the source launchers in
`bin/`; `tests/` at the repository root; Nix in `nix/`; the gateway recipe
and patches in `gateway/`; the installers in `packaging/`; user docs in
`docs/`. Resource paths in this guide (`catalog/…`, `schemas/…`,
`version.json`, …) are relative to `src/claude_multi/data/`; edit them
there in a checkout. Four authorities locate files (`claude_multi.layout`):
the packaged resources (`claude_multi.resources_root()`, the only source of
the running release's identity), the selected resources (`assets.root()`:
an explicit root, then `CLAUDE_MULTI_ASSETS`, then the packaged ones), the
verified source checkout (the only place developer commands write) and the
installation (a source tree, or the prefix of an installed package: its
`bin/` entry points and its documents in `share/claude-multi`). State and config:

```
~/.local/state/claude-multi/
├── bin/claude-multi-hook        # stable lifecycle-hook shim (never a store path)
├── bin/claude-multi-hook-3      # protocol-3 hook shim for v2 scopes
├── state-version                # state marker: missing = 3 (legacy records), 4 = v4 records
├── migration.lock               # global migration lock: exclusive for migrate /
│                                # restore-2x, shared for every launcher write, probed by hooks
├── sessions/<managed-id>.json   # records (the intent authority): v4 (v1-3 still read)
├── sessions/<managed-id>.v3.json  # byte-exact legacy backups written by `migrate` (restore-2x)
├── scopes/<managed-id>/
│   ├── .claude/agents/cm-*.md   # generated agent definitions
│   ├── settings.json            # compiled session settings (hooks, fence, policy)
│   └── lineup.md, lineup.gen, lead-set.json, .claude/skills/cm/SKILL.md
│                                # every v2 scope
├── notice/<runtime-id>.seen     # lineup generation a runtime session last saw
├── lineup-log/<managed-id>.log  # spawn/apply log, 1 MiB + one rotation
├── hook-errors.log              # protocol-3 hook failures, metadata only
├── last-session-by-cwd/<hash>.json  # `-c` pointers (one per cwd; the legacy
│                                # .ordinary file is read, never written; `.v3` backups)
├── locks/                       # runtime-index + per-session lifecycle FileLocks
├── drafts/                      # dev pipeline drafts
├── gateway/                     # the gateway's working directory (0700, with logs/):
│                                # a .env here stops `claude-multi-proxy run` and the logins;
│                                # gateway.lock (held by the running gateway across exec),
│                                # gateway-start.lock, exec.json (instance nonce),
│                                # start-history.json, persistence-hold.json,
│                                # inhibition.json (the root's gateway inhibition: only while a
│                                # transaction owner — service hand-off, installer, updater,
│                                # machine move — changes the gateway, or after one died midway;
│                                # an earlier service-handoff.json refuses the same way until
│                                # migrated into it), logs/gateway-<UTC>-<nonce>.log (one per
│                                # on-demand start)
├── gateway-inhibition.lock      # the inhibition's writer fence (shared by non-dispatching
│                                # writers from check to write, exclusive while a begin records)
├── channel                      # the installation that owns this state root (bundle | nix |
│                                # source; one line, 0600; claimed under channel.lock, so one
│                                # first run wins); another channel's launcher refuses
└── lead-prompt-<digest>-<managed-id>.md        # compiled cm-lead prompt
~/.config/claude-multi/          # profiles, bindings, gateway config.yaml (secrets), api-key,
                                 # native-contract.json (an operator override from an earlier
                                 # release: classified, never applied),
                                 # custom.json, settings.json (operator settings),
                                 # continuity.json (gateway continuity aliases; HOME-relative),
                                 # profiles/<name>.json + bindings.json (profiles and
                                 # named bindings; written by the profile and session commands),
                                 # management-key* (active, .next, .disabled, .prepared, .lock; names only),
                                 # compositions/ (legacy input of `profile migrate`),
                                 # endpoint.json (gateway port, backend, unit, outbound proxy),
                                 # choices.json (closed; choices older releases must never meet:
                                 # default_profile, session_env_keep — settings.json and
                                 # preferences.json are read by older releases under closed schemas)
~/.local/share/claude-multi/install/{versions/<v>,current,previous}  # bundle installs (installer-owned;
                                 # installer.json: the launchers and PATH lines it wrote, with digests)
~/.local/share/claude-multi/nix/current  # the Nix package the supervised service runs (an indirect
                                 # GC root; written only by `gateway service install`)
~/.config/systemd/user/<name>.service    # the supervised unit (optional; header-marked, product-written;
                                 # claude-multi-gateway.service by default)
```

Release: the version in `version.json` (1.1.0; catalog 38).
Current default: the **`balanced` seed profile**
(`catalog/profiles/balanced.json`): Opus 5.5 lead at ultracode, Sol high
explorer/xhigh analyst, Opus analyst-strong, Luna max / Sol xhigh / Sol max
implementer grades, Sol max reviewer and Opus reviewer-strong.
The trusted catalog pins Claude
**2.1.292** as the verified binary. The gateway is **CLIProxyAPI 7.3.15**
(pinned in `gateway/UPSTREAM.json` and built by `tools/build.py` with the official
Go toolchain, `CGO_ENABLED=0`; see "Gateway build" in §4) with twenty-one local patches (numbered 1–10 and 12–22; 11 is deferred) —
loopback OAuth bind, Kimi/Claude compat, the non-Claude cache-retention
boundary (§2 invariant 11), the read-only management allowlist (an
engine-level guard serves
only `GET /v0/management/auth-files`, `…/auth-files/models` and
`…/model-definitions/:channel` and refuses every other management-scoped
request with a bare 404 before any key check; the env password does not
imply allow-remote and the RESP surface is closed; inert until a
start-selected management key is injected), and
the config watcher (parent-directory watch, so a rename-replaced
`config.yaml` hot-reloads; blank or key-dropping configs never load), and the
start-up gate (after a (re)start, model routes wait — at most
30 s after start, then one warning and they serve — until a FIFO barrier
behind the watcher's initial auth updates confirms the file (OAuth) auths are
registered, so a request racing the start no longer gets HTTP 400 "unknown
provider"; `Service.Run`'s order is unchanged; health, management and static
routes never wait), followed by the env-only management patch
(`remote-management.secret-key` and the local password are inert at startup and
reload; only the environment secret enables/authenticates management). The
series then adds credential-save receipts, credentialed redirect refusal, keyed
OpenAI-compat safety, auth snapshot ownership, server config snapshots, plugin-host
locking, Claude metadata locking, the OAuth model overlay and suppression of
the Antigravity version updater under `--local-model`, in that order; #18 is
the Codex 0.159.1 client identity (one bounded exception: the pinned
registry's static `override_header` entries sit on `gpt-5.6-luna`, which no
catalog or retired line routes); #19 binds the
`--antigravity-login` callback listener to 127.0.0.1 instead of every
interface (`sdk/auth/antigravity.go`; Go gate `antigravity-loopback-callback`,
built-gateway row `LoopbackOAuthListenerTests` LO4); #20 is codex
API-key safety (`internal/runtime/executor/codex_key_safety.go`, reusing
`internal/compatsafe`): for a codex API-key auth every failure of the HTTP
Execute, ExecuteStream and compact paths (a non-2xx body read through the
64 KiB + 1 byte bound, a terminal `error`/`response.failed` event, the
bootstrap overload rejection, a transport, read or scanner error) keeps the
codex classification's status, retry delay and credential scope with the
fixed text of one `compatsafe` kind, and an API key with codex cloaking off
gets no `Version` default and no session header (cloaked keys and ChatGPT
accounts keep the codex identity; images, websockets and `HttpRequest` are
outside it; Go gate `codex-api-key-safety`, built-gateway row
`CodexApiKeySafetyTests` CK1, declared prerequisites credentialed-redirects,
keyed-safety and codex-client-identity). #21 joins refresh workers at
shutdown: `Service.Shutdown` waits, within the existing bounded shutdown,
for every refresh run started before the stop through its durable save and
receipt report, and `Run` starts its 30-second shutdown budget at exit (HTTP
graceful draining is unchanged; a deadline exit leaves persistence
unverified; omission row RS1). The last patch, #22, decodes OpenAI-compatible
content-chunk arrays (Mistral-style `text` and `thinking` chunks) on all
three response paths into text and unsigned thinking blocks, ignores
`closed:true` as a block terminator, and keeps string, null and absent
content byte-identical; unsupported chunks keep each path's earlier
behavior (omission rows MC1/MC2). Signed Mistral history replay stays
unsupported. Refused
credentialed redirects yield fixed local 502 `upstream redirect refused`, with
no upstream body/Location or path-bearing refusal log. Keyed-compat safety is
route-scoped to Claude `/v1/messages` → chat; compact, images and SDK
`HttpRequest` are outside that promise. Retry-After patch 11 is deferred: it
stays a candidate (`gateway/candidates/`), never shipped. The embedded
registry carries every catalog wire except the overlay-only generations
(the OAuth how-to in §5); the pinned registry directory
(`${cliProxyApi.src}/internal/registry/models`,
pre-patch) is a checked-in resource, `registry/` (generated by
`python3 tools/build.py gateway registry` after `gateway fetch`; the flake
passes the pinned source's directory to `nix/package.nix` as
`cliProxyApiRegistry`, and the package build refuses a snapshot that
differs from it; the package also links the gateway it ships as
`libexec/claude-multi/cli-proxy-api`). `run`/login exec the gateway with a scrubbed environment and the
rendered config pins `discovery: {enabled: false}`. Gateway auth
is **helper-only**: no launch puts a credential in the process
env; the compiled `apiKeyHelper` is the single source (§2 invariant 3).

Launch argv (one launch path for every session): `claude
--session-id|--resume <runtime-id> --name cm:<profile>[@project]|cm:direct:<lead>
--settings <scope>/settings.json --model <lead selector> --effort <session effort>
--add-dir <scope> --append-system-prompt-file <lead prompt>` + passthrough
(`compiler.compile_lineup_launch`).
`--agents`/`--disallowedTools` are never used: argv definitions vanish on
supervisor takeover (observed live); on-disk files are re-discovered every
process start.

Profile lineups and direct lineups, one compile path:

- **Profile lineup** (`claude-multi`, `--profile`, the card): the profile's
  lead and bound `cm-*` agents (roles v2), the split fence (lead set,
  agent set, fallback-only set), compiled policy denies, the lineup notice
  and `/cm`; the session follows its profile or is pinned.
- **Direct lineup** (`claude-multi direct`, `/cm direct`):
  an ad-hoc lineup with a lead and no `cm-*` agents, compiled by the same
  `compile_lineup_scope`/`compile_lineup_launch`; `/model` offers the lead
  set. Lineup changes are LIVE (agents only) or RELAUNCH (recorded as
  `pending`, applied by the next resume), never an in-place lead swap
  outside the lead set. Upstream bare `claude` is never touched.

## 2. The doctrine (every change must preserve these)

1. **Record = intent; catalog = trusted source; scope = pure function of
   (record, installed catalog).** Scopes are always re-derivable; repair
   recompiles from record + catalog and displays drift. Never treat scope
   content as authority. The native contract (v2, `catalog/native-contract.json`)
   is path-free: its `verified` list pins one Claude Code version with the
   per-platform `{sha256, size}` from Anthropic's signed manifest, and the
   packaged contract is the only one in effect. An operator override file
   (`~/.config/claude-multi/native-contract.json`, written by earlier
   releases) is never applied: an old-format one is classified against the
   pin (`contract_source` `override-ignored-stale`/`-newer`/`-invalid`) and
   doctor names it for removal (Attention). An **older-format** override (the
   `effort_vocabulary` shape, `catalog.is_legacy_contract_override`) is
   removed at Runtime init — a one-shot stderr notice plus a doctor
   Attention line — unless the lock is held, the file is a symlink/unsafe,
   the command is `session-event`, or a newer state marker is present (then
   the degrade path reports it). Launches execute only claude-multi's own
   copy of the pin (`pin.owned_path`: `<data root>/claude/<version>/claude`),
   size- and hash-checked on every launch before any state write.
2. **Two UUIDs, not one.** `managed_id` keys claude-multi state (record file,
   scope, pointer, locks). `runtime_session_id` is the authoritative native
   `--resume` target, reconciled from SessionStart hook metadata
   (startup/resume/clear/compact); old IDs become bounded aliases (≤16).
   Monotonic `launch_epoch` rejects delayed hooks from older launches; a
   higher epoch always wins. `transcript_path` is ignored, never stored,
   transcripts never read. **Forks:** a `fork`-sourced hook only appends to
   `pending_forks` (never retargets authority); when a later hook retargets
   authority ONTO a pending fork id, the marker self-clears (reality
   resolved it — the 2026-07-27 self-block incident). Genuine pending forks
   block resume/transition until the operator adopts
   (`sessions link`, which strips the parent's marker) or discards
   (`sessions resolve-fork`; metadata-only, transcript kept);
   **both decisions bump the parent's `launch_epoch`, revoking the fork's
   baked hook credential** so its later hooks cannot migrate the parent's
   authority into the fork's lineage (trade-off: a still-running
   parent app reconciles on its next launcher resume — the relink-runtime
   precedent). The cap never evicts silently: the 17th distinct fork hook
   fails visibly. Resume flows converge resolved-by-reality markers at
   action paths (plan/resume guards); display paths keep them visible.
   `doctor --repair-all` converges the stale-authority case; every
   fork-blocked message names the fork UUID + exact commands
   (`sessions.pending_fork_message`, single source, ordinary-aware).
3. **Never embed volatile paths in scope content.** Package/store paths
   change on every rebuild → mass scope mismatch (the 2026-07-24 incident).
   Hooks invoke `<state>/bin/claude-multi-hook`, refreshed by every launcher
   run. The shim prefers the resolved launcher, falls back to PATH.
   `scope.resolve_hook_command` is the single authority for the real command.
   v2 scopes use `<state>/bin/claude-multi-hook-3`, refreshed
   next to it from the same resolved command; every v2 hook command ends in
   `--hook-protocol 3`, so an older (v1) scope never gets protocol-3 hook
   behaviour.
   Same pattern for gateway auth: `<state>/bin/claude-multi-gateway-token`
   (`scope.ensure_token_helper_command`) backs the compiled `apiKeyHelper`,
   so sessions relaunched by the background daemon — which scrubs
   `ANTHROPIC_*` from its children's env — keep gateway routing. The shim
   runs `<launcher> gateway ensure --quiet --max-wait 10` first (with
   `--base-url "$ANTHROPIC_BASE_URL"`, the calling session's compiled
   destination, when the client's environment carries it: a session compiled
   for another endpoint than the configured one is refused) and prints
   the token only on exit 0 (ours and ready; a stopped gateway is started,
   bounded); every other outcome exits nonzero with empty stdout. The
   compiled value is shell-quoted (`scope.token_helper_setting`). The
   helper is the **only** credential path: every launch plan
   unsets the six credential keys (`catalog.CREDENTIAL_ENV_KEYS`:
   `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`,
   the two `*_FILE_DESCRIPTOR` keys, `CLAUDE_CODE_USE_GATEWAY`), launch
   injects no token, and `compile_lineup_launch` requires
   `token_helper_command` and asserts the scope carries `apiKeyHelper` (on
   the pinned client an env token outranks the helper, so an inherited one
   would silently defeat token rotation). The token value never enters
   scope files; only the non-secret `env.ANTHROPIC_BASE_URL` does. The token path is **strictly
   HOME-relative** (`~/.config/claude-multi/api-key`), matching its writer
   (`proxy.config_dir`) and reader (launch via catalog-pinned
   `gateway.token_file`) — it deliberately ignores `XDG_CONFIG_HOME`, which
   moves profiles, bindings, settings and the override (`sessions.config_root`) but not the
   catalog-pinned token location.
4. **Fail closed, atomically.** All state writes: same-dir temp + fsync +
   rename, mode 0600 in 0700 dirs, symlink-refusing (`state.py`).
   `CommittedStateError` marks "bytes replaced, dir durability unconfirmed" so
   callers run transaction-specific recovery. Scope writes stage to
   `scopes/.<id>.new/` then rename.
5. **CAS-by-own-write + mutation tokens.** Rollback only touches record/scope
   while the on-disk record still equals what that attempt committed
   (`mutation_token`, `committed_bytes`). Rollback is **mutation-aware**: a
   failure before this attempt committed anything touches nothing (an
   unconditional rollback would delete state another writer committed).
6. **Lock ordering: runtime-index → lifecycle(per-session) → pointer.** Never
   invert. Pointer critical sections are single atomic writes; blocking is
   safe there. Lifecycle fds are `O_CLOEXEC` and survive into execve.
7. **Compiled settings are a closed allowlist** (`COMPILED_SETTINGS_KEYS`):
   workflow keys, `permissions.deny`, `availableModels`, `model`, `env`,
   `hooks`, `worktree`, `autoCompactEnabled`, `apiKeyHelper`, `modelPicker`.
   Nothing else without a demonstrated failure case (`apiKeyHelper`
   earned its place via the daemon env-scrub incident; `modelPicker`
   (v2 scopes only) because without it `/model` does not list the lead
   set). The feedback-drafts preference (§3.2) travels in `env`, not as a
   new key. `permissions.defaultMode` (inside the allowed `permissions`
   key; the demonstrated failure: 2.1.284+ starts an unconfigured session in
   auto mode, which through the gateway refuses every Agent/Workflow launch
   with no classifier request) is `scope.PERMISSION_DEFAULT_MODE`
   ("default"), compiled only when `managed.permission_mode_configured`
   finds no `permissions.defaultMode` (each layer parsed like the client:
   a duplicate key's last value wins) in `$HOME/.claude/settings.json`
   (never `CLAUDE_CONFIG_DIR`), the cwd's project/local settings or policy
   files; when any user layer has top-level keys outside the pin's recorded
   `settings_keys`, only a managed policy mode counts and otherwise the
   default is compiled, whatever another user layer configures (the pin may
   skip that file; the launch notices and doctor name the keys and the mode
   that applies, `Runtime.settings_skew`); decided per
   launch/resume (`Runtime.permission_default_mode`),
   carried by `transition.expected_plan` from the live scope, and a
   defaultMode-only delta is doctor Attention (`DEFAULT_MODE_DRIFT`), never
   BLOCK or repair. `--permission-mode`/`--dangerously-skip-permissions`
   win (`tests/test_permission_defaults.py`).
8. **Managed sessions own their policy.** `autoCompactEnabled:true` is pinned
   (a user-level false silently wedges 1M sessions). The fence is **split**:
   `availableModels` = lead set ∪ agent set ∪ fallback-only set, so
   every bound agent selector is admitted (else agent dispatch silently
   falls back to the lead — the 2026-07-28 routing incident; the
   delegation probe guards the wire model against the production-shaped
   fence every build); `/model` (`modelPicker`) offers only the **lead
   set**, the `premodel` hook denies any switch outside `lead-set.json` and
   asks on a family change, and the `postmodel` hook is the record seam: a
   user switch inside the lead set is recorded as `applied.lead`,
   never hidden, never drift. Compaction env is computed over the lead set
   only: one window (min(window ceiling, the lead set's smallest provider
   bound)) and percent for the lead and every 1M-class agent.
   `CLAUDE_CODE_RETRY_WATCHDOG=1` is pinned in the durable settings env:
   managed sessions auto-recover transient upstream errors instead
   of dying for lack of a typed "continue" (mid-stream-after-content errors
   remain unrecoverable at the pinned client — documented).
9. **Simplicity budget.** No new mode/schema/daemon/state without a
   demonstrated failure case. Prefer deletion over addition.
10. **Upstream `claude` is never configured or hijacked.** No global
    writes (`~/.claude/agents`, `~/.claude/settings.json`, gateway env).
11. **Non-Claude outbound cache-retention boundary.** The gateway
    patch `cli-proxy-api-non-claude-cache-retention.patch` (pinned
    CLIProxyAPI 7.3.15; derived over the split executor files)
    enforces that `prompt_cache_retention` — the OpenAI Responses-platform
    cache TTL control — is stripped at the **final outbound boundary** for
    every non-Claude route. One sanitizer
    (`internal/runtime/executor/non_claude_cache_retention.go`) is called at
    exactly these points:
    - **Claude executor** `Execute` (`claude_executor_execute.go`) and
      `ExecuteStream` (`claude_executor_stream.go`), third-party base URLs
      only, after translation, payload rules, cloaking, message
      normalization, CLI identity and sensitive-word obfuscation, and
      **before CCH signing** (the signature covers the sanitized body).
      Covers Kimi/Qwen-GLM/DeepSeek/Meta/OpenRouter `claude-api-key` routes
      and the Kimi OAuth executor's Claude-format path, which delegates
      here.
    - **`countTokensUpstream`** (`claude_executor_tokens.go`), third-party
      base URLs only, after its final body transformation (it has no
      signing step). Reached by the official origin and by the Kimi OAuth
      executor's `CountTokens`, which still delegates upstream.
    - **Codex `cacheHelper`** (`codex_executor_request.go`), after
      cache-key insertion, input-ID sanitization and identity rewriting:
      ordinary `/responses` execute and stream, `/responses/compact`, and
      the image paths.
    - **Codex WebSockets**: `Execute` (`codex_websockets_execute.go`) and
      `prepareCodexWebsocketStream` (`codex_websockets_stream.go`), after
      identity rewriting and before logging/frame transmission. Every
      `response.create` frame — first send, retries, and duplex
      `response.create`/append follow-ups — is built from that body.
    - **xAI** `prepareResponsesRequestTo` (`xai_executor_request.go`), at
      the end of the shared final preparation (HTTP, `/responses/compact`,
      WebSocket); upstream's single early `sjson` delete is folded into it,
      not duplicated.
    - **Meta** `prepareResponsesRequest` (`meta_executor_execute.go`), same
      pattern: upstream's early delete folded into the final invariant.
    - **OpenAI-compatible chat** (an extension of this patch):
      `OpenAICompatExecutor.Execute` and `ExecuteStream`
      (`openai_compat_executor.go`), after the final body mutations (translation,
      payload rules, tool-result and max-tokens normalization, prompt-cache-key
      insertion; the stream also `include_usage`) and before the request is
      built, keyed and keyless alike. `Execute` strips on its chat branch;
      `ExecuteStream` always sends `/chat/completions`, so it strips
      unconditionally, even under the `responses/compact` Alt. The one exception
      is `Execute`'s genuine non-stream `/responses/compact` request, a
      Responses-platform request that keeps the field (below).

    **Not applicable on 7.3.15** (no outbound payload the boundary could
    miss): `CountTokens` for a custom/third-party base URL is now
    **local** — upstream estimates tokens in-process and sends no request —
    so Kimi/Qwen/DeepSeek/OpenRouter `count_tokens` results are local
    estimates, no longer the provider's count (a behaviour change from
    7.2.80); only the official Anthropic origin still counts upstream. The
    duplex `response.steer` passthrough needs `codex.response-steering`,
    which the render leaves off. The Kimi OAuth executor's own
    OpenAI/Responses passthrough is not a claude-multi route (the catalog
    has no Kimi OAuth pool; Kimi is a `claude-api-key` direct route).
    (Unmodified by design, as before: the `/alpha/search` server
    passthrough, plugin management routes, and Claude OAuth refresh — they
    carry no messages/responses payloads. Also 7.3's codex realtime/live
    passthroughs (`/v1/realtime*`, `/v1/live`; `internal/api/server_routes.go`
    ~81–100): they carry SDP/session JSON, not Responses payloads, so there
    is no retention field to strip.)

    Claude classification is **endpoint-first**: only the default base URL
    or a resolved official HTTPS `api.anthropic.com` (default/443 port,
    case-insensitive host, any path, no userinfo) keeps the field; custom,
    non-HTTPS, non-default-port, userinfo-bearing, or malformed base URLs
    strip regardless of token shape — an OAuth-shaped token never upgrades a
    non-official endpoint.
    OpenAI-compatible platform Responses keep the field: only the genuine
    non-stream `/responses/compact` request of `Execute`; every chat request of
    that executor strips it (above). The sanitizer
    removes **all** top-level occurrences (duplicate keys included),
    preserves `prompt_cache_key` and nested entries, is idempotent, and
    fails closed with an enforced postcondition — empty/nil bodies fail
    closed as invalid JSON; no body leaves unless provably free of the
    field. The patch hunks carry context against the sequentially patched
    source; the module applies patches in manifest order and the
    patch-manifest tests compare exact ordered lists — never reorder or
    drop entries without regenerating the patch. The recipe's typed Go gates
    (`gateway/UPSTREAM.json` `gates`, run by `tools/build.py gateway gates`
    and the flake checks `gateway-gates`/`gateway-race`, network-free) run
    `go test ./internal/runtime/executor` (retention + kimi-compat
    executor tests) plus the kimi-compat config/synthesizer/sdk tests,
    plus the config
    watcher gate `go test ./internal/watcher/...` (real fsnotify
    rename-replace, held-open old inode, temp/sibling names ignored,
    blank/whitespace/missing never load, api-key-dropping reload refused),
    plus the start-up gate, a second filtered
    `go test ./sdk/cliproxy -run '<the patch's seven start-up tests>'`
    (`test_packaging.StartupGateTests` pins the list and the patch body;
    the barrier tests run under the watcher gate),
    plus the management allowlist gate `go test -skip '^(…)$'
    ./internal/api/...` (the patch's own `TestManagementAllowlist_*` tests
    — on production-shaped servers with the plugin host wired as
    `sdk/cliproxy/builder.go` does, plus a source tripwire that pins every
    management-key consumer by file and count — and every upstream
    `internal/api` test except seven that pin surfaces
    the allowlist refuses by design, skipped by explicit name — no wildcard,
    no `-count`; `test_packaging.ManagementAllowlistGateTests` pins the list
    and the patch body); keep those gates when
    adding routes, and keep the sdk/cliproxy runs filtered (that package
    carries an upstream failure on pristine 7.3.15, and the allowlist patch
    fails three of its end-to-end tests that write through the management
    API). Do not add model-name
    conditions, do not strip globally, and keep the patch independently
    removable — any new non-Claude route must route through one of these
    boundaries, never re-introduce the field after them. A new executor
    file in a future upstream release is an **uncovered route** until it is
    added to this list (the list follows the split executor files of 7.3,
    which added routes the 7.2.80 patch never saw).

    **Coverage boundary:** the end-to-end `R1`/`R2` retention rows discriminate the
    third-party Claude execute/stream sanitizer, including duplicate top-level
    keys; `R3` guards byte-preserved nested retention. The tokens, codex HTTP/compact,
    codex WebSocket, Meta and xAI call sites above remain **Go postCheck-only**;
    Python codex/compat rows are translator/executor facts, not proof of those
    sanitizer calls. The OpenAI-compatible chat boundaries differ: both
    are covered by the patch's own compat-retention Go test in postCheck, and
    the `P1` patch row (`PatchOmissionProbeTests.test_compat_retention`, probe
    `TestOmissionCompatRetention`) discriminates the non-stream `Execute`
    boundary by omission on the built gateway; `ExecuteStream`'s is postCheck-only.
    The `O2-B088` observation row pins zero upstream hits for direct count_tokens, not estimate accuracy
    or compaction safety.
    The sole patch-list owner is the recipe `gateway/UPSTREAM.json`
    (ordered `{basename, sha256, admitted}` series); `nix/gateway.nix` reads
    it, `catalog/gateway.json` mirrors it (pinned by tests) and the flake
    derives the package's attestation from `passthru.gatewayPatches`.
    That export describes the shipped local series, not downstream overrides.

## 3. Module map (src/claude_multi/)

| Module | Owns | Key contracts |
| --- | --- | --- |
| `state.py` | atomic writes, private dirs, FileLock | symlink-safe; `CommittedStateError`; lock fd `O_CLOEXEC`; `filesystem_failure`/`failure_text` are the one text of a no-space, quota, permission or read-only failure (what and where, home-relative, plus the fix; an own error keeps its message): the entry point's and the terminal `lineup`'s (whose `/cm` form keeps status 0) |
| `strict_json.py` | strict JSON + canonical bytes | dup-key rejection, size limits; `canonical_file_bytes` (state), `pretty_file_bytes` (user-facing docs) |
| `sessions.py` | records v1–v4, store, pointers, locks, adoption, reconcile | **record v4** (closed `oneOf` branch of `schemas/session.schema.json`): `profile`, `follow`, `applied` (`lead`/`agents` bindings with key, generation, selector, effort; the Settings snapshot, `compaction`, `native_agents`, `workflows`, `no_subagents`, `lead_providers`), `applied_hash` (= `bundle_digest(applied)`, the CAS key; recomputed by `_validate_v4_invariants`), `lineup_generation` (0 = no v2 scope: migrated/linked records), `lead_class`, optional `migration`, `pending` (a relaunch-class change the next `-r` applies), `title`, `launch_fence` (`launch_digest` of the launch-time `settings.json` keys `LAUNCH_TIME_SETTINGS_KEYS` + the `lead-set.json` bytes; required iff gen ≥ 1), `lead_target`, `scope_lead`; identity/lifecycle fields keep their v3 names. Builders are pure (`make_v4_record`, `next_v4_record`, `applied_from_lineup`, `applied_document`, `restore_overlay`); `lead_needs_choice` is the one needs-a-choice predicate (a null successor or a key unknown to the merged catalog); `record_summary` is the one row reader; `recorded_window` is the compaction window a record launched with (record-authority compiles decide agent classes against it). **State marker** `<state>/state-version`: missing = 3, ≤ 4 accepted, newer/malformed/symlinked → `StateMarkerError`; `ensure_state_v4` (bounded exclusive wait) writes `4` before the first v4 record; `SessionStore.save` writes **only v4**; lifecycle writers (`reconcile_runtime` with the `normalize` callback and tri-state v4 model evidence, `record_session_end` (runtime-compared), `relink_runtime`, `resolve_fork`, `converge_pending_forks`, `link`'s parent writes, `record_lead_switch`, `mark_ended`) patch the raw document and save it **in its own version** (`_save_lifecycle`; a v3 record stays v3). **Migration lock**: `migration_lock(root, shared=…)`, `launcher_write_guard` (shared, non-blocking, around every launcher write → `MigrationBusyError`), `acquire_exclusive_bounded`, `migration_window_wait` (hooks wait ≤ 3 s before skipping); lock order migration → runtime-index → lifecycle → pointer. Pointers: one `<d>.json` per cwd (the legacy `.ordinary` file is read by `last()` only; per-file `_pointer_names` gates for writes). `forget_session` also removes `<id>.v3.json`, pointer backups naming the id, `.seen` markers and the lineup log. `pending_fork_message`/`relink_message`/`fork_adopt_command` are the single message sources (v3 and v4). The product writes no v3 record; tests build v3 inputs with `tests/_v3.py` |
| `migrate.py` | `claude-multi migrate` and `restore-2x` | `convert_record` (v1/v2/v3 → v4: the translated legacy keys, never resolved at migration; `translate_key` (public; `profile migrate` uses it), the first retirement after the record's catalog; the mechanical agent rule; `applied.settings` = the default snapshot; `profile` from `migration_map`); `plan` (the dry run: no lock, no write, not even a shim refresh) → `MigrationReport` rendered by `render_text`/`render_json` (blocked records, possibly-missed hooks, stale transcripts by path/mtime only); `run` takes the migration lock **exclusively (blocking)**, writes the marker itself (never `ensure_state_v4`), converts under each lifecycle lock with byte-exact `sessions/<id>.v3.json` backups and merges/backs up pointers; a convert target whose profile file does not exist refuses and names `claude-multi profile migrate`; `migrate_one` is the launch-time migration of one record (bounded exclusive wait); `restore_2x` (from v4 state only): refuses while any record may be live (● daemon, `/proc` cmdline, last event ≠ `end`) unless `--not-running`/`--assume-dead` (never for a ● id), re-checks each record under its lock, overlays the backup with the current lifecycle (`sessions.restore_overlay`), quarantines v4-born records to `quarantine-3x/`, restores pointers, retires backups to `restored-2x/<ts>/`, removes the marker last; `--assume-dead` also clears the caller's own `CLAUDE_MULTI_MANAGED_ID` when every process naming it is an ancestor of the caller (`sessions.ancestor_pids`) and the session is not daemon-owned, so the rollback runs from the lead's own session. On live state only as a rollback to an earlier launcher |
| `migrate_profiles.py` | `claude-multi profile migrate` (dry run by default) | the legacy presets (catalog-32 v1 documents under `<config root>/compositions/`) → profiles by `migration_map` (reviewed rows read at call time, the generic rule for every other name; no second copy); keys through `migrate.translate_key` as of catalog 32, an omitted lane = the frozen catalog-32 default; a bare run and the dry run only report; the apply writes through `ProfileStore` (`new` per target, `install_seeds` for absent seeds) and nothing else, and refuses while any outcome blocks; the preset files are never opened for writing; never imports `cli`/`tui`/`views`/`launch`/`scope`/`compiler`/`hooks`/`transition`/`lineup` |
| `migration_map.py` | how legacy compositions become profiles (`migrate` and `profile migrate` read it) | reviewed rows `PRESET_TARGETS` (seed / convert / dropped) and `CONVERT_RULES` (`ConvertRule`: rule kind, overrides, `lead_providers`, `primary_provider`, `expected_warnings` as profile finding codes) — none ship, tests supply their own; a composition without a row follows `generic_row` (named like a shipped profile → that seed; otherwise a mechanical `generic_rule` conversion into a profile of its own name; the legacy `default` gives way to the default profile); `record_profile(composition_name)` (dropped rows, `default`, names without a row and every ordinary record → `null`) — the single source, never copied |
| `lineup.py` | `claude-multi lineup <request>` / `/cm`, pending relaunch changes, propagation | `parse_cli` (skill mode = `--session` present: stdout, exit 0, refusals prefixed `claude-multi: `, never `/dev/tty`; exactly one request argument; an empty `${CLAUDE_SESSION_ID}` falls back to `CLAUDE_CODE_SESSION_ID`; intercepted on the raw argv before `split_passthrough`), `show` (the gateway line, then `provider_error_lines`: the last day's failed requests per provider the session binds, matched by its bound selectors, gateway-wide, with the log read's coverage and the `/cm fallback <provider>` or `/cm set <agent>=<model>` remedy; read by the command adapter's `provider_errors` callback through `gateway_facts.report_events`, the bounded log doctor and Sessions V read; unreadable is unavailable, never zero), `parse_request`, `build_target`, `classify` (LIVE iff a gen ≥ 1 scope, equal `relaunch_fields()`, the target lead in the on-disk lead set, every agent selector in the launch-time fence proven by `launch_fence`, no context gap; else RELAUNCH), `apply`: under the shared migration hold + lifecycle lock, a LIVE apply writes agent files (new → changed → removed) → `lineup.md` → `lineup.gen` → lineup-log `apply` line → record last (never `settings.json`/`lead-set.json`; non-launch files compiled through `transition.expected_plan` against the live scope, so proven launch files state `lineup.md`/`lineup.gen` exactly as doctor and converge expect), a RELAUNCH inside the session becomes `pending` (applied by `claude-multi -r <id>`), `--relaunch` from outside releases every lock before confirm/prepare/perform; agent-name collision and credential checks for newly bound agents; generation fencing. The pure helpers (`binding_label`, `diff_rows`, `window_diff_rows` — the "context window" row a resume shows when the ceiling or the lead set moved the window —, `render_diff`, `render_text` with `window_summary`, each role's window grouped) serve the resume diff and the TUI. Propagation: `followers` (preview), `on_saved`/`on_removed` decide membership under each record's lock (rename pins followers and keeps `lead_target`; removal clears it) |
| `composition.py` | the legacy v1 composition reader + the context-policy constants | only `CompositionError`, `LEAD_ID`, `WORKFLOWS_VOCABULARY`, `AUTO_COMPACT_PERCENT`/`AUTO_COMPACT_OUTPUT_RESERVE`/`AUTO_COMPACT_REACTIVE_HEADROOM`/`OPERATING_WINDOW_CEILING` (800K), `operating_window`, `auto_compact_trigger` ((capacity−20K)×90%; `compiler.py` imports the constants and these two), and the catalog-independent `validate_document`/`load_composition_file` (`profile migrate` reads legacy presets with them; `schemas/composition.schema.json` and `catalog.SUPPORTED_DATA_VERSION` stay for the same reason). Tests that need legacy session snapshots build them with `tests/_v3.py` (a verbatim copy of the resolver and `validate_composition` over `tests/fixtures/v3/default-composition.json`) |
| `profile.py` | profiles v2, named bindings, lineup resolution, review routing, `BindingStore`, `ProfileStore` | pure except `load_profile_file`, the stores and the import-time schema reads (profile + bindings schemas read and checked **once at import**, so `evaluate`/`resolve` never touch the filesystem); never imports `cli`, `tui`, `compiler`, `scope`, `launch`, `transition` or `composition.py`; `parse` (E1 version before the schema, schema errors verbatim, E2 binding shape: exactly `{model, effort}` or `{use}`); `evaluate(doc, cat, *, bindings=, effective=, ad_hoc=)` → `Evaluation(errors, lineup)` — collects every error (never raises for content; an invalid catalog — unknown provider, malformed line, missing role — is an error, not a KeyError), resolves retired keys through `catalog.resolve_key_in` (successor + notice; a null successor is an error on the lead and an unbind + notice on an agent, and structural rules then fail closed with a hint), `effective=None` = no New line admitted and every provider enabled, E7 refuses a custom model except an ad-hoc direct lead; `resolve` raises `ProfileValidationError`; `ResolvedLineup` (lead + `session_effort` + `comparison_effort` + `lead_class`, bound agents in `AGENT_ROLE_IDS` order with `RoleSpec` and `lead_step`, `unbound`, `spread`/`spread_text`, `outsiders`, `named_bindings`, `applied_bindings()`, `relaunch_fields()`, `settings_overrides`); optional fields default `workflows` `native`, `description` `""`, `lead_providers` None, `settings_overrides` `{}`; `settings_overrides` = only `compaction_percent` (`OVERRIDABLE_SETTINGS`, bounds from `settings.COMPACTION_PERCENT_*`); warnings include (`same-family-review`, `lead-strong-concentration`, `strong-not-stronger`, `fan-out-quota`, `context-below-lead-class`; `primary_provider` suppresses concentration/fan-out for that provider) plus `strict-tool-schema` (any slot on `STRICT_TOOL_SCHEMA_PROVIDERS` = `meta`; a warning, never an error); `role_window(selector, *, client_tokens, provider_tokens, policy, decide)` is the one context-class rule (an agent on a `[1m]` line whose provider bound is below the policy window runs on the alias without `[1m]` in the 200K class; otherwise its class runs at min(class, window)); `evaluate` decides agent bindings under `session_policy` (the lead set of the resolved lead, the effective ceiling, the document's percent) and carries it as `ResolvedLineup.policy`; `binding_selector(entry, provider, effort, *, lead)` is the single selector rule (client-effort → the line selector; gateway-effort → `efforts[effort]`, an ultracode lead → `efforts[default_effort]`) for the lead set, the agent files and the live switch; `derive_routing`: review routing from families only (authors = lead + bound writer grades; no bound reviewer → empty cells, never an error); `ad_hoc_direct(model, effort)`. **`BindingStore`** (`<config root>/bindings.json`, `{"version": 1, "bindings": {<name>: {model, effort}}}`, 0600; operator state, never a catalog document): B1–B9 on added or changed names only (name grammar, collision with live keys/retired keys/`@` bases, line/effort checks, B8 delete refused while referenced, B9 a change that would newly invalidate a referencing profile is refused when `profiles=` is given); `load` tolerates stale collisions and retired models (`conflicts()` reports them). **`ProfileStore`** (`<config root>/profiles/<name>.json` over `Catalog.seed_profiles`): a seed without a user file is served virtually; `new`/`duplicate` refuse an existing name (P1), seeds refuse `rename`/`delete`, `reseed` is the only seed overwrite, `install_seeds()` writes only absent seeds and is called only from write-allowed entry points, `seed_updates()` reports newer shipped seed versions (doctor Attention), `referencing(name)`; saves are schema-only (semantic checks are the caller's `evaluate`); `commit` (`save`/`new` returning the digest of the bytes it wrote: an editor's next expected digest, never an unlocked re-read), `seed_snapshot` (a seed's state and the digest of the same bytes) and `refresh_unedited` (a batch reseed that re-judges every planned seed under one lock acquisition before writing any; `ProfileBatchError.changed` names every profile whose file a part-way failure left replaced — the one it stopped at too when only a later step of it, such as its install record, failed — so no message calls a changed profile unchanged and each gets the propagation offer). Both stores: construction and reads are side-effect-free; every mutator runs state-marker check → `ensure_private_dir` → **one** acquisition of the store's leaf lock (`bindings.json.lock`, `profiles.lock`) → load/check/mutate → unlocked atomic write — a store method never re-acquires its own lock |
| `compiler.py` | pure launch plan (argv/env/lead prompt) | one compile path for every session: Direct lists v2 lines through `views.line_rows` and the v3 hook reconcile resolves selectors on v2 lines in `cli._direct_model_for_selector`; requires scope_dir, hook_command **and** `token_helper_command`, and asserts the scope `apiKeyHelper` (fails closed); unsets `CREDENTIAL_ENV_KEYS`; never PATH-fallbacks; family defaults = `family_default_env`: `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET}_MODEL` = the `fable`/`opus`/`sonnet` line's `wire_model` (+`[1m]` for a 1M line) — a missing or New (`status: new`) line omits its variable, a non-Anthropic family line raises; `compile_lineup_launch(*, docs, prompt_bodies, lineup, effective, session_action, lineup_generation, state_root, scope_dir, hook_command, token_helper_command, launch_epoch, passthrough, no_subagents, session_cwd, launch_environ, worktree_available)` → one `CompileResult` for every session, managed or direct; reads only v2 data (`profile.LineupCatalog.from_docs`; callers pass merged catalog ∪ custom docs); argv `--session-id\|--resume <id> --name <lineup_session_name> --settings <scope>/settings.json --model <lead selector> --effort <session_effort> --add-dir <scope> --append-system-prompt-file <prompt>` + passthrough (resume pins `--model`/`--effort`; name `cm:<profile>` or `cm:direct:<lead key>`, plus the legacy `@project` suffix); process env from `lead_set_context(fence, percent, ceiling=)` (`profile.window_policy` over the lead set: window = min(`Effective.window_ceiling`, the smallest provider bound), `reactive_trigger`, scalar only below a 1M client class), `percent = settings.compaction_percent_for(eff, lineup.settings_overrides)` (a profile override wins; `Effective` stays Settings-level); a lineup whose `policy.window` differs from the compiled window is refused (its agent classes were decided for another window); `agent_context_gaps` fails a bound agent whose provider bound is below its client window and the process window; `env_unset` = `v2_env_unset`: `V2_ENV_UNSET` (credential keys, family defaults, Explore cap, subagent model keys, compaction keys) + `POLICY_DEFEATING_ENV_KEYS` + `GATEWAY_SECRET_ENV_KEYS` (`MANAGEMENT_PASSWORD` and mixed-case inherited spellings) + every merged provider `secret_ref` env name + every `*_API_KEY` of `launch_environ`, never `ENV_UNSET_KEEP` (`CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_MULTI_SECRET_ENV`: product names only, an operator's MCP key is unset like any other; the gateway render reads secrets from the secret file, never the process env); family defaults and the Explore cap live only in the scope's flag settings; `generate_session_appendix` is lineup-independent (relaunch-only inputs + the `lineup.md` path) and the lead prompt path is content-addressed `lead-prompt-<sha256[:16]>-<uuid>.md` (the prune regex shape); `git_work_tree(cwd)` is the one-`git rev-parse` seam that marks writer grades unavailable outside a git work tree; `workflow_default_line` is the one text for `workflow_default_binding` (workflow `agent()` without a `cm-*` agentType and native general-purpose, never Explore/Plan, as the placement probe shows); `CompileResult` carries `fence` and `lineup_generation` |
| `scope.py` | scope plan/write/gate, hook + token shims | `resolve_hook_command`/`ensure_hook_shim`/`hook_shim_path`; shim chmod repaired unconditionally; `ensure_token_helper_command`/`gateway_token_shim_path` (apiKeyHelper); `CatalogMeta.gateway_base_url`; exact `cm-*` collision gate; **The one scope compile** (`swap_scope`/`restore_prev_scope`/`remove_live_scope` are the `.new`/`.prev` swap every launch and converge uses): `compile_lineup_scope(lineup, cat, eff, prompt_bodies, meta, *, lineup_generation, managed_id, hook_command, launch_epoch, token_helper_command, no_subagents, worktree_available)` → `ScopePlan`, pure and fail-closed: agent files per bound id in `AGENT_ROLE_IDS` order (`name, description, model, effort[, isolation][, disallowedTools]` from roles v2, description = role text + sentinel, agent `ultracode` refused; the legacy tool-list guard is deleted); the **split fence** (`compile_fence` → `Fence`): lead set = offered lead-capable lines of the lead class, narrowed by `lead_providers`, one `LeadSetRow` per effort selector through `profile.binding_selector` (a list-shaped/custom line gives one row); agent set = every selector of every offered agents-capable line plus the selector its agents run on under the session window (`profile.role_window`: the alias without `[1m]` for a bound below the window) and the selector of every agent bound on an offered line; fallback-only = `REFUSAL_FALLBACK_WIRES` + `[1m]` iff a lead row is Anthropic-family (in `availableModels` only); `availableModels` = sorted union; `fence_gaps` (exact membership, never prefix) asserts the lead, every bound agent and the workflow default; `modelPicker` = the lead set with `replaceBuiltInOptions`; settings env = family defaults over offered lines + `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP` + `CLAUDE_CODE_SUBAGENT_MODEL` from Settings `workflow_default_binding` (`workflow_default_selector`/`workflow_default_window`, its class decided under the session policy; FORCE never); `worktree.baseRef: head` and `autoCompactEnabled` always; `disableWorkflows` also under `--no-subagents`; `SECRET_PATH_DENIES` (the fixed HOME-relative Read/Edit denies) on every v2 scope, plus `secret_path_denies(environ)`'s environment-dependent ones (a moved Anthropic config folder, a key file `CLAUDE_MULTI_SECRET_ENV` names outside the protected folders); six hooks through shim-3 ending `--hook-protocol 3`, no matcher (the legacy shim is refused) plus `CLAUDE_CODE_RETRY_WATCHDOG` for direct too; `ScopePlan.other_files` = `lineup.md` (`lineup_md_bytes`, ≤ `LINEUP_MD_MAX_BYTES` 16 KiB: lead, agents, provider exhaustion, routing table, round cap, rules, checks), `lineup.gen` (`<N> <sha256(lineup.md)[:12]>`), `lead-set.json` (lead, rows, launch-time `context` window/trigger/percent/scalar — never rewritten by a live apply) and `.claude/skills/cm/SKILL.md` (`SKILL_MD_BYTES`, `'$ARGUMENTS'` single-quoted); `plan_hash` covers `other_files` only when non-empty (legacy hashes unchanged); `line_view` is the single merged-view seam (`OfferedLine.source` `catalog`/`custom`); effects `stage_plan` (all relpaths validated before any write), `write_scope` = stage + swap, `live_drift` (the moved `transition._live_drift` body, `other_files` included), `read_disk_plan`; `find_skill_collisions` (read-only). **Shim-3:** `hook_shim_v3_text`/`ensure_hook_shim_v3`/`hook_shim_v3_path` (`<state>/bin/claude-multi-hook-3`, same write discipline as the legacy shim, whose frozen text is `hook_shim_text`): the UserPromptSubmit fast path reads the runtime id only from a payload that starts with `{"session_id":"` (the client's order; linear), requires lowercase UUIDv4 ids, and exits 0 starting no process but `cat` when `scopes/<mid>/lineup.gen` equals `notice/<rid>.seen`; the shim never execs, forwards launcher stdout only on exit 0, passes the status through for `start`/`end` only and exits 0 for every other event (older-launcher argparse rejection, missing launcher, signal); `premodel` fails closed: a missing launcher or a non-zero exit prints the fixed deny (`hooks.premodel_fail_closed_response`, rendered into the shim text) |
| `hooks.py` | protocol-3 hook handlers: lineup notice, model-switch fence, postmodel warnings, spawn log | `cli.main` dispatches `SCOPE_ONLY_EVENTS` (`prompt`, `premodel`, `postmodel`, `subagent`) **before** Runtime: no catalog load, shim write, store dir or record lock; imports only stdlib + `client_check`, `lineup_files`, `lineup_log`, `sessions`, `state`, `strict_json`, `platform.posix_fs` (the flock) (the set is pinned by `test_hooks`; never `cli`/`compiler`/`catalog`/`profile`/`scope`); never raises, `dispatch` always returns 0, stdout carries at most one JSON document written once; payload ≤ 4 MiB strict JSON; an internal error → `{"systemMessage": "claude-multi <event> failed: <Class> — run claude-multi doctor"}` (`premodel`: an explicit deny carrying it, `premodel_fail_closed_response`, the same fixed deny that shim-3 and `cli.main`'s argparse rejection answer), one sanitised stderr line and one metadata line (time, event, managed id, class — never payload) in `<state>/hook-errors.log` (0600, 256 KiB, one rotation; doctor counts it); `prompt`: the notice when `<state>/notice/<rid>.seen` ≠ `lineup.gen`, `.seen` written after stdout (never on an md/gen hash mismatch); `premodel` (`classify_switch`): exact `normalize_model(to_model)` against `lead-set.json` rows (never wire/prefix; `requested_model` ignored) → deny outside, ask on a family change, else allow; `from_model` unmatched → the compiled lead; damaged `lead-set.json` → deny naming `doctor --repair`; `postmodel` (`USER_SWITCH_SOURCES` `command`/`picker`/`sdk` only): warns on an out-of-set target and on a claude-multi selector saved as `model` in the user's Claude settings (read only, never written; the launcher's postmodel path records `applied.lead`); `subagent`: one lineup-log line (`agent_type`, `agent_id`, `lineup_gen`, `scope_selector` from the agent frontmatter, `label` "binding at gen N (reload unconfirmed)"); `read_notice`/`notice_text`/`notice_response` build the notice (in-process notices add "Lineup gen N staged — active after /reload-plugins in this session"; `read_notice(lead=)` is the `applied.lead` seam, None → `lead-set.json` lead); `migration_lock_held` probes `<state>/migration.lock` shared and non-blocking, never creating it; `transcript_path` and `CLAUDE_CODE_SESSION_ID` are never read |
| `lineup_files.py` | names + pure helpers shared by `scope.py` and `hooks.py` | stdlib-only leaf: `LINEUP_MD`, `LINEUP_GEN`, `LEAD_SET_JSON`, `SKILL_RELPATH`, `LINEUP_MD_MAX_BYTES`, `GEN_LINE`, `normalize_model` (lower-case, one trailing `[1m]` removed), lineup-log path helpers; no filesystem access |
| `lineup_log.py` | the per-session lineup log | `<state>/lineup-log/<managed_id>.log` — outside the scope, so never plan content, hash, drift or golden; canonical JSON lines ≤ 4096 bytes appended under `flock` on an `O_APPEND\|O_NOFOLLOW\|O_NONBLOCK` fd that must be a 0600 regular owner file (symlink/dir/FIFO/foreign/0644 refused, never followed); one rotation to `.log.1` at 1 MiB (`append_bounded`, shared with the hook error log); `binding_label(N)`; `remove`/`log_ids` for forget and `doctor --prune`; imports only `state`, `strict_json`, `lineup_files`, `platform.posix_fs` (the flock) |
| `launch.py` | verify→readiness→state→execve | full-hash binary check every launch, under the pin's use lock (`PinnedCopy`, inherited across exec); **the one launch/resume/relaunch commit** `perform_launch` (v4 only): the shared migration hold (while migrate/restore runs), the lifecycle lock, the CAS on epoch/token/`applied_hash`/`lineup_generation` and the re-read `lineup.gen`, `scope.swap_scope` (`.new` → live, the old live to `.prev`), record save (`title` re-read under the lock, `last_event_source = None`), pointer, execve through the `execve` seam; every failure restores `.prev` byte-exactly |
| `transition.py` | converge + the record-authoritative expected plan | `expected_plan(record, …, live)`: the scope lead (`scope_lead`) and the recorded agents evaluated against the installed catalog with the record's Settings snapshot; a moved selector or an evaluation error refuses (applies at the next resume); the launch-time `settings.json` keys and `lead-set.json` are restaged byte-for-byte when `launch_fence` proves them, and `lineup.md`/`lineup.gen` are then stated against those proven files, so a catalog change never rewrites the fence or the lineup text under a running session; `catalog_launch_differs` compares with the catalog compile for the launch lead and the proven env's launch epoch. `converge` (lock-then-load, inside `launcher_write_guard`): v1–3 and gen-0 records are reported, never written; generation fencing persisted; drift swapped through `scope.swap_scope`; a `/model` switch is never drift. A relaunch is a resume with a target lineup |
| `cli/` (the package as a whole; owners in the rows below and §3.1) | commands, TUI screens, Runtime, doctor | `--legacy` refused before Runtime (exit 2); a newer state marker refuses every write command before Runtime and makes Runtime skip the shim refresh (read-only commands, `restore-2x` (its own marker check) and `session-event` exit 0 keep working); doctor drift compares the sentinel-bearing render of the same key slots (a paused rotation's exact phase render is info + Attention, never drift), a served-but-older sentinel BLOCKs as "gateway did not reload", a `previous-key` is Attention; `doctor --rotate-token` (tty only) drives `proxy.rotate_token` through the Runtime seam (its questions and the wait's progress on the terminal, the result on stdout, a pause or an interrupt on stderr; the TUI passes its one terminal stream); Runtime init refreshes both shims (not while an inhibition it does not own is recorded, §3.3) and degrades a broken contract override to packaged + `broken_override_error` (doctor BLOCKs; never applied); every loopback `/v1/models` + `/healthz` call goes through `Runtime.served_models`/`check_readiness` (injectable `served_models_callback`/`health_get`, default live; gateway token from the runtime HOME); report commands write to stdout, interactive flows to the tty; resume paths self-heal resolved-by-reality fork markers; a transition on a ● session warns and names `sessions stop`; `Runtime(allow_state_writes=…)` — `main` passes `command != session-event and no newer state marker`; with writes allowed the contract-override except-branch runs the the legacy override cleanup (`catalog.remove_legacy_contract_override`) and sets `contract_notice`, printed once to stderr (`show_contract_notice`) and shown by doctor as Attention `contract override removed: …`; doctor continuity: one `GatewaySnapshot` read of `continuity.json` (absent → `continuity.seed_only`, corrupt → the same seed set + one BLOCK, never drift), a persisted set extended from another state root is Attention (`claude-multi-proxy init --state-root <root>`), an unserved continuity alias is Attention (`claude-multi doctor --prune-aliases <alias>`), a missing file says nothing; `doctor --prune-aliases [ALIAS …]` drives `proxy.prune_aliases` (refused under a newer marker); `_doctor_rotate_token` passes the session state root; provider pane/listing read v2 lines + continuity and label a model-less provider `configured · no models[ · N continuity aliases]` (never `0/0`); custom collision sets = every v2 line + retired keys + `@` bases, never the view; `discover` prints a provider's `created` date; `discover openai` marks each id `cataloged as <line>` (v2 lines, New included) / `retired (<key>; continuity only)` (retired `last_wire`) / `candidate`, plus `visibility=`/`upgrade=`/`retires=`; every `discover` provider call needs real terminals and no Claude-session marker (§6 rule 1; `_provider_call_tty` = process stdin AND stdout isatty — never `/dev/tty`; the test seam), and the old approval flag only prints migration guidance; an unregistered id's hint is `claude-multi custom add-model <id> --provider <provider> --wire <wire> --context <n>`; the Providers-pane consent modal for a keyless listing shows the URL and never says "public" (llm-local is a LAN host); **Hooks:** Runtime init also writes shim-3 (`hook3_command`; under a newer marker only the path); `session-event` takes `start\|end\|prompt\|premodel\|postmodel\|subagent` and `--hook-protocol 3`; an argparse rejection of protocol-3 or non-start/end hook argv exits 0 (a `premodel` argv also prints the fixed deny, fail closed); v3 `start` (`_session_start_v3`) reads the payload once, clears the runtime id's `.seen`, builds the notice from scope files, and only then builds Runtime and runs the legacy reconcile under a broad except (any failure = one context line + stderr, the notice is still written, exit 0, no `.seen`); every SessionStart source (fork and unknown included) gets the notice, merged with the fork/repair text into one `additionalContext`; the legacy shim's `start`/`end` (no `--hook-protocol`) keep their output, except that errors go to stderr with exit 1, and both skip record writes while the migration lock is held; managed lead equivalence goes through v2 lines + `resolve_selector`/`last_wire` (`_lead_equivalents`), ordinary equivalence falls back to the record key's retired selectors; the doctor `CLAUDE_CODE_SUBAGENT_MODEL` radar says what the placement probe shows (workflow agents without a `cm-*` agentType and native general-purpose, not Explore/Plan; use Settings `workflow_default_binding`); **Launches:** one launch path (`Runtime.prepare(LaunchTarget, action=fresh|resume|relaunch)` → `Runtime.perform` → `launch.perform_launch`; `--profile NAME`/`--profile-file PATH|-`, legacy `--composition*` aliases; a direct provider without its credential blocks the launch; the line-mode confirm and chooser are the non-curses path — dumb `TERM`, no ctty, `--line`, a Python without curses — the confirm's body being `views.card_text(…, line_mode=True)`), `lineup` intercepted on the raw argv (skill mode), `profile list|show|new|edit|rm|rename|duplicate|reseed|migrate` (+ `compose`/`show` aliases), `migrate [--dry-run] [--json]` and `restore-2x [--not-running ID] [--assume-dead IDS]` (dispatched before the marker gate; `refresh_shims=False` for the dry run and `--print-launch`), `sessions link --profile|--direct`, `sessions mark-ended <id>|--all-dead`, `sessions resolve-fork` (refused before it asks when `sessions.fork_resolution_problem` names a reason; then y/N, default No, `--yes` off a terminal, like `sessions forget`), the `sessions transition` alias on the relaunch executor; doctor: profiles/bindings evaluated with current Settings (errors BLOCK, notices and credential gaps Attention), per-record retired keys and one needs-a-choice line, missing followed profiles, v4 scope integrity (`transition.expected_plan`, fence ⊉ bindings BLOCK, catalog-only and rebuilt-launch-file Attention, `/model` switch info), marker health, the settings radar (user + every record cwd's project/local settings: policy-defeating env keys, credential keys — which also refuse `--rotate-token` — `disableAllHooks`, `maxEffortLevel`, `alwaysThinkingEnabled:false`, `switchModelsOnFlag:false`, `disableSkillShellExecution`; names only), claude-multi selectors saved in the user's Claude settings (offer the revert, never write), `/cm` skill collisions, the pinned-registry retirement radar (30 days), one bounded `journalctl` pass (`_read_gateway_journal` seam; live seams only) for `invalid_grant` and per-model 429/402/403, hook-error counts since the last launch; `doctor --prune` also removes orphan `.seen` markers, superseded lead prompts, lineup logs of forgotten records and rotates `hook-errors.log`; `doctor --repair-all [--include-live]`; `sessions stop` waits out the compact-boundary window; **The TUI** (the curses screens render from `views`, draw through `tui`, and never import curses themselves): `_curses_ok(line, in, out)` = not `--line` and `tui.streams_curses_capable` routes the bare fresh launch (`launch_card`), `_resume_flow` (resume card; `NeedsChoiceError` → `_NeedsChoiceChooser`), bare `-r` and interactive `sessions list` (`_sessions_list_tui`) and `profile new\|edit` (`_run_profile_editor`) to curses; a curses start failure falls back to the line flow; every screen returns an intent and `Runtime.perform` runs after teardown, never inside curses; screens take no lock and write only through the store/`lineup` APIs (`ProfileStore`, `BindingStore`, `SettingsStore`, `lineup.apply`/`on_saved`/`on_removed`, `SessionStore.set_title`); a read-only Runtime refuses every write (`SETTINGS_READ_ONLY`). Screens: `_LaunchCardScreen` (with the `_CardHealthActions` mixin: health strip, U update, H doctor; `_plan()` prepares or shows BLOCKED; a resume card's sign-in and key remedies name its way back to a fresh card, `_WayBack`: one Esc per screen above it, or `run claude-multi` when Esc ends claude-multi; `profile_pick_order` = most recently used first over v4 records: this directory's profiles, then those used elsewhere, then the rest by name), `_DirectScreen`, `_SessionsScreen` (`_liveness` = ● running / ◐ unknown / ○ ended over the `_live_prefixes`/`_proc_ids` Runtime seams), `_LineupDialog` + `_PerAgentScreen` (lock-free preview, `lineup.apply` authoritative, RELAUNCH recorded `pending`, v1–3 refused), `_NeedsChoiceChooser`, `_ModelsScreen` (the only admit surface), `_ProvidersScreen` (the journal read through its `journal=` seam, `_journal_facts`), `_SettingsScreen` (four edited fields, `views.effective_context` recomputed after each write), `_PropagationScreen`, the profile-editor callbacks; the subprocesses a screen can reach are exactly these (`$EDITOR`, update, doctor repair, token rotation, the journal read; `git rev-parse` and `claude stop` inside the APIs), pinned by `tests/test_screen_static.py`; gateway-service remedies only through `gateway_service_hint(verb)`; `CompositionStore` is seedless and read-only (`IMPLICIT_2X_NAMES = {"default"}`; constructing it creates nothing) |
| `cli/__init__.py` | the lazy compatibility facade | imports nothing; `_COMPAT_EXPORTS` maps each old `cli.<name>` to its one owner module (explicit, no wildcard, no warnings); internal code imports the owners, and tests patch the owners, never the facade |
| `cli/entry.py` | `main` | module-level imports are the hook path only; every `session-event` is routed before a command module loads (§3.1) |
| `cli/dispatch.py` | `handle_command` | one delegation per command in the unchanged branch order; `custom`/`sessions` still fall through to the unsupported-command refusal |
| `cli/commands/` | one module per command | `profile` (+ the `compose`/`show` aliases, `_edit_profile`), `ceiling` (`window-ceiling`: show, set a value, `--reset`; out of range exits 2 naming the range), `sessions`, `migration` (`migrate`, `restore-2x`), `discover` (the in-session provider-call gate, `_provider_call_tty`), `custom`, `models`, `update` (the release update journey, `release_update.py`), `doctor` (action selection and report rendering), `lineup` (`_lineup_main`), `quota`, `gateway` (the gateway verbs), `providers` (the provider verbs), `repin` (`claude-multi-dev repin`), `uninstall`, and `setup`: without `--step` it runs every step not yet done; a step's own options (`parser.SETUP_STEP_OPTIONS`: `--claude-from` for `claude`, `--proxy`/`--no-proxy` for `gateway`) select that step and are refused on another one (exit 2) |
| `cli/launch_flow.py` | launch flows | the line confirm and choosers, `_resume_flow`, `_direct_command`, relaunch preview/exec, `_perform_card_result` (after curses teardown) |
| `cli/screens/` | the curses screens | `common` (shared widgets, `_LINE_FALLBACK`, `_curses_ok`, `_output_palette`), `models`, `direct`, `providers`, `settings`, `propagation`, `profile_editor`, `transition`, `actions` (the per-screen key actions), `gateway_actions`, `get_started`, `profiles`, `signin`, and `launch_sessions`: the card, sessions screen, lineup dialog, per-agent screen and needs-choice chooser call each other, so one module keeps that cycle; screens never import commands, dispatch or entry |
| `cli/doctor.py`, `cli/doctor_actions.py` | doctor reports; repair, prune and rotation actions | reports never import actions; report order, statuses and remedies unchanged from `cli.py` |
| `cli/gateway_facts.py` | gateway, journal and credential observations | explicitly invoked observations (never in `views.py`); the `_doctor_now`, `_read_gateway_journal` and `_oauth_credential_records` seams; reads one bounded structured journal window through `service` → `platform.linux_service`, then keeps only parsed facts; on the on-demand backend the same pass reads the instance logs (`file_log.read_recent`); `journal_command(runtime)`, `gateway_service_hint(verb, runtime)`; doctor's `local_files_report` (roots and secret files before the key check), `platform_report` (WSL, network filesystem) and `gateway_runtime_report` (backend facts, lock, crash counter, logs, hold, binary drift, proxy) |
| `gateway_events.py` | backend-neutral credential-save collector | strict patch-8 `credential_save_v1` grammar, provider allowlist, optional source cursor, normalized timestamp, gateway instance plus auth generation/epoch, explicit bounded/incomplete/truncated/unavailable coverage; raw messages transient, ≤256 events; source result types in `platform.observation` |
| `cli/runtime.py` | `Runtime` and the read-only legacy store | imports no TUI, screen, doctor or dispatch module |
| `cli/session_facts.py`, `cli/session_events.py`, `cli/session_actions.py`, `cli/selection.py`, `cli/resume_checks.py` | session metadata and liveness; the lifecycle hooks; session mutations (stop, link, mark-ended); resume/profile selection; the resume gate | `session_facts`, `resume_checks` and the reports take `Runtime` for annotations only (`TYPE_CHECKING`) |
| `cli/parser.py`, `cli/streams.py`, `cli/text.py`, `cli/types.py` | argparse, report/TTY stream selection, literal text, launch types | `text` imports only `termtext`; `parser` only `argparse`, `pathlib`, `claude_multi`, `lineup_files`, `cli.text`, `layout` (and `identity` lazily; pinned by `test_cli_split`) |
| `termtext.py` | terminal sanitizers and `CHEATSHEET_HINT` | stdlib leaf so hook error paths never load `tui`; `tui` re-exports them; not interchangeable with `hooks.visible` |
| `assets.py` | the resource-selection seam | `default_asset_root()`/`root()`: an explicit root, then a nonempty `CLAUDE_MULTI_ASSETS`, then `claude_multi.resources_root()` (the package's `data/`); `resources_root()` stays environment-independent and is the only source of the running version; source entry points drop an inherited override (`entrypoints.prepare_source`: a source tree is `nix/package.nix` and `src/claude_multi/` side by side, present in a checkout and in the tree the sandbox checks stage, never in an installation; `test_cli_split.AssetSeamTests` builds both) |
| `entrypoints.py` | the three commands and their launch environment | `run(name, argv)` is the one program table (`claude-multi-proxy` passes `chdir=os.chdir`; the one-model session is `claude-multi direct`, with no launcher of its own); the console scripts (`pyproject.toml` `[project.scripts]`) run `prepare_console` (an editable install is a source launch; otherwise an inherited channel, resource override or hook command is dropped: an unwrapped install states no channel), the `bin/` launchers `prepare_source`, and the Nix wrappers `python3 -P -m claude_multi.entrypoints NAME` with the environment they set (one per launcher of `packaging/product.json`, as in a bundle: `claude-multi-dev` stays a source checkout's entry point, and the sandbox check compares the built `bin/` with the manifest); a channel is never inherited or made up |
| `layout.py` | where files are, by authority | stdlib leaf on the hook path; the installation (`installation()`: a source tree holding `src/claude_multi`, or the prefix of an installed package — `<prefix>/lib/pythonX.Y/site-packages` —, `entry_point(name)` for the hook command and gateway fallbacks, `document(name)` for the help's doc pointers from `share/claude-multi` or `docs/`, `installation_resources(path)` for `plan --assets`); the source checkout (`CHECKOUT_MARKERS`, `RESOURCES_IN_CHECKOUT`, `verify_checkout`, `checkout_resources`, `checkout_destination`: every developer write — promote, repin, `tools/pin_claude.py` — stays inside the verified checkout through real directories, a symlinked `src`/`data`/`catalog` refuses); a resource override never moves an executable, a document or a write |
| `endpoint.py` | the gateway endpoint and backend | `set_up(home)` (an `endpoint.json`, or an earlier gateway's `config.yaml`/key) and `require_set_up` (`NotSetUpError`, remedy `SETUP_COMMAND`): a home with neither is a new install no command renders, reloads or ensures a gateway in; `gateway_endpoint(gateway_document)` returns `base_url`/`health_path` exactly as written; with `home=` the closed `~/.config/claude-multi/endpoint.json` (version, host 127.0.0.1, port, optional `backend`/`unit`/`proxy_url`) wins, the catalog is the fallback. `apply_to_catalog` gives Runtime and every proxy load (`proxy.load_bundle`) the effective gateway document, so scopes, readiness, render port and `operator.gateway_ports` agree. `ensure_config` (explicit starts only) records a new install's first free port of 18317–18336 (bind probe, never a connect); `resolve_backend` is on-demand unless the document records `systemd` (refused off Linux); `channel` reports `CLAUDE_MULTI_CHANNEL` and never selects the backend; `proxy_url` (credential-free, `outbound_proxy_problem`; set by `claude-multi setup --step gateway --proxy <url>` or `--no-proxy` and by Get started through the setup layer's `plan_proxy`/`apply_proxy`: the write happens inside the inhibition fence of the home's roots, `swap_proxy`'s `before_write` registers the undo first, and `restore_bytes` puts the document back exactly when the render is refused or interrupted) becomes the render's `proxy-url` via `effective_document` |
| `gateway_lifecycle.py` | on-demand lifecycle and the gateway verbs | `Gateway(home, state_root, environ, gateway_document, seams=Seams(...))`: `observe` (ours = instance lock held + exec stamp + PID runs the stamped binary + listener inode of that PID; free lock + no listener = stopped; anything else unknown or foreign, never stopped), `ensure` (start lock, reuse, refuse, one detached `claude-multi-proxy run --prepare-and-exec --detach --instance <nonce> --state-root <root>` with the allowlisted `spawn_environment`, bounded start readiness incl. the published render sentinel, crash counter 3/10 min for automatic starts), `stop`/`restart` (proof + persistence hold first), `status` (supervision, confinement `CONFINEMENT`, log count/bytes, outbound proxy, `binary_drift`, the inhibition), `logs` (the unit's journal or an instance's log file, the lines through `secret_store.redact_tail` as one run with up to `secret_store.TAIL_CONTEXT_LINES` lines above them as context, as is every `Outcome.log_tail` (read as `LOG_TAIL_READ`, cut to `LOG_TAIL_LINES`), so a value that spans lines is redacted whole, also when it starts above the lines shown; journal records end at a line feed only); under the start lock and before any dispatch (a new install's port included) every start, stop and hand-off checks the root's inhibition (`inhibition_refusal` → status `inhibited`, exit 1; the object holding the record's token is its owner and passes, and hands the token to the gateway it spawns), where `ensure` still awaits a gateway proven ours (read-only, never a start); an automatic `ensure` in a home that is not set up (no `endpoint.json`, no `config.yaml`/key) refuses instead of using the packaged port; a spawn first runs `prune_logs`; `state_root_refusal` refuses a WSL state root on a Windows drive; macOS observations go through `platform.darwin_process`; `running_binary()` names what an install cleanup must keep; the systemd backend delegates through `service.unit_control` after the same ownership classification (a stopped proof only enqueues `start --no-block`; foreign/unknown refuse with no manager request); one deadline bounds the start lock, every manager call, the spawn, the ownership, health and models observations (macOS `ps`/`lsof` included; each blocking call gets the time left) and readiness, which is never reported late. Runtime: `gateway()`, `ensure_gateway()` (skipped for injected fixture gateway seams) before launch/resume readiness; `provision_endpoint()` records a new install's port before `prepare` compiles a scope (and before the first automatic start), inside the launcher write guard after re-checking the state marker (a long-lived Runtime never writes into a migrating or newer state root), and readiness refuses a launch compiled for another endpoint; the public exits (`EXIT_CODES`) are 0 for an ok outcome and 1 for every other (the status and message keep the detail), and `cli.commands.gateway` adds 2 usage, 3 a declined `clear-hold` and 130 cancelled |
| `gateway_service.py` | the supervised service | `GatewayService(gateway, asset_root=, seams=ServiceSeams(runner, which))`: `install(name=None)` hands an on-demand gateway to the systemd user unit under the start lock (no inhibition another owner holds — a stale `service` one is taken over and finished —, proven ours, persistence hold, the hand-off's inhibition (owner `service`, `begin_handoff`; only the hand-off's own `Gateway` object passes it, through its random token; never by PID; an owner already holding the root's inhibition runs the hand-off under its own record), stop + proven exit, private dirs 0700, `installs.apply_selection` for the exec link, atomic unit files with the `systemd_unit.HEADER` line (carrying the launcher's `tls.configured` certificates, `unit_trust`: each `SSL_CERT_FILE`/`SSL_CERT_DIR` in the unit's environment, bound read-only where the unit's view hides it — HOME, `HIDDEN_ROOTS`; one that is not a readable file/folder, has a path a unit cannot carry (`systemd_unit.check_trust_path`: no whitespace, quotes, `%`, `$`, `:`, backslash or control character, after `%h/` too) or would show the unit HOME (`_exposes`: HOME, a folder above it, a hidden root or a folder above one, as set or through a link) refuses the install or refresh with `TRUST_REMEDY`, nothing changed; status judges a unit by the certificate locations it carries, `systemd_unit.trust_of`, with the binds the hardening policy requires for them (`_bind_required`), never the unit's own, and a carried location this release would refuse is stale), daemon-reload, FragmentPath check, `endpoint.set_backend`, `enable --now`, readiness), restoring the on-demand backend on any failure (the restoration stops the unit only behind `_stop_veto`: proven ours and clear of the persistence hold; a veto or an unproven exit keeps the service installed and selected and the hand-off record, and says how to finish; a restoration counts as complete only once the manager's daemon-reload of the restored files succeeded, else the record stays with its remedy); on an installed service it re-reads the endpoint under the start lock (refuses when the service changed meanwhile), recreates missing private dirs, refreshes files and link as a hand-off of its own (inhibition phase `refresh`, recorded before the first change and removed only once complete or completely restored — the files back and the manager's daemon-reload of them succeeded; an interruption or an unfinished restoration keeps it and names `gateway service install`), finishes a hand-off that died midway (`_finish_handoff`: enable, start, prove ready, only then remove the record) and classifies reload (launcher only) vs restart pending (unit or gateway binary changed, compared through `libexec/claude-multi/<gateway>`); `uninstall()` reverses it and restores the service when the on-demand start fails (complete, and the record removed, only once that restoration's daemon-reload succeeded), and with no service recorded finishes an interrupted `uninstall` record (`_settle_uninstall`: unit not running, its files removed, a daemon-reload that succeeded — every time, since an earlier attempt may have removed the files and failed to reload —, the record ended; a failed reload keeps it); `status(live=)` gives installed/name/backend/stale/switch-pending (doctor `gateway_facts.service_report`, TUI `gateway_actions`). The spec is packaged data (`load_spec`, never an asset override). |
| `gateway_inhibition.py` | the state root's gateway inhibition | `<state>/gateway/inhibition.json` (closed, version 1, 0600, written under `gateway-start.lock`): `owner` (`service`, `installer`, `updater`, `cutover`; any lower-case name), `purpose`, `phase`, `created`, `updated`, `expiry` (`owner-process` {pid, seconds} or `deadline` {seconds}: when it counts as stale — still refusing, never ignored), `remedy` (the owner's finish/release command), `token` (the owner's continuation, never shown). API: `begin` (one owner at a time; `take_over_stale` for the owner's own kind), `advance`, `end` (idempotent), `recover` (hands an interrupted owner its record back; nothing recorded is None; refuses a live or another owner's record), `read` (never raises: an unreadable record inhibits), `blocking(token=)`, `guard` (raises `Inhibited`: the installer's and updater's check), `refusal` (the one message + remedy every path shows), `fenced(roots, token=, home=)` (the writer fence of §3.3; `begin` takes it exclusively; with `home` the managed root is read again under it), `home_roots(home, state_root)` (the writer's root and the managed root; `managed_root` raises `AuthorityUnreadable` for a `continuity.json` that cannot be read), `revalidate(home)` (under the api-key lock before a write: the managed root must be one this flow fenced, else `AuthorityChanged`); an earlier `service-handoff.json` reads as a `service` record and every record writer migrates it first. `CLAUDE_MULTI_INHIBITION_TOKEN` carries the owner's token to its own commands (explicit gateway verbs, `claude-multi-proxy`, the shim refresh, the plan boundary); `gateway ensure` (the token helper) never honours it. `python3 -m claude_multi.gateway_inhibition status\|begin\|advance\|end\|recover\|check` is the shell owners' command (token on stdout or in the environment, never on a command line) |
| `installs.py` | channels, install roots, the channel marker | `check(state_root, environ, record=)` (the entry point's guard: claims the launcher's channel in `<state>/channel` on the first writing run under `channel.lock` (`claim_marker`; a claim that cannot be written refuses), refuses another channel except a read-only plain doctor, fails closed on unknown content), `guard(state_root, environ, writes=)` (the same for the raw `lineup` intercept, before its Runtime), `bundle_link`/`nix_link`/`discover` (doctor's installation list), `plan_selection`/`apply_selection`/`restore_selection` (the service's exec link; Nix: link to the running store path + `nix-store --add-root --indirect`), `install_root(environ, installation)` (the launcher tree, never a resource directory) |
| `choices.py` | `choices.json` | the closed document for choices older releases must never meet (`FIELDS`: `default_profile`, `session_env_keep`, `acknowledgements`, `window_ceiling` — the context window ceiling, 200K..800K, default 800K; `window_ceiling(environ)` never raises and names an unreadable file's problem, `parse_window_ceiling` reads `400000`/`400K` and refuses with the range); `read`/`update` (atomic 0600, locked, default value removes the key; `update(expected=digest(…))` compares the document under the same lock and refuses with `ChoicesChanged`, `replace(key, old, new)` writes only while the value is still `old`) |
| `account_pools.py` | the account pools as data | the packaged `account-pools.json` (closed schema `schemas/account-pools.schema.json`; the release's own data, never a resource override; read once): per pool its catalog `provider`, `display`, `sign_in` (`flow` `browser` — methods `browser`/`address` — or `device`, the account `host`, the `claude-multi-proxy` login `command`, the `signin_policy` values that offer it), `acknowledgement` (`id`, `text`), `record_prefix` and `registry` (`section` the catalog's lines are checked against, every one of the `sections` its sign-ins serve from); `record_prefixes` lists every prefix the pinned gateway names sign-in records with, a pool's or not. A pool's name is its gateway channel. `parse` adds the rules the schema cannot state (name grammar, a listed record prefix, the section among the sections, offered by `public`, no shared provider, display, login command, acknowledgement id or record prefix); `PoolTable.classify` gives a record name to the longest matching prefix (`kimi-ai-1.json` is never a `kimi` record), names only; `section_pools` maps a registry section back to its pools (two pools may share one). Imports only `errors`, `strict_json`, `validate` |
| `gateway_hold.py` | the persistence hold | `evaluate_files` (every retained instance log from the cleared offset; `file_log.scan_instance_logs` problems, unreadable logs and an unfinished last line (`file_log.read_evidence`) hold) / `evaluate_journal` (from the earlier of 24 h ago and the running instance's start, never before the clear; that instance's start marker must be in the window) through `gateway_events.recovery_hold`, recording every unresolved failure it sees as a hold with its journal `evidence` identity at once (a journal vacuum cannot unhold it; it resolves itself only while a complete window still shows that failure and its repair); recorded holds and unreadable records veto; `clear` (typed `claude-multi gateway clear-hold`) records per-instance offsets and removes only the recorded holds its `HoldReport.recorded` showed (newer ones stay and are returned; the verb then exits 1); `prune_at_start` (at a spawn, previous instances only, beyond `PRUNE_BUDGET`) persists each pruned log's unresolved failure or incomplete evaluation as a hold with its `coverage` before deleting the file; an unreadable hold record prunes nothing |
| `platform/` | POSIX backends (Linux and macOS) | the package imports nothing; `observation` (result types: running / stopped / unknown), `posix_fs` (directory fsync, one `os.replace`, flock, exec, ownership), `linux_service` (systemctl/journalctl argv behind `service.py`, plus the three control verbs and the unit-file verbs of `gateway service install|uninstall`), `systemd_unit` (the supervised unit's pure renderer and parser; `HEADER` marks product-written files), `linux_process` (bounded `/proc`), `darwin_process` (macOS: `lsof -Fpu` + loopback connect probe for the listener, kill(0) + `ps -o comm=` for the gateway PID, one bounded `ps -axww` session scan, one bounded `ps -axww -o comm=` executable-path read for the prune rule, the shared `/tmp/cc-daemon-<uid>` daemon root; sessions dispatch through `sessions._platform`), `mounts` (filesystem type under a path, WSL detection), `posix_process` (exists, SIGTERM, the detached spawn), `file_log` (per-instance logs: exclusive 0600 create, newest-first listing, bounded windows with `<nonce>:<offset>` cursors and UTC envelope times, `read_recent` for doctor; never truncates; `remove_instance_log` deletes only a whole finished log); a missing capability is unknown, never stopped |
| `pin.py` | contract v2 accessors, the owned copy and pin facts | `version`, `platform_record`, `evidence_class`, `settings_keys` (`verified[0].settings_keys`; required for a releasable pin, None — fixtures only — skips the skew check); `owned_path` (data root, `claude.exe` on win32); `verify_owned` (regular, non-symlink dirs and file, size, executable, full sha256 — every launch); `take_use_lock`/`take_use_lock_in`/`UseLock` (the per-version `.in-use` lock, see `retention.py`; `owned_version_of` names the version an executable path is the owned copy of); `copy_state` (metadata only, for the card); `installed_client`/`version_from_path` (the user's `claude` by real path, never run); `staleness` (> 30 days or a newer user claude; no command); `override_attention` (doctor text) |
| `acquire.py` | owned-copy acquisition (`setup --step claude`) | sources in order: `--claude-from`, retained `pinned-clients/<v>`, native versions dirs (`$XDG_DATA_HOME/claude/versions`, `~/.local/share/claude/versions`, Windows `%USERPROFILE%`), resolved PATH `claude`; else the consented download (`DownloadPlan`, Range resume, three tries, streaming hash, same-host redirects only); per-version host lock; 0700 dir / 0755 file via verified temp + rename; `migrate_retained` (launch path, copy never move); `pins.json` launcher → pin index for the prune rule; `setup --step claude` (`cli/commands/setup.py`) exits 0 when the copy is in place, 3 when the offered download is declined, 1 on a human-guard refusal, not set up or failure, 130 when cancelled |
| `trust.py` | release signatures | stdlib sshsig verifier (`SHA256SUMS.sshsig`, namespace `claude-multi-release`, sha512 prehash, strict Ed25519 with canonical-s checks), `allowed_signers` parsing with `valid-after`/`valid-before` rotation; `release_signers()` reads the packaged `release-trust/allowed_signers` (the only trust an installation verifies updates with; a file with no key refuses); `parse_sums` |
| `self_update.py` | the bundle update core | `read_installation` (`install/versions/<v>`, `current`, `previous` = the version current before the last switch, whatever the numbers), `check` (latest `MANIFEST.json` + signed `SHA256SUMS`), `plan` (size, restart class, Claude Code change and its download size, state format, rollback blocked), `apply`/`rollback` under the install lock and with **required** safety seams (`hold`, `protected`, `inhibit`, `prune_copies`, `acquire_pin`; `rollback` also the `confirmed` installation, refused when the links moved since); the two links change through `switch` (recorded in `install/.switch.json` first) and `finish_switch` puts an interrupted one back; the gateway hold vetoes a gateway-changing switch, versions a running gateway executes from are kept (unknown keeps every version) and never replaced in place, the incoming Claude Code is put in place before the switch, every step is a phase of `inhibit`'s record (`download`, `pin`, `replace`, `switch`, `prune`), and the cleanup ends with `prune_copies` for the new current; `install_lock`, `tidy` (an interrupted run's leftovers: a version moved aside as `versions/.replaced.<v>.<pid>` comes back, staging and partial downloads go); `installed_pins` (current/previous/running pins for the owned-copy prune rule) |
| `release_update.py` | `claude-multi update` and the card's U | one journey: channel (Nix/source get instructions, exit 1), the running launcher is `install/current`, ownership (`install_txn`), the packaged release key, `HttpsTransport` (https only, release host plus its download host, size caps, `tls` trust through `environ`; proxy selection and bypass from the process environment via `getproxies_environment()`, lowercase including empty overrides wins, no gateway proxy or OS proxy discovery, no direct retry; connection and CONNECT failures, `HTTPException` included, have fixed credential-free text) or `--from-dir`; equal version = up to date (never planned); y/N or `--yes`; refused inside a Claude Code session; exits 0/1/2/3/130; the run's inhibition (owner `updater`, `_transaction`: ended on success or when nothing changed, kept by an interruption from `replace` on — `self_update.Interrupted`, exit 1, or 130 when cancelled) and its recovery (`_recover`: the next update or rollback takes it back under the install lock, `tidy` and `finish_switch`, ends it, then carries on); a rollback passes the installation it showed as `confirmed`; `copies_pruner` (the use-lock prune for the incoming contract); `pin_acquirer` (the incoming contract's pin through `acquire`, recorded for the incoming release); `doctor_lines`/`card_hint` (offline release age, trust source) |
| `install_txn.py` | the installer's and updater's safety transaction | strict channel-marker ownership (`installs.read_marker` rules; `--migrate-from-nix` accepts only a valid foreign marker), `claim` (the runtime's marker format, inside the root's writer fence for the inhibition's owner), state format (forward only), the gateway inhibition (`gateway_inhibition`, no fallback: `guard` refuses any record the caller does not own — an earlier release's `service-handoff.json` and an unreadable one included; `begin` (owner `installer`/`updater`, owner-process expiry, never a silent take-over), `Inhibition.advance`/`fenced`/`end`/`restorable` (phases up to `pin` change nothing the installation runs from), `recover` (explicit, under the install lock)), `protected_versions` (the running gateway's version from its start record; unknown = None), `hold_reason`, `prune_copies` (`retention.prune_copies` for the current release); a `main` that `install.sh` runs through the bundle's own interpreter, which also runs `claude_multi.gateway_inhibition`'s command for the installer's record |
| `install_receipt.py` | `install/installer.json` | format 2: launchers `{path, sha256}` and PATH lines `{file, marker, line}`; strict `read` (a hash-less format 1 vouches for nothing), `Launcher.owned`, `PathLine.present`, `record` (the installer's writer) — the one schema the uninstall command reads |
| `tls.py` | certificate trust of the launcher's own HTTPS | `SSL_CERT_FILE`/`SSL_CERT_DIR` (`configured()`, which the supervised gateway's unit also carries), else the defaults, else a system bundle at a well-known path; never unverified; `source()` is doctor's line (`show=` formats each value: doctor passes `paths.display`, so a relative one is shown from the working directory, home-relative) |
| `identity.py` | the `--version` line | the version, catalog, gateway (with its patch count) and Claude Code versions from the packaged resources; printed by the entry points for a bare `--version` |
| `upgrade.py` | evidence-gated re-pin (`claude-multi-dev repin`; maintainer) | candidates (the PATH `claude` target, then the native versions dirs, version-key ordered, invalid ones skipped with a note); signed manifest (gpg, consented fetch or `--manifest-dir`) before any candidate execution → offline inspect → contract v2 with every signed platform build + version-pin sync (boundary-anchored; `promoted_catalog_version` bumps `catalog_version` only when the checkout equals the running release, keeps a checkout already ahead, refuses one behind) → suite → receipt sha256; crash-atomic `_write_repo_file` for promotion AND restore; no operator override; never builds or installs (the pin ships with the next release); FileLock-serialized under the state root; `[N/M]` progress + heartbeat; `ESSENTIAL_EVIDENCE_PREFIXES`: the four delegation/compaction completion lines keep indices 0–3, `per-seed delegation outcome: ran ` is index 4, then the pinned-client checks' and class-pinned probes' PASS lines (`client check S<n>: PASS `, `client probe <ID>: PASS class=<class> `; the feedback-drafts line also accepts its named `ESSENTIAL_EVIDENCE_ALTERNATIVES` class), each listed in the tuple — a BOUNDARY skip of any of them is missing evidence and `repin` refuses |
| `tui.py` | curses widget layer + the profile editor stack | the **only** product module that imports `curses`/`termios` (the developer harness `probe.py` also imports `termios`), inside a guarded block: `curses_available()`, `CursesError`, lazy `_key_kinds()`/`_pair_fg()`; without curses `streams_curses_capable` is False and every entry degrades to line mode (`import claude_multi.cli` works with curses blocked); `run_curses_on_streams` calls `set_escdelay(ESC_DELAY_MS)` (25 ms) unless `ESCDELAY` is set and clears `IXON` for the curses session only (snapshot after the dup2 block, restored in `finally`), so `^S` saves; every external string through `visible_text`/`safe_add`; `read_key` does not re-merge Alt+chords (ncurses splits them by design) and reads a colour-query (OSC 10/11) reply as one sequence, also when the query's wait ended inside it and across further pauses before its terminator (`_finish_carried_reply`; no byte of a reply is ever delivered as a key, a key that cannot belong to it is delivered alone, an Esc inside it that no backslash follows is delivered as Esc before the key after it, and a reply that did not move for `OSC_CARRY_SECONDS` stops being waited for), so its terminator never arrives as Esc; Esc is the only exit key (SelectList has no `q`; header/disabled rows are skipped); uniform col-2 margin; KeyBar wraps upward (≤2 rows) and past that compacts middle bindings behind an ellipsis — the exit binding is unclippable, and `?` sits right before `Esc`; screens reserve `KeyBar.rows(width)` above the bar and draw `TOO_SMALL`/`TOO_SMALL_HINT` below their computed `min_size`; `Table` (gap, row roles, spans, cursor, scrolling `start`); SelectList multi-mode tracks toggles internally (Enter returns the sorted set, Esc None); `WORKFLOW_GUARANTEE_PANEL`; **profile editor:** `ProfileEditorState` (pure; `profile.evaluate` after every mutation; ◆ only for a `compaction_percent` override), `ProfileEditorScreen` (`^S`/`^O` save, `^G` raw JSON in `$VISUAL`/`$EDITOR`, dirty Esc asks), `BindingPicker` (lines the slot admits, efforts ← →), `RoutingPreviewScreen`, `NamedBindingsScreen`, `run_profile_editor(on_start=…)`; the stack imports `catalog` (constants and `CatalogError` only), `profile`, `settings`, `strict_json`, `views` (never `cli`/`lineup`/`sessions`/`launch`) and writes nothing — store writes are `ProfileEditorCallbacks` from cli |
| `views.py` | pure view builders for the TUI | every row, label and sentence a screen shows, built from data: no curses, no I/O, no store writes; imports only `catalog`, `compiler`, `profile`, `quota`, `scope`, `sessions`, `settings` (never `cli`/`tui`/`launch`/`lineup`/`curses`; pinned by `test_screen_static`). `line_rows(lcat, eff, *, custom_ids)` is the one merged line view every line-listing screen uses (membership in `scope.line_view`, `source` catalog/custom, New and disabled included; never `Catalog.lines`/`docs["models"]`); `effective_context` = exactly `Runtime.prepare`'s `compile_fence` + `lead_set_context` (the one context resolver); `role_window_rows` = each role's effective window (`profile.lineup_windows`) for display (`--print-launch` lists them with the compaction env); the card's context row is the lead's window and names the agents when they share it, otherwise the agent table gains a `window` column (`CARD_WINDOW_TABLE_COLUMNS`), and `workflow_window` adds the workflow default's row; the Settings ceiling row (`SETTINGS_CEILING_KEY`, value and source, edited in `choices.json`); `review_sentence`, `native_summary`, `used_by`, `ages`, `project_text`, `short_label` (first ` · ` segment); the card (`card_model`/`card_fit` drop-priority fit that always keeps Status + the first error/`card_text`, whose `line_mode=True` form is the line confirm body; notice rows carry `short_forms` and `fit_text` cuts a row's middle, never the remedy after ` — `), `editor_rows`, `picker_rows`, `routing_rows`, `propagation_rows`, `session_rows`/`session_actions`, `lineup_dialog_model`, `models_model`, `direct_rows`/`direct_detail`, `provider_rows` (the doctor credential fact), `settings_rows`; `ScreenFloor`; `COUNT_TOKENS_UPSTREAM_ADAPTERS` (the context-meter note). Text from state is returned raw; screens sanitise it when drawing. The 80-column fixture goldens `tests/goldens/tui/*` are rendered from these builders |
| `catalog.py` | trusted JSON load + validate | closed schemas; `version.json` single source of version; per-document data versions (`models` 2, `roles` 2, seed `profiles/*` 2, everything else 1), gated **before** the schema so a v1 models file says "this launcher reads models v2, catalog 33+"; **models v2**: `generation`, `selector` (client-effort adapters: Anthropic OAuth, OpenAI-compat) or per-effort `{selector, proxy_contract}` map (every other adapter, `effort_mode`), `efforts`, `default_effort`, `roles` (`all` or non-lead role list), `status` (`new`/`active`), `registry_overlay` (null, or the closed marker `{"channel": "claude"|"codex"}` on an OAuth-pool line whose `transport.pool` is that channel: the wire becomes an `oauth-extra-models` registration whose capabilities come from the entry's own fields; operator lines never carry it, theirs is derived); **retired map** `catalog/retired.json` (`successor`\|null, `reason` ≤256, `since_catalog`, `provider`, `last_wire`, `display`, `context_tokens`, `capabilities`, `roles`, `selectors` → recorded contract, and on an aggregator provider the model's `family`, required like a line's (the owner its retained aliases report); `<line>@<generation>` keys for same-key generation moves) validated for cycles/≤8 hops, active successor admitting the same roles, declared contracts, selector-base uniqueness and live/route consistency; **no v1 view**: `docs["models"] is docs["models-v2"]` and `docs["roles"] is docs["roles-v2"]` (the raw v2 documents; the `-v2` keys stay as aliases), `Catalog.lines` = raw v2 incl. New, `Catalog.roles` is an alias of `Catalog.roles_v2`, and there is no `Catalog.models` or `validate_composition` (`tests/test_no_views.py` pins that and the five v1-shape readers that remain: `dev.v1_draft_entry_to_v2`, `migrate._agent_binding`, `migrate.convert_record`, `sessions.reconcile_runtime_record`, `cli._v3_normalize`); the bundle hash covers raw v2 + retired; `Catalog.retired`, `resolve_key` → `KeyResolution` (successor chain + notice; null chain = "needs a model choice"; unknown raises), `resolve_selector`/`retired_selector_index` (selector base → retired entry); routes are `providers.json` data: passthrough routes only on the Anthropic pool, every Anthropic line wire a route, `REFUSAL_FALLBACK_WIRES = ("claude-opus-4-8", "claude-opus-5")` (the pinned client falls back to them on a refusal) must be routes; `agent_efforts`/`lead_efforts` (native contract; agent never `ultracode`, `_check_effort_contract`); `remove_legacy_contract_override` never raises; **pinned registry**: `registry_dir(environ, asset_root=None)` (`CLAUDE_MULTI_REGISTRY_DIR` override, else `registry/` under the selected resources — the explicit root, `CLAUDE_MULTI_ASSETS`, then the packaged snapshot — else None), `load_pinned_registry` → `PinnedRegistry` (ids per section + codex `visibility`/`upgrade`/`retirement_at` via `codex_upgrade_fields`, the data doctor's retirement radar reads), `registry_section` (an OAuth-pool provider → its account pool's `registry.section`: `claude`, or the codex plan tier `codex-pro`; non-pool → None), `registry_absent_wires` (every OAuth-pool line incl. New) — evidence only, never a fence/context/render input; **roles v2:** `catalog/roles.json` data v2 (`ROLES_DATA_VERSION`, gated before the schema) = `cm-lead` + nine agent ids (`AGENT_ROLE_IDS`: explorer, analyst{,-strong}, implementer{-light,,-strong}, reviewer{,-strong}, designer), each `function`, `grade` (`light`/`plain`/`strong`), `prompt_file` (one per function: `prompts/cm-<function>.md`), `isolation` (`worktree` iff implementer), `disallowed_tools` (`READ_ONLY_TOOLS` Edit/Write/NotebookEdit/Agent/Skill iff explorer or reviewer), `description` (no model/provider/family name, checked through `identity_tokens`), `requires` (a non-plain grade requires its plain id); the role and prompt rules (ids of one function share byte-identical bodies, functions differ); `prompt_bodies`/`bundle["prompts"]` stay keyed by role id; `Catalog.roles_v2` = raw v2 (hashed); the scope compile reads `disallowedTools` from it; **seeds:** `catalog/profiles/{balanced,quality,max,economy,claude,openai,direct}.json` (`SEED_PROFILE_NAMES`, `DEFAULT_SEED` `balanced`) are required documents (schema `profile`, data v2), validated for name, seed marker, no named bindings, `profile.evaluate` with `effective=None`, secret scan; an evaluation crash on an invalid catalog is reported, never raised; and bundled; any other `profiles/*.json` is a load error; the catalog carries no composition (`load_raw` never reads `catalog/compositions/`), `Catalog.seed_profiles`; `resolve_key_in(lines, retired, key)` is the pure form of `Catalog.resolve_key` (profile.py uses it) |
| `render.py` | gateway YAML | `build_config_document`/`render_config` return the **finalized** document: a last keyless `openai-compatibility` item `claude-multi-render` (base-url `http://127.0.0.1:9/v1`, never dialed) whose alias `claude-multi-render-<sha8>` hashes the sentinel-less emit (api-keys included) — the reload proof (`RenderResult.sentinel`, `document_sentinel`); `RenderResult.oauth_aliases` (`rendered_oauth_aliases`) are the OAuth-pool aliases start-mode readiness needs; never finalize twice; `SENTINEL_PREFIX` is reserved (catalog validator) and excluded from `rendered_selectors`/`provider_selectors`; `gateway_tokens=` takes the ordered key slots; secrets resolve only at runtime into mode-0600 artifacts; `rendered_selectors`/`provider_selectors` are the served-set authority (aliases only — wire names are never served); `ADAPTER_PAYLOAD_CONTRACTS` pins per-alias gateway params (`output_config.effort` low/medium/high/xhigh/max for the claude protocol, codex `reasoning.effort` low/medium/high/xhigh — no codex max, `filter-thinking`); `build_config_document`/`render_config`/`provider_selectors` read the **v2** line map (catalog + custom synthetic, `status: new` included) plus a **required** `continuity=` alias map (`{}` for none); one per-provider plan (`_provider_alias_plan`) feeds the document and `provider_selectors`; catalog aliases and route names (`catalog_alias_set`) win every collision silently; `owned-by` is the provider's family, or on an aggregator the line's own (`alias_owner`), for a retained alias the `family` its retired entry declares (`retained_families`, `build_config_document(retired=)`: every production render — the proxy's, its preflight and doctor's drift compare — passes the catalog's retired map; only while the persisted entry still has that entry's provider and wire); continuity on an unconfigured provider is dropped with a notice; an undeclared continuity effort rule keeps the alias without an override (notice, never `RenderError`); filter contracts cover continuity aliases; a zero-model direct/compat section is never emitted (`models: []` would serve the whole embedded registry); `build_config_document` returns a 4-tuple (+ `continuity_rendered`, `notices`); the static block carries `discovery: {enabled: false}` (mDNS LAN advertisement pinned off; `gateway.schema.json` admits only `false`) |
| `custom.py` | custom providers/models registry (legacy `custom.json`) | `custom.json` 0600 schema-validated; provider ids reserved for the render sentinel are refused at add time and dropped (with their models) by the merge; synthetic entries are v2 client-effort (`efforts ["high"]`, `roles []`, `family: custom`, never schema-validated) merged into both `models-v2` and the view; every collision set (`merge_conflicts`, the merge filter, `add_model`) is all v2 lines incl. New + retired keys + `@` bases (`catalog_line_ids`, `retired_model_ids`) — a custom id never shadows a catalog line; `merge_docs` feeds `Runtime.ordinary_docs` (the merged view `scope.line_view`: lineup compile and fence) and the gateway render — never the trusted catalog |
| `proxy.py` | gateway process control | **Entry checks** (`_entry_refusal`, in `main` before any command; the program runs outside the launcher's dispatcher): every state-changing command checks, for the requested root and the managed root, the channel marker (read, never claimed) and the inhibition inside both roots' writer fences, held until the command ends or execs (the owner's token from the environment; a plain `snapshot-auth` copy reads only) and, for the commands that render, run or sign in, that the home is set up; an interrupt (Ctrl-C) prints one cancellation line and exits 130 (`CANCELLED_EXIT`), the fence released like on every exit; reviewed exceptions: `status`/help/version (read only) and the unit's own directives `UNIT_DIRECTIVES` (channel and inhibition only: they too refuse in a home that is not set up) (`init --prepare-start`, `init --reload-check`, `run --prepared`: the manager runs what a fenced claude-multi path or the transaction owner asked it to). `rotate_token` and `prune_aliases` refuse in a home that is not set up. **Management keys:** `init`/unprepared `run` may stage management keys; `rotate-management-key` stages/re-enables, `disable-management-key` durably disables reads and prints a restart (no auto-restart). Only `init --prepare-start` selects the key at a verified stopped boundary (`ExecStartPre=+`): it holds the single-instance lock while it prepares (`_try_instance_lock`, bounded), and the stopped proof is that lock plus an absent listener plus the recorded process gone — the manager's `MainPID` alone never authorizes the promotion; `run --prepared` privately reads and injects after inherited-env scrub, never in logins/argv/render/session env. Ordinary init/reload/start-check never promote; optional key problems preserve existing readiness exits/stamps. Exec signature includes the management injection policy. Five failed logins on allowlisted routes can ban loopback for 30 min; only refused routes and guard-refused browser/Host requests avoid strikes. Generation rollback needs confirmed process replacement, not merely reload. Loopback only; token file 0600; **working directory:** `run` and the logins create the gateway working directory `<state root>/gateway` (0700, with `logs/`; the unit's `WorkingDirectory`, the entrypoint `chdir`s into it) and refuse to exec while a `.env` exists there (never opened; CLIProxyAPI would load it after the env scrub); doctor names it (`_doctor_gateway_workdir`); **state root:** `init`/`run`/login accept one flag, `--state-root /abs/path` (default: the passed environ's `XDG_STATE_HOME`/`~/.local/state` + `claude-multi` — a relative derived default gets the same usage error as a relative flag; the unit passes `%h/.local/state/claude-multi`), which feeds the continuity record extension; `_render_locked` (caller holds the api-key lock, a leaf) reads `continuity.json`, merges the seed, extends from records, writes it **before** `config.yaml` only when changed, and returns `(target, result, ContinuityReport)` — a corrupt file is never touched and the render serves exactly `seed_only`; `run`/login print continuity notices to stderr and never fail on them; `prune_aliases` takes the `token-rotation` lock non-blocking (a rotation in progress refuses), then the api-key lock around read → plan → write → re-render, refuses on unreadable records or an unlistable sessions dir, never modifies records, and verifies the reload after release; `rotate_token(…, state_root=)`; `gateway_api_keys` = the key-slot authority (`previous-key` + `api-key`, owner-only via `read_private` like launch's reader, symlink/shape-refusing) shared by render and doctor; `init` verifies the hot reload (`await_sentinel`, ≤2 s, injectable `models_get`/`clock`/`sleep` — tests must inject them or they would poll :8317) and prints the restart command only when it did not apply; `init --start-check` (`await_sentinel(mode="start")`) is the explicit (re)start readiness check — up to `START_READINESS_TIMEOUT` (45 s: the sixth patch's 30 s start-up gate plus a margin, no 2 s clamp), connection refused retried, ready = the sentinel **and** one of `RenderResult.oauth_aliases` served (render output only, never credential files; the sentinel alone when the render has none), else exit 6 naming the missing piece; the unit never runs it (ExecStartPre's `init --prepare-start`, which has no readiness check, keeps the informational `down`, and a start that cannot register an OAuth pool must not fail the service); `rotate_token` = crash-reentrant dual-key rotation (publish `[old,new]` → sentinel + new=200 → switch `api-key` → one helper TTL (300 s) + margin → confirm no session that may hold an environment token is live → `[new]` → sentinel + old=401/new=200 → remove `previous-key`; nonblocking `token-rotation` lock; the api-key lock is never held across network/waits/prompts); `set_secret_value` = parse-preserving 0600 masked-entry writer; **gateway env scrub**: `cmd_run`/`cmd_login` scrub inherited values with `gateway_environment(environ)` (only `cmd_run` then injects its selected file-derived `MANAGEMENT_PASSWORD`) — `GATEWAY_ENV_DENY_NAMES` (`MANAGEMENT_PASSWORD`, `HOME_JWT`, `DEPLOY`, `WRITABLE_PATH`, `MANAGEMENT_STATIC_PATH`, `GITHUB_TOKEN`, `META_MINT_URL`) and `GATEWAY_ENV_DENY_PREFIXES` (`PGSTORE_`, `GITSTORE_`, `OBJECTSTORE_`) removed case-insensitively, the dropped NAMES on stderr (never values); `list_provider_models` = explicit-invocation provider listing driven by `_LISTING_SUPPORT` per-provider descriptors (frozen; status verified/attempt/unsupported, url/auth/shape overrides — `url` may use `{base}`, the configured base_url; `llm-local` = `{base}/models`, `auth: none`, `shape: openai`, attempt (no `/v1/v1`); a non-public listing on a transport without an `env:` `secret_ref` is a `ProxyError`, never a `KeyError`; `created`/`created_at` kept as `created`; `auth: none` = keyless endpoint (public or LAN — `listing_is_public` means keyless), no secret resolved; `auth: bearer` = provider secret as Bearer on a different surface; `shape: openai` parses name/context_length/top_provider.max_completion_tokens/reasoning.supported_efforts; `auth: pool-credential` = `list_codex_plan_models`: one codex credential from the gateway auth dir, 0600/symlink-refusing, an expired token refuses — never refreshed or written — returns `{id, visibility, upgrade, retirement_at}` per model (never instruction text), callers must confirm per call); `snapshot_auth_dir`/`restore_auth_dir` (`claude-multi-proxy snapshot-auth [--restore <file>]`): 0700/0600 staged copy, never overwrites, names only, restore refuses unless the unit is `inactive`/`failed` and moves the current dir aside |
| `continuity.py` | gateway continuity aliases | `~/.config/claude-multi/continuity.json` (HOME-relative like `api-key`, never `XDG_CONFIG_HOME`; 0600; schema + key checks; written only under the api-key lock); lifecycle = seed watermark + tombstones (`seed_entries`/`seed_only`/`merge_seed` seed each retired selector once per catalog version, never a tombstoned one; `extend_from_records` adds retired selectors live records name and revives a tombstone only for a live record; a selector known nowhere is reported `unservable`, never stored); entries leave only through `apply_prune` (tombstoned); `scan_records` is read-only and lock-free, never raises: only read/parse/non-object failures are unreadable (stem8 notice), every selector lookup optional (ordinary records contribute `observed_model` + scope `availableModels`/`model`), a missing `last_event_source` counts live; `plan_prune` keeps `REFUSAL_FALLBACK_WIRES` and live-referenced aliases (a named live one refuses the whole run); `read` maps every parse failure (incl. `RecursionError`) to `ContinuityError`; never imports `proxy` or `sessions` |
| `settings.py` | operator settings | `<config root>/settings.json` (XDG-aware like `profiles/` and `custom.json`) = `providers.<id>.enabled` + `admitted_lines`; **never a catalog document** (not in `load_raw`, `SCHEMA_NAMES` or the bundle hash; render/proxy never read it — a disabled provider stays rendered, fence-only); `SettingsStore` (no side effects until `save`; 0600; refuses unknown providers/lines on save, tolerates and reports them on load; `admit_line` only for a `status: new` line; `update` under a FileLock; refuses under a newer state marker); `effective` → `Effective`, `line_offered` (provider enabled and route usable, independent of admission), `snapshot`, `drift`; `COMPACTION_PERCENT_*` constants for profiles and scopes; read by the fence, the record snapshot and the screens; **policy fields** (schema + fixture mirror): `compaction_percent` (60–95, default 90), `explore_inherit_cap_disabled` (default true), `review_round_cap` (1–3, default 2), `workflow_default_binding` (`{model, effort}` or null) — `Effective` carries them and stays Settings-level; `compaction_percent_for(eff, settings_overrides)` is the one rule that applies a profile override (`OVERRIDABLE`, shared with `profile.OVERRIDABLE_SETTINGS`); `Effective.window_ceiling` (default `WINDOW_CEILING_DEFAULT`, 800K; bounds `WINDOW_CEILING_MIN`/`_MAX`) is the session window's ceiling, in memory only — `Runtime.current_effective` sets it from `choices.json` (an unreadable file refuses: CLIError, doctor BLOCK; `Runtime.settings_effective` is the same without it), `snapshot`/`drift` ignore it and `effective_from_snapshot(window_ceiling=)` takes a record's launch window; `snapshot`/`effective_from_snapshot` (strict inverse, for record-authority compiles)/`drift` cover them; `SettingsStore.save` refuses a workflow binding that does not resolve, lacks a representable selector/effort mapping, or names a non-default effort on a client-effort line; capability and role declarations are recommendations; a stale on-disk binding is tolerated by `update` |
| `dev.py` | draft→check→review→promote | promotes only models/providers; dummy secrets in checks; pretty post-images; scaffold (`model add --like`) reads raw v2 and derives selectors by documented shape only (never `str.replace`), refuses retired keys/selectors and Anthropic like-lines (a new Anthropic generation needs a route edit); promote always lands `status: "new"` (New · not admitted) and `_check_new_entry_policy` enforces the catalog-33 selector shape (Anthropic canonical `wire[1m]`, OpenAI-compat `claude-multi-<key>[1m]`, gateway-effort `<prefix><key>-<effort>[1m]` with contract level == effort); draft/review data v2 (a v1 draft points at `drafts migrate`; the journal stays v1); `claude-multi-dev drafts migrate [--apply]` — dry-run default, `--apply` renames into `drafts/archive-2x/` (0700) and converts v1 model drafts, never deletes, refuses under a newer marker; the check render uses v2 lines + `continuity={}`; `check` runs the pinned-registry presence rule (`registry_presence` → `CheckResult.registry`, printed): every OAuth-pool line (New included) whose wire is absent from its registry section is **reported, never refused** (the overlay trigger; the registry is the pre-patch source, so a patch-added wire still reports; no package registry → one "skipped" line); `promote`/`--patch-output` return and print `gateway_effort_moves` notes, historical same-alias reload vs renamed-alias continuity-until-resume, for every gateway-effort line whose `wire_model`/`generation` differs between the **installed** catalog (`running_catalog_lines`, the package's own) and the post-image (print-only: promote has no apply step to ask at); `_check_new_entry_policy`'s legacy default-composition checks run only while a legacy `catalog/compositions/default.json` exists; the seed checks always run — a New model bound in any seed, or a New provider bound, named in `lead_providers` or as `primary_provider`, is refused (New · Off) |
| `errors.py` | `ClaudeMultiError` root | operator-facing errors share one catch; internal programming errors still raise |
| `service.py` | gateway service seam | the one remedy seam `hint(verb, home=|backend=)`: control verbs always name `claude-multi gateway <command>` (guarded), manager text only on the systemd backend (`backend_of(home)` from endpoint.json); `failure_notice(unit)` is the unit's own text; journal argv; PID/health, listener ownership before authentication, exec/reload stamps and pending restart; every live read has an injected test seam. Shared `owner_policy`: foreign uid refuses before sending either token, same-uid outside MainPID or unknown ownership emits Attention and proceeds; launch and management share it |
| `management.py` | restart-bound key slots and minimized local client | channel policy: `allowlist_build` needs `CLAUDE_MULTI_CHANNEL=nix` (the Nix wrappers set it) plus the pinned binary and the attested allowlist patch; any other channel or none keeps management off whatever the gateway's backend (doctor and `claude-multi-proxy status` say so by channel, never by backend), and `pool_status` then returns `unavailable` without reading keys or the gateway; the spawned gateway never carries `CLAUDE_MULTI_PROXY_PATCHES`. HOME-relative `management-key`, `.next`, `.disabled`, `.prepared` (private selection digest), leaf `.lock`; marker wins. `ensure`/rotation stage; only `init --prepare-start` promotes after listener and PID absence is proven, publishing selection last. Disable writes marker first; missing/deleted active or re-enable stays unreadable until selection. `run --prepared` reads/injects only: no writes/chmod/locks. Wrapper attests a pinned binary and allowlist manifest; standalone clears inherited attestations. Client accepts only numeric loopback HTTP, shared owner policy before the key is sent, one `http.client` GET to `/v0/management/auth-files` via `X-Management-Key`, 1.5 s, 1 MiB / 256-entry caps, no proxy env, redirects or retries; unsafe repairs and exceptions are value-free |
| `quota.py` | pure minimized quota facts, classification and text | stdlib + `errors`, `strict_json`; no I/O. New objects retain only allowed credential fields, closed reason from raw status_message, anonymous pool handles and recognized Claude/codex windows. ≥90% current warns; expired windows reset; observation age/unknown reset/no-window exhaustion explicit, never extrapolated. Runtime caches every result 60 s, no persistence/polling; injected health/models seams suppress live management unless explicitly injected. `quota` disables state cleanup/shim refresh; doctor never quota-BLOCKs or suppresses healthy-pool journal failures; Providers details carry reset/age; card uses actual lineup pools with fresh P / resume S→T guidance. `condition()` types every observation (available, stale, exhausted, management-disabled, unavailable, not-ours); `command_lines` prints its `condition_line` on every surface (`quota`, `/cm quota`, the Providers and card details) and returns `CONDITION_EXIT` (1 exactly when no reading was made; `/cm quota` always 0) |
| `paths.py` | HOME/XDG paths and display | the authority for `state_root`/`config_root` (`sessions` keeps its wrappers and call sites); gateway config stays HOME-relative; operator config/data respect XDG; the native-projects and Claude-settings directories are two separate accessors; no side effects |
| `retention.py` | retained and owned copies | `pinned-clients/` copies are never pruned (rollback material; `verify_retained`, `same_file` for hooks/doctor); owned-copy prune rule (`prune_plan`/`prune_copies`; `prune` returns the removed names): keep the current pin, the previous release's pin, the running gateway's release pin, every version whose **use lock** is held (`use_state`, a momentary non-blocking probe that creates nothing; an unsafe lock file or version directory keeps its version too) and every version a readable process runs, in that order of reasons (a pin keeps its pin reason while in use; the lock outranks the process evidence). The use lock (`pin.take_use_lock`: `<owned root>/<version>/.in-use`, 0600 in the 0700 version directory, opened `O_NOFOLLOW`; a symlinked, foreign-owned, hard-linked or non-regular file refuses with the setup remedy) is the authority for what claude-multi runs: every launch (`launch.PinnedCopy` in `perform_launch` — fresh, resume, relaunch — and `sessions stop`) takes it shared before the hash check, and the launch's descriptor is inheritable, so the exec'd Claude Code (and its children) hold it for its whole lifetime; every probe run of the owned copy (`probe.run_native`/`run_native_pty`: developer probes, evidence suites and the qualification exact-client check, which also holds it across its runs) takes it before the hash check too, in the owned root `trusted_from_contract` recorded (`TrustedExecutable.owned_root`, so a run in a fixture environment still locks it; `pin.take_use_lock_in`), and hands it to the client (`pass_fds`, through bwrap and the namespace wrapper's exec); a version directory that appears during the check (a retained copy copied into place) is locked and checked again. The process scan is supplementary (`scan_processes`: readable `/proc/<pid>/exe` links on Linux, one bounded `ps -axww -o comm=` read on macOS through `platform.darwin_process`, `_platform` is the test seam): an unreadable process is no evidence either way and blocks nothing; a copy started outside claude-multi by a process that cannot be inspected holds no lock (documented residual). Unknown facts still remove nothing (a process table that cannot be listed or read, an unreadable or invalid gateway start record). `prune_copies` takes each candidate's `acquire.version_lock` and use lock exclusively without waiting (creating the lock file, so no launch can start in between), keeps and reports as `in_use` every version it cannot lock or the fresh plan keeps running, re-reads the facts under those locks, deletes with the locks held — every other file first, the use lock file last, so a launch that opens a new lock file finds no copy to verify — and releases the acquisition lock first; a launch waiting on the use lock re-opens it when the file was removed and then finds the version gone (not set up, or copied back from a retained copy and locked anew); a version left in place (anything but regular files in it, a file that cannot be removed) is reported as `failed`; setup reports in-use copies (never a pin) and doctor lists them as `(in use)`, a pin in use as `(<pin reason>, in use)`; `retention_report` never hashes |
| `release_manifest.py` | signed release provenance (maintainer) and `platform_key` | `platform_key`: linux (and musl), darwin, win32 in the manifest's spelling; bounded manifest/signature fetch or offline directory; pinned signer; `manifest_platforms` (every build, strict); gpg only in `repin` and `tools/pin_claude.py` — clients never need it |
| `managed.py` | read-only managed policy, settings layers and env | one policy root per OS (`managed_root_for`: `/etc/claude-code`, `/Library/Application Support/ClaudeCode`, `C:\Program Files\ClaudeCode`; files only); `policy_source` judges the policy as the client applies it (each file parsed like `JSON.parse`, a duplicate key's last value wins; the base file and the drop-ins in name order merged: objects by key, arrays joined without repeats, a later scalar wins; `origin` names the last file that set a key); a policy file that exists but cannot be used is `unreadable` (Attention) or, over 1 MiB, `unchecked` (BLOCK), never "no restrictions"; `policy_findings` (names only, never values): version bounds excluding the pin, `allowedProviders` without the gateway as a pinned `customEndpoint` (anywhere in the merged policy), `apiKeyHelper`, `forceLoginOrgUUID`, hooks off, a denied bound model (`deniedModels`, `availableModelsMatch`) block the launch (`Runtime.perform`, the card) and doctor; model locks are doctor BLOCK and a launch stderr line; the rest is Attention; `skew_layers`/`unknown_settings_keys` (user settings keys the pin does not know); never edits policy |
| `client_check.py` | which Claude Code runs a managed session (hook side) | the nearest ancestor whose argv carries `--session-id`/`--resume <id>` (Linux `/proc/<pid>/exe`, macOS `ps`); a version other than the pin → the SessionStart/prompt hooks add a `systemMessage` (exit and `claude-multi -r <id>`) and write no `.seen` marker, so every prompt re-checks; unknown is silent; never raises |
| `probe.py` | dev-only loopback harness | never touches live daemon/providers/transcripts; fixture roots only; a run of claude-multi's owned copy holds its use lock (`retention.py`) |
| `surface_matrix.py` | the object × operation census (tests read it; the docs take `document()`) | effect-free data: `OPERATIONS` (`Op`: id, object, summary, audience, CLI spellings, proxy commands, `/cm` verbs, TUI paths each with the test that drives its key, or an absence reason per surface; `destructive`/`confirm`/`stream`/`exit`, and `reversible`: why a change one command undoes is not destructive, never on a destructive row), `REQUIRED` (the operations that must have a row whatever the sources say), `MODIFIER_FLAGS`, `OPTION_ARGUMENTS` (the value-taking options that only supply an input of their command's operation), `TUI_NAVIGATION`; `census(...)` takes the inventories (argparse leaves and options, `PROXY_COMMANDS`, `CM_VERBS`, the TUI `ACTIONS`) and returns every problem: a command, option, proxy command, `/cm` verb or TUI action without a row, a row without a surface or a reason, a reason that leans on later work, a destructive row without its confirmation, a stream that disagrees with `cli/streams.py`, a TUI fixture that does not exist, never drives its action's screen (`SCREEN_ENTRY_POINTS` in the test), never feeds the action's key to the input driver or asserts nothing after that press. `tests/test_surface_matrix.py` fails until a new surface has its row |
| `observations.py` | the report vocabulary (`Fact`, `Diagnostic`, `Report`) | effect-free; scalar values only, so no raw response, exception or log line can be serialized; collectors own minimization |
| `routing.py` | pure routing explanation | record intent, compiled fence, lineup-log binding, current route and journal observation stay separate layers; journal matches are candidate-only; the text uses the lineup's layer labels and names the explained role |
| `usage.py` | observed client HTTP request counts | not tokens, billing or upstream attempts; unknown attribution and partial coverage stay explicit, never zero |
| `served_plan.py` | the shared served-change plan | pure; published render vs candidate document, route identities without key values, live impact from the record/fence scan; a destructive plan needs complete coverage; the digest binds inputs and diff; the state-root boundary (`root_refusal`) |
| `portability.py` | operator-state export document, import planner, export receipt | a semantic projection, never a file archive or a secret; imported route approvals and admissions are inert re-approval requests (no source-host trust) |
| `cli/commands/explain.py`, `usage.py`, `plan.py`, `portability.py` | the report and portability command adapters | `explain`, `usage` and `plan` are read-only (report snapshot, no store creation or shim refresh); `plan` also owns the installer-facing application boundary (`application_refusal`, `plan_application`); `export`/`import --apply` run the guarded command service inside one served-change phase |

### 3.1 The `claude_multi.cli` package

The dependency direction is **entry → commands and launch flows → screens
or doctor/actions → runtime, facts and actions → domain modules → leaves**.

- **One owner per name.** Each name the package defines lives in exactly
  one module. Code in another module calls it through the owner
  (`launch_sessions.launch_card(...)`, `session_facts._live_background_prefixes()`),
  never a copied binding, so a test patches the owner module and that one
  patch reaches every caller. Add a command as a function in
  `cli/commands/`, a screen in `cli/screens/`, a doctor check in
  `cli/doctor.py`; never grow `dispatch.py` or `entry.py` beyond routing.
- **The lazy facade.** `claude_multi.cli` imports nothing; old names
  resolve lazily through `_COMPAT_EXPORTS`. No module under `cli/` imports
  the facade, and no test patches, assigns or `setattr`s it
  (`test_cli_split.FacadePatchGateTests`, which also scans embedded child
  programs); the compatibility suite is the one exception.
- **The hook path.** `cli.entry` imports only `assets`, `errors`, `hooks`,
  `lineup_files`, `sessions`, `termtext`, `cli.parser`, `cli.streams` and
  `cli.text` (exact static and fresh-process pins, also for `cli.parser`
  and `cli.text`). `main` keeps its order: `lineup` on the raw argv; the
  parse (a protocol-3 or scope-only argv it cannot parse exits 0, a
  `premodel` one prints the fixed deny); the retired-flag refusal; `restore-2x`; the state
  marker; then every `session-event` before any command loads —
  `SCOPE_ONLY_EVENTS` go to `hooks.dispatch` (no Runtime, catalog, shim or
  store setup), the protocol-3 start to `_session_start_v3` with the lazy
  Runtime factory (both ignore passthrough), and every other event builds
  Runtime as every command does (no state writes, no contract notice),
  then refuses passthrough (stderr, exit 1, empty stdout), then runs
  `_handle_session_event`. The 23 hook paths are pinned in fresh processes:
  none loads `tui`, a screen, a command, `dispatch`, `launch_flow` or the
  doctor modules, and the scope-only ones load no Runtime/catalog chain
  (`test_cli_split.HookEntryImportTests`). `main`'s top-level `try` keeps
  `StateMarkerError` before `ClaudeMultiError` (`test_errors`).
- **Seams.** Asset roots go through `assets.py`, the gateway endpoint
  through `endpoint.py`, state/config roots through `paths.py`, and POSIX
  filesystem, service-manager and `/proc` observations through `platform/`
  (Linux only; a missing capability is unknown, never stopped). `fcntl` is
  imported only under `platform/` (and by the dev harness `probe.py`):
  `hooks.py` and `lineup_log.py` lock through `posix_fs.lock_descriptor`
  (`test_hygiene.PosixSeamTests`). The user settings directory the
  managed client reads is `paths.claude_settings_dir` (`$HOME/.claude`, never
  `$CLAUDE_CONFIG_DIR`, which every managed launch unsets): the
  NO_PROXY and permission-mode layers, the settings radar and the
  `--rotate-token` credential gate, the saved-selector report and the
  migrate cleanup days all read it.

### 3.2 The operator layer

A new model or provider arrives without a claude-multi release.
Trust tiers: T0 code (kinds, adapters, contracts, validators, credential
policy), T1 the reviewed catalog, T2 the operator's
`~/.config/claude-multi/providers.d/<id>.json` (HOME-relative, like the
gateway config). T2 **selects** T0/T1 vocabulary and never extends it.

- **Owners.** `operator.py` (no CLI/TUI/network imports): schemas, the safe
  owned-readable loader, `validate_layer` (collisions, the `custom-` key
  and selector namespace, one origin per secret, endpoint/header policy,
  redacted diagnostics), definition and route digests, `merge_docs`
  (origin metadata `catalog | legacy-custom | operator | operator-migrated`
  outside the closed core entry; never infer T2 from `family`), the ledger
  (`operator-ledger.json`: `routes`, `admissions`, `aliases`, `pruned`,
  `removed`, `transport_choices`, the migration manifest; tool-owned, never
  repaired or overwritten when corrupt), `render_plan`, capture and prune
  planning, `migrate-custom`, `displaced_lines`, `doctor_findings`, presets
  and transport descriptors. `secret_store.py`: the `get/is_set/set`
  interface over the existing strict file backend (no process-env
  fallback, logical `env:NAME` references only). `cli/consent.py`: the one
  human guard. `cli/commands/providers.py` and `cli/commands/models.py`:
  the verbs (user docs: `docs/guides/models.md` and the pages under
  `docs/providers/`).
- **Human guard.** Route approval, key import (`set-key`,
  `--secret-file`), `models admit|qualify`, `providers transport`,
  `migrate-custom --apply` and legacy `custom add-*` call
  `consent.require_human` first, before any secret-store read: refused when
  `CLAUDE_MULTI_MANAGED_ID` or `CLAUDECODE` is set or stdin/stdout are not
  terminals (never `/dev/tty`); no flag waives it. The Providers pane `K`
  (and the keyed-N "set key now?" offer) runs `ConnectActions.set_key`:
  the setup layer's `plan_set_key`, the human guard, a confirmation that
  names every provider sharing the key, a masked in-screen key prompt,
  then `apply_set_key`. The real-binary probe
  `test_scope_probe_client.ClaudeCodeMarkerProbe` pins that the pinned client
  exports `CLAUDECODE` to the Bash children of the lead and of a subagent.
- **Credentials in the setup layer** (`setup/providers.py`, `setup/check.py`,
  `setup/signin.py`; the `providers set-key|remove-key|sign-in|sign-out|test`
  verbs and the screens call them). The key file is
  `secret_store.key_file_location` for every reader (`CLAUDE_MULTI_SECRET_ENV`,
  then the validated `secret-file.json` pointer, then
  `~/.config/claude-multi/secrets/provider-keys.env`).
  `served_transaction` writes through `guarded_write`: each write's undo is
  registered before it runs and restores only while the state still shows
  that write, so a failure before the bytes were replaced touches nothing
  and one after (`CommittedStateError`, an interrupt) is put back; the
  original error is re-raised (an undo that did not complete is added as a
  note). Key plans carry `key_file` (source, real path) in their digest, and
  the apply opens the store under the transaction's locks
  (`open_planned_store`; another file refuses as stale); selecting a key
  file is itself a served change (`apply_key_file`), and the shared sample
  (`cli/commands/providers._sample`) covers the key-file identity and the
  keys of reviewed transport alternatives. A transport switch re-checks
  the alternative's key under the lock and saves a typed key before the
  ledger names the transport. `retained_key` (`remove-key ID --name NAME`)
  removes the key a removed provider left, never guessing the name.
  `applied_exit` maps a `token_mismatch` reload to exit 1. The connection
  test binds each target's `authority` (model line, effective route and
  auth, route approval, key name, presence and key file) and the verified
  render sentinel (`models.render_identity`, the admission smoke's check)
  and re-checks both right before each request. A sign-in handles SIGINT
  in the launcher with a no-op Python handler (never `SIG_IGN`, which the
  child would inherit); the port-in-use exit wins over a record changed by a
  refresh. Sign-out moves records with a non-clobbering undo
  (`_put_back`: a hard link, which never replaces a name, then the unlink;
  never a check followed by a rename); without hard links, or with a record
  written there again, the record stays in the backup: a part-way failure
  that cannot put every record back raises `SignOutIncomplete` naming the
  kept backup (which is never removed while it holds a record), and a failed
  re-check undoes its own moves and returns `SignOutOutcome.left` (still
  signed in) and `SignOutOutcome.kept` (left in the backup, named with it).
  `PROTECTED_DIRS` is claude-multi's own folder (`~/.config/claude-multi`);
  the pointer may also select a key file in a private folder the person
  chose (`secret_store.location_problem`: owned, no group or other access,
  not HOME itself; the file, once it exists, private too), and no other
  folder is shipped as a key location. `secret_store.unprotected_key_files`
  lists the override and the pointer's file (`pointer_key_files`, read
  leniently from the pointer), each as given and resolved, when it lies
  outside `PROTECTED_DIRS`; `scope.secret_path_denies` denies it to Read and
  Edit, naming its cause. `secret_store.redact_lines` (and `redact` for
  one text) is the one redactor of the public gateway-log surfaces
  (credential shapes, header secrets, cookies, secret-named fields, URL
  credentials, `auth=`, JSON account fields and e-mail account identifiers;
  shapes and names only, no stored value is read): terminal escape
  sequences and control characters are removed first and only that
  sanitized text comes back; a sensitive value is redacted whole (a quoted
  string at any escape level, a list or an object, nested; a header's bare
  value to the line's end), quoted and bracketed values keep their
  delimiters so a second pass changes nothing, a value that runs past its
  line (a private key, a quoted string, a list, an object) is redacted up to
  its end, and lines that start inside a private key are redacted up to its
  END line (a lone base64 line too). A private key is found on the
  sanitized line before any field rule replaces its BEGIN marker
  (`private_key: -----BEGIN …`), and one with no END is redacted to the
  end of the text; `redact_tail` redacts a tail with the lines read above
  it as context.
- **Account pools** (`account_pools.py`, `account-pools.json`). Every
  pool-specific fact of an account sign-in is data: the sign-in layer
  (`setup/signin.py`: provider ↔ pool, login command, offering policy,
  methods and their default, the header text by method, the
  acknowledgement, record listing and account names), the texts
  (`setup/texts.py` `ACCOUNT_KINDS`, `SIGNIN_HOSTS`), `choices.ACK_POOLS`,
  the credential-record counts and remedies (`cli/gateway_facts.py`), the
  registry mapping (`catalog.registry_section`, `discovery.section_provider`
  and `DEFAULT_SECTIONS`, `operator.static_overlay_wires`, the dev registry
  prefill) and the sign-in screens (`cli/screens/signin.py`: the method
  picker only for a sign-in with more than one method, titled with its
  account kind; the account modal's "separate from your Claude Code login"
  line from the pool's optional `client_account`, true on the Claude pool,
  `signin.client_account`) read the table; `validate_catalog` refuses an
  OAuth-pool provider whose `transport.pool` is not a pool naming it back
  (the providers schema takes any pool name). The two shipped pools keep their
  behaviour (`tests/test_account_pools.py` pins their facts and
  acknowledgement digests, so acknowledgements given earlier stay
  current). An acknowledgement in `choices.json` for a pool this build does
  not know is kept as it is and never counts. A later pool is an entry
  plus its catalog provider and lines and its login command in
  `claude-multi-proxy`; what stays code is gateway-bound: the overlay
  channels (`catalog.OVERLAY_CHANNELS`, patch 16), the OpenAI key route
  (`KEY_ROUTE_POOLS`, `TRANSPORT_ALTERNATIVES`), the codex selector prefix,
  the quota windows (`quota.py`), the journal's `invalid_grant`
  attribution and the codex plan listing. No pool beyond Claude and
  ChatGPT ships; a new one needs acceptance evidence from an account.
- **Setup runs** (`setup/answers.py`, `setup/firstrun.py`,
  `cli/commands/setup.py`). `setup --answers` checks every file the answers
  name before anything is planned (an unusable `api_key_file` or
  `keys_file` is exit 2) and describes a file that does not parse by a
  fixed category only (`operator._json_failure`). A document with an
  entry that needs a provider only another entry of it declares is refused
  before anything is planned (`answers.check_dependencies`, exit 2, the
  remedy names the declaring entry: its own `api_key_file` is the one way to
  give it a key). The one y/N covers a preview made from each item's real
  plan (its writes, a route approval's disclosures, key names, the download
  plan with its size, the starter profile's name, slots and spend notes,
  the default profile); key plans are previewed with the key file the
  answers select, and a starter with the providers connected now plus those
  the document gives a key (`Runtime.starter_plan(assume_keys=)`). Each
  item, run in order as its own transaction, plans again and applies only
  while `answers.reviewed(plan)` (every plan field except the served sample
  and the key file's source) equals what was shown; otherwise it refuses as
  stale with nothing written. The profile and default items write
  `choices.json` only while it is what the preview read or what an earlier
  item of the run wrote (`defaults.apply_default(expected_choices=)`), so a
  default chosen elsewhere meanwhile is kept and the item refuses.
  `setup.providers.select_key_file` is the one key-file selection
  (`--keys-file` and `keys_file`): checked, then one served change whose
  preflight is the render with the selected file's keys
  (`served_preflight(candidate_environ=)`, so a line it stops serving is a
  removal, refused while its live impact is unknown), planned again under
  the transaction's locks before the pointer changes, with a verified
  reload, the override and installed-service lines, exit 1 on
  `token_mismatch`. The providers menu returns 130 at once for a
  cancelled choice and, when left, the last choice's 3 or 1. `doctor
  --first-run` builds a read-only runtime (no shim refresh); its Claude
  check is `external.claude_verified` (the pinned size and sha256; a kept
  copy waiting to be put in place does not count), and a stopped gateway
  passes only when `external.start_problem` finds its program installed
  and executable and its automatic starts not paused.
- **Uninstall** (`setup/uninstall.py`, `cli/commands/uninstall.py`). The
  plan checks every root (`check_root`: a link, a non-folder or an
  unreadable root is left as it is and named; the release root must
  resolve inside HOME and outside the Nix store) and removal reopens each
  file's folder from its checked root with `O_NOFOLLOW` (`_open_folder`;
  the root's device and inode must match the plan). Classification order:
  kept backups, then credentials (`secret_store.key_file_inventory`: the
  override, the service's pointer-or-default file and the default, matched
  by path, resolved path and file identity; plus the gateway keys and
  configuration, `secret-file.json` and the account sign-ins of
  `secret_store.credential_locations`, the one credential inventory the
  compiled denies and `portability.forbidden_roots` use too), then the
  root's class; an unreadable pointer raises `UninstallRefused`.
  `install/previous` and its `versions/<v>` are never removed. The
  installer's receipt is read with `install_receipt.parse` (the writer's
  schema; a hashless earlier format authorizes nothing); launchers carry
  its sha256 and the planned identity, checked again right before the
  unlink, and a PATH edit removes exactly the recorded line.
  `session_check` is the destructive liveness check (background liveness
  with its uncertainty, the process table, a record without `end`;
  unreadable is possibly running). After the confirmation `Fence` takes
  the migration lock exclusively (bounded, `LOCK_WAIT`) and the sessions
  are checked again; `external.service_state` (`absent`/`recorded`/`stray`/
  `unknown`) refuses unknown and stray before the gateway is stopped, and a
  recorded service goes through `GatewayService.uninstall`;
  `external.stop_gateway_for_uninstall` evaluates the persistence hold for
  a stopped gateway too; `external.hold_gateway_starts` then holds the start
  lock (gateway still stopped, no inhibition, no hold) until the files are
  gone. The gateway inhibition (`gateway_inhibition.guard` over
  `home_roots`, never acting for an owner) refuses before the question and
  again under the migration lock, and the lifecycle's `inhibition_refusal`
  under the start lock; a home with no recorded port (`endpoint.set_up`)
  is never observed. Once the start lock is held, `Fence.stores` takes the
  lock of every store whose files go (each lock file the plan removes, and
  the profile, binding, choices, settings, preferences and operator stores'
  locks even before a writer made them), with a short bounded wait, and
  holds them until removal ends: a store still in use refuses before any of
  its files goes. A lock file is unlinked only while this run holds it,
  last (`Fence.remove_own`), and only as the plan classified it: one this
  run made, or one the plan removes whose file is still the one held; a
  credential only after the typed phrase, a never-removed file never (kept
  and named). Everything removal needs
  is imported before the first file goes: the command finishes after the
  release it runs from is gone (`tests/test_uninstall_installed.py`
  installs a real bundle with the real installer and proves it).
- **Render.** Admission never filters the render (New lines' aliases serve);
  an unapproved or changed route is not rendered. Every emitted T2 alias is
  captured in the ledger (with its route digest) before `config.yaml` is
  replaced, under the api-key leaf; a capture never retargets to another
  route. Policies: explicit (`init`, `providers apply`, write verbs) refuses
  an invalid layer; reload (`--reload-check`) degrades an unreferenced
  invalid/unapproved declaration and refuses when a live reference would be
  lost, when emitted aliases cannot be captured (corrupt ledger), or when
  capture history is unknown and a live session uses an unserved `custom-`
  alias; start (`--prepare-start`) degrades. Prepared execution writes
  no policy or config. A `config.yaml` replaced before its directory fsync
  failed is published (`proxy.ConfigPublishedError`, a `CommittedStateError`
  carrying the render): write verbs keep their inputs and say so, never
  "nothing changed". A line key declared in two or more files refuses every
  declaration (a third never reinstates it). A read-only
  target's refusal prints the fragment only after the token-shape and
  stored-value screen; `providers edit`/`models edit` refuse value-free.
  `models rm --successor` re-proves liveness, references and the successor
  under the locks and rewrites only slots still equal to the reviewed ones.
- **Lock order:** migration guard (SH) →
  **served-change barrier** → token-rotation (non-blocking) → store locks
  (operator store, preferences, profiles, bindings, secret file) → Settings →
  api-key leaf. `sessions.served_change_phase` takes migration then the
  barrier **once per commit phase**, with a bounded wait
  (`state.SERVED_BARRIER_TIMEOUT`) that refuses with the neutral
  `state.BARRIER_BUSY_TEXT` ("another gateway operation is in progress"); the
  barrier is not reentrant. Inner services called inside a phase (lineup
  decide/converge, render, subordinate store commits) take the held token and
  only assert it (`state.require_barrier`, `sessions.phase_or_guard`), never
  acquire it. Hooks and the ordinary, unit (`--prepare-start`/`run`), `init`
  and rotation renders stay barrier-free. A command with several commit
  phases (such as lineup apply → relaunch) releases the barrier between them
  and revalidates its CAS sample in each. Exception:
  `doctor --rotate-token`'s dead-confirm path goes rotation → non-blocking SH
  migration → lifecycle through `mark_ended`, never the barrier. The state-root
  authority (`proxy.root_authority_refusal`; an unreadable
  `continuity.json` refuses too) is revalidated inside the phase. Prompts,
  cards, consent and network work happen with no lock held; a long operation
  snapshots, releases, then revalidates under the lock (stale commits refuse).
- **Admission** is a digest-bound local badge (`models admit`: confirmed
  metadata writes only, no inference or qualification). Diagnostics and
  `models qualify` are separate, optional, explicitly requested operations;
  qualification evidence is verdict-only in `<state>/operator-evidence.json`.
  Record-authority compiles keep recorded grants; new or rebound operator
  slots use current provider enablement, route approval and ledger integrity.
  Launch and resume also use these current facts: a corrupt or unreadable
  ledger refuses operator use, while an absent ledger is valid on catalog
  and keyless routes. Staged requested agent roles are accepted; admission
  is an optional attestation (a warning when absent), never a use-time gate.
- **Pool lines.** A `providers.d/anthropic.json` or `openai.json` file
  declares lines only on the catalog OAuth pools (no route to approve).
  Claude-pool lines are client-effort (a list; the canonical wire selector,
  `[1m]` above 200K declared context) so the pinned client applies its
  own model handling; codex-pool lines map each level to a reviewed contract
  (`custom-<key>-<level>`). Each renders through `oauth-extra-models`
  (`operator.overlay_projection`: declared context, optional output limit,
  thinking levels from the efforts on both channels) before admission, under the
  overlay precedence of the OAuth how-to in §5. The exact-client probe XC
  (`test_scope_probe_client.ExactClientProbe`, an essential re-pin class) runs
  the compiled Direct lead and a cm-* agent on an overlay-only claude wire.
- **Presets and samples** (`presets/`, `examples/providers.d/`) ship as
  package data (inside `src/claude_multi/data/`); they are templates, never
  grants, and nothing installs them automatically. A preset is a
  providers.d document (kind, base URL, the key's `env:NAME`, family, the
  model list, no lines) plus a review block `preset`
  (`schemas/preset.schema.json`: the support label `preset`, never more
  without evidence, and `source: docs` with the vendor page and the date
  it was read, or `source: generic` for the server-on-your-network
  template); `operator.preset_document` drops the block, so a declaration
  never carries it, and `operator.presets` lists the presets that read
  (`PresetInfo`). The picker (`setup.providers.picker_entries`) shows each
  preset with a key as its own `preset:<name>` entry after the shipped
  providers (Anthropic-compatible first; an OpenAI-compatible one stays
  unavailable with the closed route's note until
  `catalog.keyed_compat_audited` opens, then it is offered with no other
  change) and each server preset as an `other:lan:<name>` entry; the
  new-provider picker of Providers N (`picker_entries(only="other")`) lists
  the same `preset:<name>` entries before "Other" (Enter on a
  `preset:<name>` entry in either picker, `ConnectActions.preset`: its name
  and its address — the preset's base URL by default, another one passed to
  `plan_add_preset` as the base URL and checked by the same layer
  validation as `providers add --preset --base-url` —, then your own
  endpoint's preview, route approval and masked key, filled in from the
  preset; the setup menu's `_connect_preset` is the line form);
  `plan_add_preset` returns the keyed `EndpointPlan` of your own endpoint
  (route approval, the preset's key name) or the keyless `PresetPlan`.
  A key is never shared or replaced by accident: the plan carries the key
  saved under its name (presence and length) and every provider using that
  name (`key_sharers`: the merged view's providers, so a `custom.json`
  provider counts while `operator.merge_docs` keeps it — never after the
  migration marker, never one the layer dropped). When another provider
  already uses the preset's key name, the new instance gets its own
  (`instance_secret_name`: the preset's name plus the provider id's suffix, checked by
  `secret_name_problem`) unless `key=reuse` shares the saved key (no key
  is typed) or `key=replace` replaces it (the surfaces confirm first,
  naming every sharer; Cancel and N are the defaults); the apply re-checks
  the sharers and the saved key's length under the locks. Get started
  (`ConnectActions._preset_key`), `setup --step providers` and
  `providers add --preset` (`--reuse-key`, `--replace-key`) offer the same
  three choices; a key name another provider uses is refused for your own
  endpoint. `plan_set_key` carries the same sharers (`KeyPlan.shared_with`):
  replacing a saved key other providers use names every provider using it
  first (`providers set-key` asks y/N, default No, `--yes` skips the
  question and still names them; K confirms with Cancel focused; the
  `--answers` preview names them), and the apply re-checks the sharers
  under the locks. `plan_set_key` takes its served sample before any plan
  fact (`served_sample`, passed on as `served_preflight(sample=)`), and
  the apply resolves its provider again under the locks: still there,
  keyed, with the planned key name, or `Stale` with nothing written.
  `tests/test_presets.py` lints every preset (HTTPS for a key, the listing
  on the base's origin, no secret-shaped text, unique ids, displays and key
  names, none shared with a shipped provider) and covers the picker in both
  route states and one preset alone reaching a usable profile.
- **Transport alternatives** (`operator.TRANSPORT_ALTERNATIVES`): a
  reviewed T1 descriptor per (catalog pool provider, choice), with the
  gateway channel it renders into; the ledger stores the choice and its
  approved route. `render_plan` applies it to a render copy only
  (`render.TRANSPORT_ALTERNATIVE_KEY`, which names the channel), and
  either channel sets `oauth-excluded-models: {<pool>: ["*"]}`; an
  unapproved choice is withheld, never a pool fallback (doctor's BLOCK
  names `providers transport <pid> api-key` for an unapproved choice and
  `providers set-key <pid>` for an approved one whose key is not set;
  screens show the key's G → K, and the card's H report says
  `G (providers) → K on <provider>`, a resume card its way back first:
  `launch_sessions._card_doctor_line`). Anthropic API key:
  logical `PLATFORM_ANTHROPIC_API_KEY` (T1-owned), `x-api-key` to
  `https://api.anthropic.com`; the pool provider becomes a keyed
  `claude-api-key` section with the same selectors (passthrough routes
  included), `cloak: never`. OpenAI API key: logical
  `PLATFORM_OPENAI_API_KEY` (T1-owned, reserved even while not offered),
  bearer to `https://api.openai.com/v1` (the codex Responses executor
  appends `/responses`; an empty base would select the account backend):
  one `codex-api-key` section that lists only the lines reviewed for it
  (`catalog.key_route`; their reviewed efforts' selectors, the documented
  window as `max-context-length`, the reviewed efforts as thinking levels,
  force mapping), `disable-codex-cloaking: true`, `request-retry: 0`, no
  websockets, never an empty `models` list; a missing, blank or invalid key
  omits it, retained continuity and capture aliases are not served on it
  (one render notice), and `transport_problem` refuses the choice while no
  line is reviewed. `render.rendered_selectors`, `provider_selectors` and
  `served_plan.routes_from_document` (`SECTION_CODEX_KEY`: base, bearer,
  header names, the section switches and each model's capabilities) read
  the section. The route is offered: the codex API-key safety patch turns
  every provider failure of a codex API key (non-2xx body read through a
  64 KiB bound, terminal stream event, transport error) into fixed local
  text, and a codex API key with cloaking off sends no codex client
  `Version` default and no session header; `tests/test_openai_key_route.py`
  checks both on the real render and the isolated gateway, and
  `TransportAlternative.closed_reason` still closes a descriptor a build
  does not offer. A selected codex key transport (approved or withheld,
  keyed or not) also renders the global `disable-image-generation:
  passthrough` (`render.IMAGE_GENERATION_KEY`), never otherwise: the pinned
  codex executor adds its hosted `image_generation` tool to every Responses
  request unless the setting says otherwise, and `passthrough` neither adds
  nor strips it (`internal/config/disable_image_generation_mode.go`); the
  codex account pool serves nothing while the key is selected, so the
  account route keeps its request shape. On the pinned gateway one client
  turn on the key route is one Responses request (no retry when it
  succeeds), and the client's `max_tokens` is not forwarded: the Claude →
  codex translation sets no `max_output_tokens`, so the model's own output
  limit applies (`PinnedGatewayKeyRouteTurnTests`; `/responses/compact`,
  multi-turn continuation and encrypted-reasoning replay with the pinned
  client are not exercised). In the TUI, Enter (and K) on an account
  provider whose key this build offers opens its account-or-key chooser
  (`ConnectActions.chooser`, `screens.providers.key_choice`; the key route
  names its reviewed models; Anthropic's consent keeps its title,
  `cli.text.TRANSPORT_TO_KEY_TITLES`), and the switch is the setup layer's
  `plan_transport`/`apply_transport`. While the key transport is selected,
  a reviewed line's details in Models (`views.line_inspection(key_route=)`)
  show the key route's evidence: its documented input bound, window and
  output, and `catalog.key_route_floor` (the conservative validated floor),
  never the account route's measurement. Outbound proxy:
  `render.outbound_proxy_problem` admits only an empty or credential-free
  `http`/`https`/`socks5`/`socks5h` URL (host and port; no userinfo, path,
  query or fragment) with value-free diagnostics; the reviewed `gateway.json`
  `proxy-url` renders through it, T2 has no per-provider proxy, and
  `claude-multi setup --step gateway --proxy <url>` (or `--no-proxy`) sets it
  through the setup layer's `plan_proxy`/`apply_proxy` (one served change
  for a running gateway).
- **Feedback drafts.** `settings.PreferencesStore` owns
  `<config root>/preferences.json` (closed schema: `claude_feedback_drafts`
  `off|notify`, absent = off; never `settings.json`, a record snapshot, the
  ledger or `LAUNCH_TIME_SETTINGS_KEYS`). It is an explicit compile input
  (`scope.compile_lineup_scope(feedback_drafts=)`, `compile_lineup_launch`,
  `transition.expected_plan`, `RuntimeParts.feedback_drafts`): `off` adds
  `CLAUDE_CODE_SEND_FEEDBACK=false` to the scope `env` (a launch-time key,
  restaged from proven launch files, so a toggle never drifts a running
  session); `notify` omits it. The proof is
  `test_scope_probe_client.FeedbackDraftsProbe` on the real pinned binary (a
  default-tools subagent and one listing `SendFeedback`): off removes it
  from every tool list; the `env-off` class needs a positive notify control
  for the lead and a subagent. 2.1.281 records the `env-off-subagent-withheld`
  class: notify restores the lead only, because the client withholds the tool
  from every subagent (a client boundary, not the preference's effect); both
  classes are essential (`upgrade.ESSENTIAL_EVIDENCE_ALTERNATIVES`), and the
  `env-ignored` class fails. `ExpectedPlan.preference_launch_differs` makes doctor say
  "managed-session preference changed; applies at the next resume"
  (Attention, never BLOCK). `COMPILED_SETTINGS_KEYS` is unchanged.
- **Tests:** fixtures under `tests/fixtures/operator/`; `test_operator`
  (domain), `test_cli` Operator* classes (verbs, guard matrix),
  `test_scope_probe_client` (CC, `FeedbackDraftsProbe`, XC on the pinned client),
  `check_fixture_isolation.py` probe 3 (operator state in the probe HOME).

### 3.3 The gateway inhibition and the entry points

One durable inhibition per state root (`gateway_inhibition`) freezes every
change to the gateway or its installation while one transaction owner works:
`gateway service install|uninstall` (owner `service`), the installer and the
updater (they call `gateway_inhibition.guard` before they mutate, and hold
their own record with `begin`/`end`), and a machine move (the shell command).
It extends the start lock and replaces the old hand-off record: `begin`
writes under `gateway-start.lock`, and every start, stop and hand-off checks
it under the same lock before it dispatches anything. The refusal is one
outcome (`inhibited`, exit 1, `gateway_inhibition.refusal`'s message naming
the owner and its remedy). Reading stays available: health, status, doctor,
and `ensure` for a gateway already proven ours and ready (it awaits, never
starts). An owner may hold the start lock for its whole transaction: a
caller that finds it taken while an inhibition is recorded never waits for
it — a change reports the inhibition at once (`Gateway.lock_for_change`, the
service verbs' `_lock`), and `ensure` checks the destination, ownership and
readiness without the lock (`_ensure_unlocked`, read-only). A stale record
(owner process gone, or no progress within its bound) keeps refusing; doctor
BLOCKs on it (Attention while in progress). Recovery is explicit: the
owner's `recover` hands it its record back, and the service verbs take over
a stale `service` record (that finishes an interrupted hand-off: install
re-applies and starts, or re-runs an interrupted refresh; uninstall, with no
service recorded any more, finishes an interrupted `uninstall`); an
unreadable record is never taken over. An earlier `service-handoff.json`
reads as a `service` record (stale ten minutes after its start or once its
process is gone; unreadable, or present beside `inhibition.json`, it is
unknown) and every record writer migrates it under the start lock first.

Writers that do not dispatch serialize with `begin` through the writer fence
(`gateway_inhibition.fenced`: `<state>/gateway-inhibition.lock` shared from
the check through the write; `begin` holds it exclusively while it records),
so a writer that passed its check finishes before a begin returns: the shim
refresh, `Runtime.render_gateway`, `Runtime.provision_endpoint`, the setup
step's endpoint change (also under the start lock, and its start under the
same hold), every write phase of `proxy.rotate_token` and `prune_aliases`
(`_FencedLock` around the api-key lock), and the `claude-multi-proxy`
commands (held from the entry check until the command ends or execs; the
lock is close-on-exec). A HOME-scoped change fences every root that may own
that home's gateway (`gateway_inhibition.home_roots`: the writer's own and
the managed root `continuity.json` records), so another `--state-root` or
`XDG_STATE_HOME` never bypasses the managed root's owner. A root adoption
(`init --state-root R --adopt-root`) is a writer of both roots: it holds
both fences shared and moves `continuity.json` under the api-key lock. So the
managed root is read again once the fences are held (`fenced(…, home=)`)
and, under the api-key lock, right before each write
(`gateway_inhibition.revalidate`: `render_runtime_config`, every
`_FencedLock` phase of the rotation and the prune — which also resolve the
roots afresh for each phase — and the management-key step of `init`): a root
adopted meanwhile that the writer did not fence refuses with `the managed
state root changed while this command waited; run it again` (exit 1), since a
begin of the new root does not wait for it. The management-key write itself
is not under the api-key lock (its leaf lock is its own), so its re-read
narrows, but does not close, that window. A `continuity.json` that exists but
cannot be read or validated is an unknown authority, never none: every
HOME-scoped configuration or credential change refuses
(`AuthorityUnreadable`, exit 1, the remedy names the file; doctor shows it),
the shim refresh is withheld (`shims_withheld == "authority"`), and reading
stays available (a plain `snapshot-auth` copy checks its own root only). The
unit's directives stay the reviewed exception (their render keeps the
seed-only fallback). The fence is never held across a start, a stop or a
wait for the start lock (a launcher holding the start lock never waits for
its own `run` child).

Every path that can change state outside the command dispatcher applies the
checks itself or is a reviewed exception (tests call each one directly):

| Path | Channel | Ownership | Inhibition |
| --- | --- | --- | --- |
| `claude-multi-proxy` init (plain, `--start-check`, `--adopt-root`), `run` (plain, `--prepare-and-exec`), the sign-ins, the management-key verbs, `snapshot-auth --restore` | marker read in `_entry_refusal` for the requested and the managed root (never claimed) | `run`: the single-instance lock | `_entry_refusal` inside both roots' writer fences, held through the command (the owner's token from the environment; a spawned `run` gets it only from its owner) |
| the unit's directives (`init --prepare-start`, `init --reload-check`, `run --prepared`) | exception: the installed unit runs the link `service install` selected under the launcher's guard (the not-set-up refusal still applies: a service install always records the endpoint) | `--prepare-start`: the instance lock is part of the stopped proof; `--prepared`: the lock | exception: the manager runs what a fenced path (`ensure` on the systemd backend, the service verbs) or the transaction owner asked it to |
| `claude-multi-proxy status`, `snapshot-auth` (a copy) | read only | — | read only |
| the `/cm` lineup intercept | `installs.guard` before its Runtime | the record and lifecycle locks of `lineup.apply` | Runtime leaves the shims alone; a relaunch's start goes through `Gateway.ensure` |
| the hooks (`session-event`) | exception for session metadata only (records, notices, logs); the shared shims follow the marker: Runtime's shim refresh reads it and leaves them alone for another channel or an unreadable marker (`shims_withheld == "channel"`) | the record locks and the epoch | Runtime (non-scope-only events) leaves the shims alone; no hook starts, stops or renders |
| the developer entry point (`claude-multi-dev`) | exception: it writes the verified checkout and its drafts | — | it never builds or installs; the plan application boundary (`plan.application_refusal`) refuses anyone but the owner |
| `python3 -m claude_multi.gateway_inhibition` | exception: channel-neutral (an installer or a move may change the channel) | the start lock | the transaction owner's own tool: it writes only the record |

The launcher's Runtime skips the shim refresh while an inhibition it does
not own is recorded (`Runtime.inhibited_shims`), so a transaction owns the
hook and token shims until it ends; it skips it too when the state root's
channel marker names another installation or cannot be read
(`Runtime.shims_withheld`, checked at the write for every entry point that
builds a Runtime). Publishing a configuration is fenced the same way:
`Runtime.render_gateway` (the provider and model writes),
`proxy.rotate_token` and `proxy.prune_aliases` refuse for anyone but the
owner.

A home that is not set up (`endpoint.set_up`: no `endpoint.json`, no
earlier `config.yaml` or key) gets nothing rendered, reloaded or started on
the packaged port, which another program may own: `Gateway.ensure` without
`choose_port`, `Runtime.render_gateway`/`verify_reload` (the operator verbs:
`models add`, `providers apply`, …), `rotate_token`, `prune_aliases` and the
rendering `claude-multi-proxy` commands refuse with `endpoint.NOT_SET_UP`
and `claude-multi setup --step gateway`. Only an explicit start, the setup
step, a service install or a launch's `provision_endpoint` records the new
install's port. An existing install keeps the packaged port until setup or a
move writes `endpoint.json`.

## 4. Development workflow

```bash
# from the repository root (resources live in src/claude_multi/data/)
python3 tools/test.py                                # full suite in a private temp HOME (the gate at the end of a larger change)
python3 tools/test.py tests.test_launch              # focused
python3 tools/test.py --tier fast                    # fast tier — iteration only; never a gate
python3 tools/test.py --claude PATH                  # the real-client lanes, with the pinned Claude Code
PYTHONPATH=src:tests python3 tests/bless.py          # re-bless goldens after intentional compiler changes — review the diff!
PYTHONPATH=src:tests python3 tests/bless.py --check  # dry run: lists goldens that would change/be pruned; exit 1 if any, writes nothing
python3 tools/docs_gen.py                            # rewrite the generated regions of the docs
python3 tools/docs_gen.py --check                    # exit 1 when a generated region is stale
git diff --check
python3 tests/check_fixture_isolation.py             # acceptance: shipped model add, shipped retirement, operator state in HOME → zero test/golden changes
python3 tools/test.py tests.test_python_packaging    # wheel/sdist build + install outside the checkout (pinned setuptools)
python3 tools/build.py gateway --target host         # the gateway for this host (see CONTRIBUTING.md)
python3 tools/build.py bundle                        # this host's release bundle
# optional Nix lanes (run at the release candidate)
nix build --no-link .#checks.x86_64-linux.claude-multi  # sandbox suite (flake check; git-tracked files only)
nix build --no-link --file tests/default.nix         # same derivation, alias (also sees untracked files)
nix-build --no-out-link nix/package.nix              # offline package build
```

Run the suite through `tools/test.py`: a bare `python3 -m unittest` run
uses your own HOME and environment.

- **Gate levels.** Development gates stay light; the heavy battery runs
  once, at the release candidate. Fail-fast and cheap steps first:
  - a change: `bless.py --check`, `git diff --check` and the test modules
    that cover it (tripwire on). New PTY or subprocess tests are also run
    once from a different cwd with an empty HOME.
  - the end of a larger change: the same plus the full host suite.
  - a release candidate: the full host suite, then the Nix sandbox check,
    fixture isolation, the package build and the gateway check.
  The list below is what those levels run.
- **Gates, in order:** the full suite (`tools/test.py`, tier full),
  `git diff --check`, `tools/docs_gen.py --check`,
  `check_fixture_isolation.py` when the catalog or test isolation moves.
  Nix is optional for contributors; the release candidate also runs the
  flake check `checks.x86_64-linux.claude-multi` (from the repo root; it is
  `tests/default.nix` itself, not a copy, and evaluates only git-tracked
  files — `git add` new files first or the sandbox will not see them) and
  `nix/package.nix`. `nix build --file tests/default.nix` stays as an alias
  for the same derivation.
- **Gateway gates:** the local fail-closed gateway lane
  (`tests/_gateway_harness.py --run`) and the separate
  `checks.x86_64-linux.claude-multi-gateway` sandbox check
  (`tests/gateway-check.nix`). The main check remains separate.
  Run from the repository root, using the **current built** admitted-series
  output (never the live service or a guessed PATH binary):

  ```bash
  export CLAUDE_MULTI_TEST_CLI_PROXY_API="$GW_OUT/bin/cli-proxy-api"
  export CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE="$(echo "$STARTUP_OUT"/bin/*-startup-probe)"
  export CLAUDE_MULTI_TEST_REQUIRE_GATEWAY=1
  export CLAUDE_MULTI_TEST_GATEWAY_EVIDENCE="$EVIDENCE_DIR"
  PYTHONPATH=src:tests python3 tests/_gateway_harness.py --run  # the local gateway lane
  nix build --offline --no-link --print-out-paths .#checks.x86_64-linux.claude-multi-gateway  # the gateway sandbox check
  ```

  `STARTUP_OUT` is the matching `tests/gateway-startup-probe.nix` diagnostic,
  not the shipped gateway. `--check-modules` prints the shared ordinary
  inventory. The require flag turns unavailable gateway/isolation boundaries
  into failures; the runner additionally rejects every skip. Evidence dirs
  are 0700, JSON 0600; synchronous finalization propagates write failures.
  `--validate-evidence "$EVIDENCE_DIR"` checks required files and table rows,
  including the complete unique shipped-wire census. The gateway sandbox
  check also proves the
  Nix sandbox (no sandbox fallback), supplies trusted store bwrap and writes
  `$out/unittest.log` plus `$out/evidence/*.json`; success requires zero skips.
  It validates its evidence privately (0700/0600) **inside** the build; Nix
  then normalizes the output to 0555/0444, so the store copy is published,
  world-readable metadata (the same allowlisted, secret-free rows), not
  private evidence. Check that copy with
  `--validate-published-evidence <out>/evidence` (read-only store content
  check); `--validate-evidence` stays the private host/build check.
  Never claim a cached build is a fresh execution.
- **Budget and diagnostic lanes:** the ordinary gateway lanes target ≤20 s on a
  quiet host; >30 s stops the leg. Concurrent-load timings are labelled,
  not used for that stop rule. Revert, lifecycle, races, the hint-client
  check and Retry-After are outside this budget.
  `tests/test_gateway_hint_client.py` (the hint-client check) is
  **host-only, full tier**:
  successful native completion, all main/subagent/helper/count-tokens classes,
  unchanged detector inputs and complete relay accounting are mandatory.
  `/rename` auxiliary coverage is not a no-beta Haiku-helper census.
  Retry-After is likewise host/full-tier observation, not an ordinary gateway-lane row.
- **Revert/re-pin tool:** from a clean committed tree run
  `PYTHONPATH=src:tests python3 tests/check_gateway_patch_revert.py --out <private-dir>`.
  It builds diagnostic omissions offline and publishes `patch-revert.json`;
  each patch needs a discriminating own-red row and the complete series an
  all-green control. Declared dependencies use closure omissions: patch 7
  needs patch 5's `internal/config/config_load.go` hunk; omit {5,7} and
  compare {7} alone to attribute patch 5 (`_gateway_harness.PATCH_DEPENDENCIES`). Expected dependency cross-red
  is not own-red evidence. Undeclared build failures remain blocked.
  Each omission build is a full gateway build: check that the Nix store and
  the Go caches have room before a batch (`df -h /nix/store "$HOME/.cache"`).
  The tool's exit 0 requires each omission to be discriminating: own rows red and
  every other row green except declared dependency rows; a globally red
  omission fails, and a `CLAUDE_MULTI_TEST_REQUIRE_GATEWAY=1` boundary in any
  omission run is recorded as `blocked` (exit 2), never as red.
  Diagnostic derivations must reuse the gateway's pinned `goModules`, with
  input identity checked **before** building; no new fetching vendor
  derivation. Vendor reuse is judged on what the derivation consumes: each diagnostic
  expression asserts `tests/vendor-inputs.nix` (the go-modules derivations in
  its string context) equals the gateway's, and the Python tools
  (`_gateway_harness.consumed_vendor`: `nix derivation show` `env.goModules`
  plus every `*-go-modules.drv` input's output) record and require the pinned
  path; the `share/go-modules` files only echo an attribute.
- **Race diagnostics (observation only):** from a clean committed
  tree, with the same free-space check, run
  `PYTHONPATH=src:tests python3 tests/check_gateway_races.py --out <private-dir>`
  (`--series-diagnostics <store-out>` reuses a built
  `tests/gateway-race.nix` output; `--startups/--reloads/--http-runs` bound
  the workload; `--keep-logs <private-dir>` keeps raw local logs outside the
  evidence; `--self-test` runs only the pure checks, which the ordinary suite
  also runs as `test_gateway_harness.RaceToolSelfTests`). Every process runs
  in `bwrap --unshare-net` against a unix-socket fake; exit 2 is blocked
  (no isolation). It publishes `<out>/races.json` (dir 0700, file 0600,
  metadata only) after validation: the scenario inventory per build, the
  consumed-vendor identity, stored verdicts equal to recomputed ones, and the
  rejected-reload control never negative. A negative reads **"not reproduced
  within the bound"**, never "race absent", and only when every required
  scenario completed its workload and itself executed both sides of the
  pair's target path (reload scenarios above the startup baseline); split
  coverage across scenarios is inconclusive. Reproductions are always kept.
  Not a product gate and outside the ordinary budget.
- **Fast tier (`CM_TEST_TIER=fast`) — iteration only; never a gate.** It
  skips every real pinned-binary class (`test_scope_probe`
  RealPinnedBinaryTests and the `test_client_*` real-client modules) with a
  `BOUNDARY: fast tier` reason via the test-side gate `tests/_tier.py`
  (`real_binary_gate` / `fast_tier_boundary`); everything else still runs.
  Measured 2026-09-26: ~98 s fast vs ~221 s full. Unset or `full` is the
  unchanged full suite (the final and release levels); any other value errors.
  `check_fixture_isolation.py` drops the variable, so acceptance runs are
  always full.
- **Live-gateway connect tripwire (always on).** `tests/__init__.py` arms
  `tests/_tripwire.py` for every run that imports the `tests` package
  (`discover -s tests -t .`, focused `tests.test_x` runs, the Nix sandbox):
  a connect/connect_ex to 127.0.0.1/::1/localhost (any loopback or
  unspecified address) on 8317, 8316 or the port of the developer's own
  `~/.config/claude-multi/endpoint.json` (read once at import) raises
  `ConnectionRefusedError("test tripwire: live gateway port")` before the
  real connect, is recorded in `_tripwire.HITS`, reported on stderr at exit,
  and — when `CM_ISOLATION_GUARD_LOG` is set — logged once as a `BLOCKED`
  line (it refuses before delegating, so with the external sitecustomize
  tripwire also installed a hit is counted once). It covers the test
  process only; child processes rely on the external tripwire and the
  real-binary network namespace. No test binds a fake gateway on those
  ports, so there is no opt-out: fake servers use port 0 or unix sockets.
  `tools/test.py` installs the external tripwire as well (the
  `sitecustomize` guard from `check_fixture_isolation.GUARD_SOURCE` on every
  child's `PYTHONPATH`) and fails the run on any logged hit.
- **Hygiene gates.** `python3 tools/history_scan.py --repo .
  --out <private dir>` scans every blob, commit and tag reachable from all
  refs for credential shapes and personal identifiers and prints names,
  counts and locations only (never a value; exit 1 while a candidate
  credential remains). A hit is a synthetic sentinel only for a placeholder
  marker, fill characters, a repeating pattern, a value made of words or an
  identifier, or an entropy below 0.72 of a random value of its own alphabet
  and length (hex keys never reach an absolute bits/char bar); everything
  else is a candidate. The same shapes and verdicts gate the tree
  (`test_hygiene.SecretScanTests.test_tree_carries_no_candidate_credential`,
  every file whatever its suffix). Identifiers are also counted in tree/blob
  paths and ref names. The public identifiers are generic
  (`GENERIC_IDENTIFIERS`: the home directory of a real account; neutral
  identities such as `/home/user` pass); the exact list of the people and
  computers behind the repository is a private input that is never
  committed: a file named by `--identifiers FILE` or by
  `CLAUDE_MULTI_PRIVATE_IDENTIFIERS` (format: `history_scan.py`
  `PRIVATE_INPUT_ENV`), which CI writes from a repository secret when there
  is one. The tree gate `test_hygiene.IdentifierGateTests` uses the same
  table (file contents and file names): no identifier in the repository
  except the sites on `IDENTIFIER_RATCHET` and the private input's
  `identifier-site` entries, each at its exact count with the pass that
  removes it (test data uses neutral identities: `/home/user`,
  `example.lan`; a test that asserts recorded content keeps the string and
  is listed). The origin gate (`OriginNarrationTests`) reads its patterns
  and ceilings from the same private input; without it, only its mechanism
  runs, on synthetic vocabulary, and the tree scan reports a BOUNDARY skip.
  `OPERATOR_DEFAULTS` records operator preferences shipped as product
  defaults that no identifier names, with their owners. Shrink the tables
  when a site is fixed.
- **Test servers** (fake providers in `probe.py`, `test_launch`,
  `test_proxy`, the unix upstreams) call `serve_forever(poll_interval=0.01)`
  so `shutdown()` returns in ~10 ms instead of up to 0.5 s; new test servers
  do the same.

On memory-tight hosts the suite's tmpfs fixture copies can fail with
`Errno 122 Disk quota exceeded`: run with a disk-backed temp dir, e.g.
`TMPDIR=~/.cache/claude-multi-test-tmp python3 tools/test.py`

- **Goldens** pin byte-exact compiler/scope output (`goldens/v2/**`: the
  fixture `balanced`/`direct` scopes, argv, env, lead appendix, notices),
  the hook shims (`goldens/shim/**`), the v4 records, reports and `/cm`
  outputs (`goldens/v4/**`), plus the gateway render
  (`goldens/render/gateway-default.yaml`).
  All of them are generated from the frozen **test fixture** (see below), not
  the shipped catalog. Any intentional change to generated bytes requires
  bless + diff review. `bless.py` owns the whole `tests/goldens/` tree: it
  writes every golden (the render one via `test_render._render`, so test and
  golden share inputs), then **prunes** every file it did not write and prints
  each as `pruned <path>` — review deletions like any other diff.
  It refuses to write outside `tests/goldens/`. `bless.py --check` generates
  the same bytes but writes and deletes nothing: it lists each
  `would change|would add|would prune <path>` and exits 1, or exits 0 when
  the tree is clean. Golden tests assert through
  `tests/_golden.py` `assertGolden(self, path, actual_bytes)`, which fails
  with a truncated unified diff and the exact bless command — use it for
  every new golden family. The hook shim made scope bytes
  machine-independent again — keep them that way (no volatile paths, no
  timestamps).
- **Test catalog fixture.** Tests read a frozen asset root,
  `tests/fixtures/assets/` (10 catalog-32 keys converted to models v2, all
  `status: active`, plus one retired key `muse-spark`; all 8 providers,
  byte-copied schemas, settings, version, prompts, roles (roles v2 and the
  six function prompts, byte-equal to shipped); seven **frozen
  fixture seed profiles** (`catalog/profiles/`) that mirror each
  shipped seed's slot→family map, `native_agents`, `workflows`,
  `lead_providers` and `primary_provider` rather than copying its bytes (the
  fixture has no astra/luna/sonnet line), guarded structurally by
  `test_profile_seeds`; the legacy default
  composition moved out of the catalog tree to
  `tests/fixtures/v3/default-composition.json` (frozen by sha256 at
  its catalog-32 bytes, never loaded by `load_catalog`, read only by
  `tests/_v3.py` to build legacy snapshots); `gateway.json`
  `patches: []`; native-contract executable under `/nonexistent/`). The
  fixture deliberately violates the catalog-33 shipped-shape rules (canonical
  selectors, key+effort selectors, contract level == effort) —
  those are shipped-shape tests and dev-promote checks, never
  `validate_catalog` rules. Roots come
  only from `tests/_catalog.py`: `FIXTURE_ROOT` (behavioural tests),
  `SHIPPED_ROOT` (the real catalog, i.e. the packaged resources) and
  `GOLDENS_ROOT`; every repository path comes from `tests/_layout.py`
  (`REPO_ROOT` for source, entry points, child PYTHONPATH and checkout
  paths; `RESOURCES_ROOT` for the packaged resources; `FIXTURES_ROOT`;
  `PATCH_DIR`, `CANDIDATE_DIR`, `NIX_DIR`, `DOCS_DIR`, `PROBE_BASELINES`;
  `logical_path` resolves an evidence label against its owner;
  `fake_checkout` builds a writable source checkout for developer-command
  tests; `tests/test_resource_layout.py` guards against repository-root
  resource joins), so no test computes `Path(__file__).parents[…]` itself
  and every file a test reads is part of this repository. **Rule: tests use
  the fixture; only
  shape/evidence/upgrade-synced tests read the shipped catalog** —
  `test_catalog`'s shape and evidence classes (exact-inventory asserts are
  invariants, so adding a model never moves them), the upgrade-synced
  literals in `test_catalog.py`/`test_native_contract.py` (rewritten by
  `claude-multi-dev repin`; `test_upgrade.RealSyncedLiteralTests` proves the sync still finds
  them), `test_scope_probe` and the `test_client_*` real-client modules (they run the real pinned
  binary), `test_packaging`, `test_hygiene`, and catalog-release classes
  marked `@uses_shipped_catalog`. New or rewritten tests never pin shipped
  model ids; derive counts/positions/heights from the loaded catalog. The
  fixture is never regenerated at test time
  (`tests/fixtures/build_fixture_assets.py` records how it was cut; re-running
  it is a deliberate fixture change → re-bless). `test_fixture_assets` guards
  it: schemas byte-equal to shipped (mirror a schema change), clean
  `validate_catalog`, routes-as-data rules, no real binary path. The
  **pinned-registry fixture** `tests/fixtures/registry/` is a
  frozen cut of the 7.3.15 registry (`claude`, `codex-free`, `codex-pro`
  sections and codex metadata for the fixture's OAuth wires; recipe
  `tests/fixtures/cut_registry_fixture.py`); tests reach it only through
  `CLAUDE_MULTI_REGISTRY_DIR`, never the real registry.
  `tests/check_fixture_isolation.py` is the acceptance experiment (not
  auto-discovered): probe 1 copies the tree, adds a fake model line (shipped
  selector shape) to the copy's **shipped** `models.json`, runs the full
  suite inside a private network namespace (only loopback; a Python
  tripwire logs any connect to 8317/8316), re-blesses and diffs the
  goldens; probe 2 does the same in a fresh copy with a fake retired
  key (`isolation-retired`, successor null, one selector) in the shipped
  `retired.json`, after asserting `resolve_key` and the continuity seed
  cover it (`--retired-probe` runs only probe 2); probe 3 runs the
  suite with a valid `providers.d` declaration and a ledger that admits its
  line in the **probe HOME** and requires those files byte-unchanged
  afterwards (`--operator-probe` runs only probe 3). The OVERALL line names
  all three (`model`, `retired`, `operator`). Exit 0 means zero failures,
  zero gateway attempts and zero golden changes in every probe.
  If the only failures are real-binary probe classes it re-runs them once
  outside the namespace (one real-binary probe is namespace-sensitive). `--python-guard-only` for hosts
  without user namespaces; `--only TEST` for focused re-runs.
- **PTY tests** (`tests/test_tui_pty.py`) drive the real TUI in pseudo-terminals
  (line + curses modes, no-ctty fallback). The bare-launch child arms a 20s
  `faulthandler` so a hang self-diagnoses into the captured output.
  The real-binary probes (`tests/test_scope_probe.py` RealPinnedBinaryTests)
  are timing-sensitive under machine-wide load: a timeout-class error there
  that passes on an isolated re-run is the documented flake signature
  (seen once 2026-08-10 with the suite running alongside heavy agent fan-out);
  re-run the class before distrusting the pin.
- **Real-binary runs are network-hermetic.** Every
  `allow_real` run (RealPinnedBinaryTests, the `tests/test_client_*`
  real-client modules, the `claude-multi-dev repin` evidence run) execs the pinned client
  inside `bwrap --unshare-net` — loopback only, host resolver sockets,
  `/run/user/<uid>` and container-engine sockets masked, fixture-private
  `TMPDIR`/`CLAUDE_CODE_TMPDIR` — and reaches the fake provider through a
  unix-socket bridge. **A trusted bubblewrap (root-owned or a store path) is
  therefore required**: without it these tests skip with `BOUNDARY:`, and
  `claude-multi-dev repin` refuses (its essential probes are missing). The Nix
  main sandbox check has no bwrap, so it runs these as boundary skips.
  The separate gateway check supplies trusted store bwrap: the trust check
  accepts root ownership, or `/nix/store/` ownership (not the caller's euid) equal
  to the overflow UID read from `/proc/sys/kernel/overflowuid`; an
  unreadable/invalid file falls back to the kernel default 65534,
  **not** root-only, so a 65534-owned store executable stays trusted then.
  A non-store or ordinary user-owned executable remains refused. This seam
  does not prove that the overflow UID is unmapped; that trust residual is
  retained, not silently broadened into a uid-map guarantee.
- **Go tooling:** set `GOPATH=$HOME/.cache/cm-go`, `GOMODCACHE=$HOME/.cache/cm-go/mod`
  and `GOCACHE=$HOME/.cache/cm-go/build` (never `~/go`).
- **Gateway build:** `python3 tools/build.py gateway` (stdlib Python ≥ 3.11, no
  Nix) builds the patched gateway from `gateway/UPSTREAM.json`: `fetch`
  (upstream source at its commit and the official Go toolchain archive for
  the host, both sha256-verified), `vendor` (`go mod vendor`, normalised
  vendor-tree hash), `apply` (the admitted series, no fuzz or offset),
  `build` (`CGO_ENABLED=0 -trimpath -buildvcs=false -mod=vendor`, upstream
  version in `main.Version`; four shipped targets plus a windows-amd64
  compile canary into `dist/gateway/<target>/`; like `gates`, `record` and
  `notices` it first re-hashes the source and vendor trees and refuses a work
  dir edited since `vendor`/`apply`, so no output carries input hashes its
  content does not match), `inspect` (no concrete Nix
  store reference, static Linux, system-only macOS libraries, the linker's
  ad-hoc signature on darwin/arm64, patch markers) and `record`
  (`BUILD.json`, `gateway-contract.json`; for the admitted series it also
  refuses a build the checked-in notices or SBOMs no longer describe, then
  copies `gateway/licenses/` plus a generated `THIRD_PARTY_NOTICES.txt` to
  `dist/gateway/licenses/` and each shipped target's CycloneDX SBOM to
  `dist/gateway/<target>/cli-proxy-api.cdx.json`); `gates` runs the typed Go gates
  (portable with CGO off; Linux race gates with cgo), `repro` builds a target
  twice in different directories and compares, `compare --other` checks a
  binary built elsewhere. Inputs are cached in `~/.cache/claude-multi-build`;
  `--offline --inputs DIR` builds from pre-fetched inputs only.
  `notices` regenerates `gateway/licenses/` (verbatim licence and notice
  texts of CLIProxyAPI, the Go toolchain and every module the shipped targets
  link — read from each binary's embedded build information; a module's
  vendored subpackages that carry their own licence keep it at its
  package-relative path, a module vendored inside another's directory keeps
  its own, and source files named like notices are not notices — the patch
  series' `MODIFICATIONS.txt` and the `modules.json` inventory with SPDX ids
  and go.sum hashes; `license-overrides.json` only for a module that ships no
  licence) and `gateway/sbom/<target>.cdx.json` from a work dir that built
  every shipped target; `contract` writes the package resource
  `src/claude_multi/data/gateway-contract.json` from the recipe (byte-equal
  to the Nix passthru, which the gateway derivation and the package assert);
  `registry` copies the pinned source's model registry (from a work dir
  prepared by `fetch` alone) into `src/claude_multi/data/registry/`.
  `tests/test_third_party_notices.py` re-renders everything derivable offline
  and fails on drift. Regenerate both after any series, source, vendor or
  toolchain change.
  `gateway/upstream-review.json` records the review of every upstream release
  after the pinned one (range, commits, the rendered configuration the
  reachability judgement assumes) with one disposition per fix: `backport`
  (an admitted patch now, only for a fix reachable in the rendered
  configuration that protects stored credentials or sign-in/API-key route
  availability), `rebase` (carried by the rebase onto the next upstream line)
  or `not-applicable`. `test_upstream_recipe.UpstreamReviewTests` ties it to
  the pinned commit, so a re-pin needs a new review. govulncheck
  (`-mode=binary`, per shipped target) needs its vulnerability database and
  runs in CI, not on the offline build host.
  `nix/gateway.nix` fetches the same inputs as fixed-output derivations and
  runs the same steps offline, so Nix and plain builds are byte-identical;
  the flake exposes `packages.gateway-<target>`, and the checks
  `gateway-gates`, `gateway-race`, `gateway-inspect`, `gateway-repro` and
  `gateway-windows-canary`. `tests/test_build_tool.py` compares a Nix
  output and a `dist/` build when `CLAUDE_MULTI_TEST_NIX_GATEWAY` and
  `CLAUDE_MULTI_TEST_DIST_GATEWAY` name them.
- **Release bundles:** `python3 tools/build.py release --out DIR` (no Nix;
  `bundle --target T` for some targets, no installers) assembles one
  `claude-multi-<version>-<target>.tar.gz` per target of
  `packaging/product.json` (the bundle → gateway target and Claude Code
  platform map, and the `bin/` launchers: `claude-multi`,
  `claude-multi-proxy`; never `claude-multi-dev`), plus the
  release's `MANIFEST.json`, `SHA256SUMS` and `install.sh`/`install.ps1` with
  the version, checksums and signing keys filled in (`install.sh` carries the
  bundles' checksums only, `install.ps1` the sha256 of `install.sh`, and
  `SHA256SUMS` lists the manifest, the bundles and both installers, so the
  signature covers every release member without a checksum cycle). Inputs, each verified:
  the gateway recipe's dist (`--gateway-dist`, or the gateway steps run
  first; `BUILD.json`, the contract, binaries, notices and SBOMs are checked
  against the recipe and `gateway/licenses`/`gateway/sbom`, the Windows
  canary never ships), the pinned python-build-standalone runtimes
  (`packaging/python-runtimes.json`, delete-only prune), their licence
  inventory (`packaging/licenses/python/`: `inventory.json` and the texts,
  read once from the same release's `pgo+lto-full` archives by
  `tools/_build/runtime_licenses.py derive`; per target the full archive's
  sha256, every library linked into the shipped runtime with its version,
  SPDX licences, texts and the extensions that link it, and every pruned
  extension; the build refuses an inventory that does not describe the
  pinned runtimes, a shipped library without its text, a shipped
  strong-copyleft library or an extension module it does not describe —
  copyleft rule: an extension whose library is strong copyleft is pruned,
  as `_dbm` (Berkeley DB) is, and the launcher never imports one; weak
  copyleft ships with its text), the package in
  `src/`. A bundle is an installation prefix: the package in
  `lib/python3.14/site-packages/claude_multi` (resources in its `data/`),
  the documents in `share/claude-multi`, the runtime in `runtime/python`,
  the gateway in `libexec/claude-multi/cli-proxy-api`, `share/licenses`
  (the runtime's in `share/licenses/python/`: CPython's `LICENSE.txt`, the
  target's library texts, `INVENTORY.json` and `THIRD_PARTY_NOTICES.txt`),
  `share/sbom` (CycloneDX: launcher, gateway, runtime with its linked
  libraries as components) and `MANIFEST.json`.
  Bytecode (the package's and the pruned standard library's,
  unchecked-hash, in-bundle file names) is compiled by the pinned
  interpreter native to the build host, which the build re-execs under (so
  gzip bytes agree across builders; `--no-reexec`, `--python` for tests).
  The `bin/` launchers (`tools/_build/bundle.py`) resolve their installation
  without resolving the install's `current` link, force
  `CLAUDE_MULTI_CHANNEL=bundle`, the resources, the hook command
  (`<root>/bin/claude-multi`) and the bundled gateway, unset
  `CLAUDE_MULTI_PROXY_PATCHES`, append the bundled terminfo to
  `TERMINFO_DIRS` and run `python3 -I -B`. The host target's bundle is run
  before it is archived (every package module and every standard-library
  module the package imports, and `--version`). Archives are deterministic
  (source date = `SOURCE_DATE_EPOCH` or the commit time; identity = the
  uncompressed tar's sha256); `release compare --other DIR` (each
  directory's `SHA256SUMS` checked against its files, then the archive
  contents, the manifest and both installers, the fields a gzip encoding
  changes — compressed checksums and sizes, the installers' copies of
  them — aside) and `release repro` check it. `tools/release.py` runs the release: `preflight`
  (clean tree, release version and catalog bump, dated CHANGELOG, contract,
  gateway parity, notices, hygiene, history scan, the production key, the
  release-level gate receipt), `build`, `reproduce` (fresh clone),
  `sign` (prints the `ssh-keygen -Y sign` command; never holds the key),
  `verify-draft` (the signature, checksums and manifest — `SHA256SUMS` must
  list exactly the manifest, its archives and both installers, as `build`
  checks of its own output too — then the draft
  installed by its `install.sh` into a temporary HOME: both launchers'
  `--version` and `.github/scripts/journey.sh installed` on it — doctor's
  first run on the new installation (`journey_fixture.py first-run`:
  `doctor --json` and `doctor` agree, and the result is ready or blocked
  only by the named conditions of an install before its setup, each
  doctor line matched whole; any other exit fails), the on-demand gateway
  and its confinement, the managed fixture turn and resume with
  `--client`, the foreign listener, the uninstall keeping state; any
  failure fails it, and so does a journey without the first run's success
  line) and `publish` (`gh`: the draft must hold exactly the verified
  release — every file `SHA256SUMS` lists (the archives, the manifest and
  both installers) and `SHA256SUMS`, byte for byte — before the typed
  confirmation, and again,
  every asset downloaded and compared by sha256 and size with the uploaded
  signature, right before it is made public; anything else is refused and
  the release stays a draft). The packaged
  `release-trust/allowed_signers` names the production key (its fingerprint
  is published in `SECURITY.md` and `docs/security.md`, and
  `test_operator_docs` checks both against the line); a trust that names no
  key is refused by `trust.release_signers`, so such a build cannot verify a
  release with its own trust (the refusal tests use a keyless copy,
  `tests/_release.py` `keyless_trust`/`keyless_repo`). Tests:
  `tests/test_release_build.py` (all four targets over synthetic runtimes
  and gateway outputs), `tests/test_release_tool.py` (throwaway keys).
- **Installing and updating:** `packaging/install.sh` (POSIX sh) verifies
  `SHA256SUMS` with `ssh-keygen -Y verify` whenever ssh-keygen exists (a
  failed or missing signature is fatal; the checksums built into a
  release's installer are used only without ssh-keygen), unpacks into its
  private work directory, then runs the bundle's own
  `claude_multi.install_txn` (strict marker, inhibition, state format,
  gateway hold) before any change and again under the install lock and its
  inhibition; it records that inhibition through
  `python3 -m claude_multi.gateway_inhibition` (owner `installer`, the
  shell's PID, the token exported as `CLAUDE_MULTI_INHIBITION_TOKEN` to
  every later child) and advances it (`replace`, `switch`, `prune`,
  `launchers`); a failure or signal before `replace` ends it, after it
  keeps it, and `--repair` recovers it (`recover --owner installer`), tidies
  and finishes; with nothing installed yet (a first install stopped before
  a version reached `versions/`) `--repair` installs the release it is
  given (`--from-dir`, or the download location), verified like any
  install, whose run recovers the record under the install lock. `current` and `previous` change as one recorded step
  (`install_txn switch` = `self_update.switch`: the links as they were go
  durably to `install/.switch.json` before the first changes, removed once
  both are in place); a run stopped between them is put back as recorded
  (`finish-switch`, `self_update.finish_switch`: by `--repair` and by the
  next `update`; an unreadable record changes nothing and keeps the
  inhibition). A rollback switches only to the pair it showed (the
  updater's `rollback(confirmed=)`, compared under the lock; the
  installer re-reads its links under the lock). Cleanup keeps `current`, `previous` and the running
  gateway's version and prunes Claude Code copies with the use lock
  (`install_txn prune-copies`); it writes the launchers, the marker
  (`claim`) and the receipt (`install_receipt`). `claude-multi update` (and the card's U) is
  `release_update` over `self_update`: same transaction, the packaged
  release key, no same-version replacement. State migrates forward only:
  a rollback (installer or `update --rollback`) refuses once the state
  moved past what the older version reads, and older code over newer
  state refuses writes (and, at the hook dispatcher, model switches) with
  the remedy. Tests: `tests/test_installer.py`, `tests/test_install_txn.py`,
  `tests/test_self_update.py`, `tests/test_release_update.py` (the real
  installed `claude-multi update` and acquisition over fake signed
  releases, and the https transport against a test CA); fake releases
  (`tests/_fake_release.py`) carry this checkout's package.
- **CI (written, run only after approval):** `.github/workflows/ci.yml`
  (fast lane: goldens, whitespace, `tools/history_scan.py --range` over the
  pull request's or push's commits — the scan policy is in the file — and
  lint; nightly battery, a serial PTY lane, the Nix checks),
  `release.yml` (build, arm64 rebuild + `release compare`, macOS bytecode,
  `tools/_build/vulncheck.py` (govulncheck per shipped gateway, fail
  closed), the essential battery on the exact pinned client
  (`tools/test.py --claude`), journeys with `.github/scripts/journey.sh`
  on the exact bundles — install, the pinned client, the on-demand gateway
  and its loopback confinement (refused on the host's other addresses, a
  control listener on every interface answering), a first managed turn
  against a loopback fixture provider (`.github/scripts/journey_fixture.py`,
  declared and admitted like any provider) and its resume, another
  program's listener on the gateway port refused, update and rollback —
  the draft behind the protected `release` environment) and
  `pin-watch.yml` (`tools/_build/pin_watch.py`). Every action, tool and
  container image they use is pinned (actions by commit, images by index
  digest; zizmor also by its wheel's sha256, installed with
  `--require-hashes`) to its newest release (JavaScript actions on
  Node.js 24) and recorded with its upstream verification date in
  `.github/PINS.md`. `tests/test_release_ci.py` checks them as text (the
  pins against `PINS.md` both ways), checks every tool option they pass,
  runs the journey script on fake releases and its fixture helper on
  loopback. The release integration tests that sign fake releases skip
  without ssh-keygen (`_fake_release.needs_ssh_keygen`) unless
  `CLAUDE_MULTI_TEST_REQUIRE_RELEASE=1` (set by the CI workflow and by
  the sandbox check, which supplies openssh's ssh-keygen) makes them fail
  instead. The sandbox check also supplies openssl (its binaries), so the
  TLS tests that make a disposable certificate authority run there.
  `tools/test.py --claude PATH` places the
  verified pinned client in the private HOME for the real-client lanes;
  without it such a lane (the keyed client lane once
  `CLAUDE_MULTI_TEST_CLI_PROXY_API` is set) is reported not runnable and the
  run fails.
- **Version**: bump `launcher_version` in `src/claude_multi/data/version.json` for behavior changes;
  `catalog_version` only for trusted-catalog content changes. Records carry
  both; old launchers fail closed on newer record versions. An unreleased
  line bumps `catalog_version` once (the first catalog change after the last
  release); later content changes on that line keep its number (1.0.0 stays
  at 37) and update only the digests and goldens they move. The first change
  merged for a new release line sets `launcher_version` to `<next>-dev`; the
  release sets the final version. `claude-multi-dev repin` refuses a
  checkout of another major.minor release or one older than the running
  launcher (`upgrade.checkout_guard`; a `-dev` suffix is ignored).

## 5. How to make common changes

- **Add/revise a model or provider:** two sanctioned paths, by size. For a
  new provider or a provider-kind change (transport, auth, routes), use the
  product's own pipeline — `claude-multi-dev` draft → check → review
  (exact diff/hash) → promote; never hand-edit the catalog
  (`src/claude_multi/data/catalog/`) without it. For a
  same-provider model addition (the Opus 5 pattern), a direct catalog edit
  is acceptable **when it lands with the full battery**: schema load +
  `validate_catalog`, the shipped-catalog shape/evidence tests in
  `test_catalog` green (a new `minimum_tested` above the 2.1.216 baseline
  goes into its locked `floors` map), the live gateway re-rendered
  (`claude-multi-proxy init` writes the config; the patched
  7.3.15 watcher hot-reloads the rename-replaced `config.yaml` and init
  verifies it through the render sentinel within 2 s — it prints
  the restart remedy (`claude-multi gateway restart`) only when the reload did not
  apply, e.g. an unpatched gateway or the startup race), doctor's served
  cross-check clean, and one consent-gated live call. The behavioural suite and the goldens run on the frozen
  test fixture (§4), so a shipped model addition moves **no** behavioural
  test and **no** golden — `tests/check_fixture_isolation.py` proves exactly
  that. Only touch the fixture when the change needs a new *line class* the
  fixture lacks (then re-bless and review).
  **No test reads the running gateway.** Picker tests run on the injected
  fixture served set: `Runtime(served_models_callback=…,
  health_get=…)` is the seam every `/v1/models` and `/healthz` call site
  routes through (the gateway token is read from the runtime HOME), and
  `CLITestCase` injects the fixture set plus a fixture token. The "not
  served" row/modal path is covered deterministically by
  `test_screens_direct.DirectMarkTests.test_an_unserved_row_asks_before_launching_and_cancel_stays`. The real radar for a catalog
  the gateway does not actually serve is **doctor's served cross-check**
  against the live `/v1/models` after the reload — run `claude-multi
  doctor` and confirm it is clean; never point a test at 127.0.0.1:8317.
  Only `models.json` / `retired.json` / `providers.json` / seed
  `profiles/*.json` are ever edited.
  A preview→production flip follows one sequence (wire_model → context
  re-verify → effort tiers → one live call → display), as qwen3.8-max
  did. GLM is retired and inactive; reopening needs demand, an exact route
  and per-call qualification approval; do not infer availability from
  another GLM product or reuse its ID/route.
  DeepSeek aliases follow a distinct
  first-party convention: the wire is whatever id
  DeepSeek's own docs name as canonical — since the V4.1-Flash release that
  is literally `deepseek-flash` (previously `deepseek-v4-flash`, which is now
  only a compatibility redirect with no stated sunset); the Pro slot stays
  `deepseek-v4-pro`, whose requests DeepSeek reroutes to V4.1-Flash from
  2026-09-14 04:00 UTC until V4.1-Pro ships. Dated Flash-0731 / Pro-0813
  strings are resolved version labels, never first-party callable ids.
  **Accepted residual:** a docs-canonical tier name (`deepseek-flash`) can
  retarget to the next generation with no catalog change and no error — the
  same hidden-state hazard the OpenRouter rule rejects. It is accepted only
  because DeepSeek publishes no dated callable id to pin instead, so the
  routing_note's "currently …" label (and the version-bearing display) is
  the sole in-repo truth anchor and must be re-verified at each DeepSeek
  release. A docs-only GA adds the catalog entry with a conservative
  `validated_tokens` floor; move that evidence field only after the
  separately approved live call. OpenRouter
  moving aliases (e.g. `~x-ai/grok-latest`) are NOT trusted catalog wires
  even when they currently resolve the target release: the doctrine requires
  record+catalog to be the complete authority; an upstream alias retarget
  would be hidden state and cannot be repaired by the per-response model
  echo (scope is already compiled). Pin `x-ai/grok-<version>` instead and
  record the moving alias only as considered/current-target evidence.
  If user profiles need a not-yet-installed catalog model, evaluate the
  exact profile documents against the candidate catalog first
  (`profile.evaluate` in a sandbox copy, or `claude-multi profile show <name>`
  after the switch), keep the live profiles valid until the release carrying
  that model is installed, and
  change them only through `ProfileStore` (the editor or `profile` verbs;
  0600); a line that leaves the catalog needs a retired entry (below), so
  a stored profile resolves to its successor. The legacy staging of preset
  files under `tests/fixtures/compositions/<batch>/` is history.
- **Model line shape (models v2, catalog 33+).** A line is keyed by a
  **stable line key** (`opus`, `sol`, `muse` — never the generation)
  and carries `provider`, `display`, `generation` (data, e.g. `"5.5"`),
  `wire_model`, `efforts`, `default_effort` (∈ efforts), `lead` (an
  `{effort, env}` block with the effort in the contract's `lead_efforts` —
  the catalog-33 lines use `{"effort": "ultracode", "env": {}}` — iff
  `"lead"` is a capability, else `null`), `capabilities`, `roles` (`"all"` or a list of non-lead role ids;
  `[]` iff not `agents`-capable), `context`, `routing_note`,
  `minimum_tested`, `status` and `registry_overlay` (`null`, or the `{"channel": "claude"|"codex"}` marker for an OAuth-pool wire the pinned registry lacks; see the OAuth how-to below).
  A codex-pool line may also carry `key_route`, what the OpenAI API
  documents for it: `efforts` (a subset of its own), `context_tokens`,
  `input_tokens` (at least its provider bound), optional `output_tokens`,
  `source: "docs"`, `source_ref` and `checked`; only such lines are served
  on the OpenAI API-key route, at those efforts, and nothing is inherited
  from the account route's evidence (`catalog.key_route`, validated by
  `validate_line`; an operator line never carries one). The effort
  shape follows the provider adapter (`catalog.effort_mode`):
  - *client-effort* (Anthropic OAuth pool, OpenAI-compat): one `selector`,
    `efforts` a list; the effort travels in agent frontmatter. Anthropic
    selectors are the canonical wire id + `[1m]` for a 1M line
    (`claude-sonnet-5-5[1m]` — a `claude-multi-*` Anthropic id is unknown
    to the client), and the wire must be an Anthropic `passthrough_routes`
    entry in `providers.json`; OpenAI-compat is `claude-multi-<key>[1m]`.
  - *gateway-effort* (codex pool, claude-compatible direct): `efforts` maps
    each level to `{selector, proxy_contract}`, selector
    `<prefix><key>-<level>[1m]` (`gpt-multi-` on the codex pool,
    `claude-multi-` otherwise; `[1m]` iff 1M) and the contract of the same
    level (`reasoning-effort-<level>`, `output-config-<level>`) declared in
    the provider's `payload_contracts`. Levels ⊆ the native contract's
    `agent_efforts` (never `ultracode`; the codex `reasoning-effort-max`
    contract exists since catalog 34).
  The shape rules (canonical Anthropic selectors, key+effort selectors,
  contract level == effort) are shipped-shape tests in `test_catalog` and
  `claude-multi-dev` promote checks, not `validate_catalog` rules (the frozen
  fixture violates them on purpose). `claude-multi-dev promote <draft> --repo <repo>` always lands
  a line `status: "new"` (New · not admitted: offered like any line on an
  enabled, usable route and rendered and served; admission in Settings
  `admitted_lines` — the Models screen `M` or `claude-multi models admit <line>` —
  is an optional badge; never in a seed); flipping it to `"active"` is a reviewed catalog edit. A direct
  catalog edit (same-provider addition with the full battery above) writes
  the whole entry in this shape; `claude-multi-dev model add --like <line> --id <new-id> --wire-id <wire> --name <draft>`
  scaffolds it (it refuses Anthropic like-lines: a new Anthropic
  generation needs a route edit — see below). Bump `catalog_version` for
  every content change — once per unreleased line: a line that already
  bumped it keeps its number (the unreleased 1.0.0 line keeps 37; §4
  "Version").
- **Retire, rename or move a line — the retired map
  (`catalog/retired.json`).** **Removing a selector requires a retired
  entry**: live sessions, records and compiled scopes still name it, and the gateway
  keeps serving it only through the continuity set seeded from this map
  (`continuity.json`; a selector with no entry goes `invalid model` after
  the next render). Rules:
  - *Removed or renamed key* → an entry under the old key:
    `successor` (the live line that takes its bindings, or `null` = the
    session needs a model choice at resume), `reason` (≤256 chars, dated
    when an upstream date matters), `since_catalog` (the new
    `catalog_version`), `provider`, `last_wire`, `display`,
    `context_tokens`, `capabilities`, `roles`, and `selectors` = every
    selector the line served → its **recorded** proxy contract (`null` for
    client-effort). A successor must be an active line that admits the
    entry's capabilities and roles; chains are ≤8 hops, acyclic.
  - *Same-key generation move of a client-effort (Anthropic) line* (the
    selector changes with the wire): add `<line>@<old generation>`
    (`opus@4.8`, `fable@5`) with `successor` = the line and `generation`
    set; keep the old wire a passthrough route while it is continuity or a
    `REFUSAL_FALLBACK_WIRES` target.
  - *Gateway-effort generation move*: generation-tag the new
    selectors (`<prefix><key>-<generation>-<effort>[1m]`) and retire the old
    selectors under `<key>@<old generation>` on their recorded wire. Running
    sessions keep the old generation through continuity; resume rebuilds from
    key + effort. Continuity is not a selectable old-model rollback. Dev
    promotion distinguishes this from historical same-alias retargets (which
    still move on reload); `--like` safely refuses generation-tagged siblings,
    naming `--from-json`. The new-line promote shape check is unchanged.
    Live doctor repair refuses generation renames with “applies at the next
    resume”, never adding an agent selector outside the recorded fence.
  - A retired selector base must stay unique and may not shadow a live
    selector on another wire or a route of another provider.
  - *A provider leaves the catalog* (as `llm-local` did): retire each of
    its lines with `successor: null` and the explicit
    `externalized: {"sample": "<provider>.json"}` marker (the validator then
    accepts the unknown provider but requires null contracts and no
    overlay), ship `examples/providers.d/<provider>.json`, and amend older
    entries that pointed at it. Render stays quiet about its continuity
    aliases (`catalog.externalized_providers` → `quiet_providers`) until an
    operator declares the provider, when they serve again; doctor names the
    sample only for sessions leading on it. Split its `_LISTING_SUPPORT`
    descriptor into the sample's `listing` block. Never fabricate an
    admission or a successor.
  - Keep entries: records and resume-time choices resolve through them
    (`resolve_key`). Dropping one does not unserve its aliases — the
    persisted continuity set is monotonic; an alias leaves the gateway only
    through `claude-multi doctor --prune-aliases` (refused while a live
    record references it).
  `tests/check_fixture_isolation.py` probe 2 proves a shipped retirement
  moves no behavioural test and no golden; the frozen legacy selector table
  (`test_catalog.FrozenSelectorCoverageTests`) must stay covered.
- **Add an OAuth model the gateway doesn't know:** first check the
  pinned registry (`claude-multi-dev check <draft>` reports missing OAuth-pool wires).
  Patch 16's `oauth-extra-models` registers extra `claude`/`codex` wires with
  `name`, optional `display-name`, positive `max-context-length` and
  `max-completion-tokens`, and validated `thinking`. The schema is closed;
  aliases belong to `oauth-model-alias`, never this overlay; no headers or
  transport controls. Two producers feed it (`operator.render_plan` →
  `render.plan_oauth_overlay`): **T1**, a catalog line or retired entry with
  `registry_overlay: {"channel": "claude"|"codex"}` (the closed marker:
  the channel must be the provider's `transport.pool`, the wire must satisfy
  the loader name grammar, capabilities come from the entry's own fields —
  context, optional output, and thinking levels of its efforts on both channels; a retired
  marker carries no thinking). The optional T1/retired `output` object matches
  T2 exactly: `declared_tokens` 1..2097152, `source` listing/docs/operator/registry,
  optional `source_ref` (1..256 chars). It is declared provenance, not measured
  acceptance; absence emits no limit. The `gpt-6.1-sol` generation uses this form.
  **T2**, an operator pool line (`models add anthropic|openai <wire>`,
  `docs/guides/models.md`): no authored overlay field,
  `operator.overlay_projection` derives it. Precedence per (channel,
  lower-case wire): current T1 > current T2 > retained captures (retired
  rank 0, captured alias rank 1; equal retained metadata merges
  conservatively); current definitions that disagree refuse the render; a wire
  the pinned static registry serves is not emitted when that registry is known
  (`proxy.overlay_static_wires`), and the gateway's static-wins rule drops any
  duplicate; exclusions still apply, invalid
  entries refuse the whole reload, and any overlay change refreshes OAuth
  registration. Render through `init`, verify doctor's served check, then make
  one separately approved minimal call; a New model is not offered until
  admission. `test_scope_probe_client.ExactClientProbe` (XC, essential) proves the
  pinned client keeps an overlay-only claude wire's declared efforts,
  thinking, window and output limit. Never hand-edit secret config or add a
  Go registry patch for a model the overlay can express; a gateway re-pin is
  the alternative. Preserve the committed patch order through 20 (11 omitted).
- **Change a role, a prompt or a seed profile.** Roles are closed:
  the id set is `catalog.ROLE_IDS`, and each id's function, grade, prompt
  file, isolation, read-only tool list and `requires` follow from its id
  (the role rules in `catalog.py`), so a role edit is almost always a `description` edit
  (never a model, provider or family name). Prompts are one file per
  function and must stay model-free (`test_roles`
  `PromptNoIdentityNamesTests`; the only allowlisted phrases are
  `claude-multi`, `Claude Code` and the seed-profile command
  `` `/cm profile claude` ``); the lead prompt's contract substrings are
  pinned in `LeadPromptTests`. Mirror every roles/prompt/schema edit
  byte-equal into `tests/fixtures/assets/` (`test_fixture_assets`). Agent
  goldens embed the analyst/implementer/reviewer bodies and descriptions:
  re-bless and review. Seeds (`catalog/profiles/*.json`, pretty bytes) are
  maintainer-reviewed: change a binding only on a maintainer
  decision, bump its `seed.version` (installed copies are never
  overwritten; `ProfileStore.seed_updates()` reports the newer version),
  update the matching frozen fixture seed and `build_fixture_assets.py`'s
  `FIXTURE_SEEDS` when the slot→family map moves, and keep a New line or
  provider out of every seed (dev promote refuses it).
- **Add a payload contract:** `render.ADAPTER_PAYLOAD_CONTRACTS`
  drives synthetic aliases, provider declarations and the generated A rows
  automatically (including codex max);
  `test_gateway_contracts.ContractCompletenessTests` checks complete coverage. Add a
  discriminator for a new contract kind; do not hand-maintain model ids/counts.
- **Add/re-derive a gateway patch:** edit only the series owner
  `gateway/UPSTREAM.json` (basename, sha256, `admitted`; a gate record for
  its Go tests), keep `catalog/gateway.json` in exact order, add a
  `PATCH_ROWS` entry in `tests/_gateway_harness.py` and prove its own-red row
  with `check_gateway_patch_revert.py`. Declare prerequisite edges explicitly;
  no rebased patch workaround for a failed single omission. Git-add a new
  patch before flake evaluation. A gateway or native-client re-pin re-runs
  the version precondition, the configured user-agent baseline checks, the hint-client
  check and the revert tool; the hint-header flag stays OFF until its own
  review enables it.
- **Change compiled settings:** extend `COMPILED_SETTINGS_KEYS` + the compile
  + tests + bless; state the demonstrated failure case in the commit.
- **Change record shape:** bump `RECORD_VERSION`, extend
  `_normalize_legacy_record` (side-effect-free read migration), update
  `schemas/session.schema.json`, never rewrite-on-read.
- **Change lifecycle semantics:** preserve the epoch protocol (higher epoch
  wins; equal-epoch duplicate-runtime hook is ignored; `SessionEnd` advisory
  only). Hooks must stay metadata-only, 5s, stdin-reading.
- **Add a command:** report vs interactive classification goes in
  `_STDOUT_REPORT_COMMANDS`; interactive flows self-degrade via
  `streams_curses_capable`.
- **Re-pin Claude Code (maintainers; a re-pin is a release):** from a source
  checkout run `claude-multi-dev repin` (`--repo PATH` defaults to the
  checkout it runs from; `--manifest-dir DIR` verifies offline). It finds a
  newer installed artifact (the PATH `claude` target, the native versions
  dirs), verifies it against Anthropic's signed manifest, inspects it
  offline (`--version`/`--help`/SHA-256), writes the next contract v2 (every
  signed platform build; `battery` evidence for this host's platform,
  `identity+smoke` for the others; the `settings_keys` the build knows, read
  statically by `upgrade.settings_keys_from_binary`, or a reviewed JSON array
  given with `--settings-keys FILE`; a new pin without them is refused before
  any write) into the checkout, bumps
  `catalog_version`, syncs the suite's pinned literals, runs the full
  offline suite (the real-binary probes find the candidate by hash) and
  records the suite output's sha256 as the evidence receipt. Promotion
  requires a zero-exit suite and every essential completion record; any
  failure restores the checkout byte-exactly. No operator override is
  written, and nothing is built or installed: the new pin reaches users
  with the next release built from the checkout. `python3 tools/pin_claude.py VERSION
  --manifest-dir DIR --receipt FILE` writes the next contract from a
  verified manifest pair without running the battery; a new version needs
  `--settings-keys-from FILE` (the build, checked against the manifest) or a
  reviewed `--settings-keys FILE` (`upgrade.render_contract` refuses a new
  version without keys, and `test_catalog` requires them in the packaged
  contract). The sync of pinned test literals moves every signed platform
  build's sha256 and `["<platform>"]["size"], N)` assertion too. Users get
  the new pin with the next release and `claude-multi
  setup --step claude`. The pin-skew battery in `test_scope_probe_client` (SK
  settings skip, OC owned copy leaves the user's install alone, FL first
  launch on an empty config, SL newer-client transcript resume) and
  `test_scope_probe.test_real_adoption_onto_newer_plain_claude` (AD) are
  diagnostics; SL and AD need a newer build installed next to the pin.
- **Subagent-model environment hygiene:** clear both
  `CLAUDE_CODE_SUBAGENT_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` at the
  one launch boundary (`compile_lineup_launch`, `V2_ENV_UNSET`). Reserve both in lead env and audit settings
  reintroduction. Since Claude 2.1.257, FORCE can override explicit agent
  models even when the old variable is absent; the generic `env_unset` loop
  applies the compiler policy without a duplicate launch.py key list.
- **Anthropic generation bumps (stable keys since catalog 33):** a new
  Anthropic generation **moves the existing line** (`fable` 5 → 5.1): new
  `generation` and `wire_model`, the canonical first-party id as the
  selector base (`claude-fable-5-1[1m]`), the new wire appended to the
  Anthropic `passthrough_routes` (a `providers.json` edit, so the
  provider-kind path or a reviewed direct edit — the dev scaffold refuses
  Anthropic like-lines), and a `<line>@<old generation>` retired entry for
  the old selector (retired-map bullet above). The legacy pattern (a new key per
  generation, `fable51`) is retired; those keys now live in `retired.json`.
  Three things make this correct rather than obvious:
  1. *Canonical selectors matter through a gateway.* The pinned client's family
     alias table is `fable:{default:"claude-fable-5-1",
     per_provider:{gateway:"claude-fable-5"}}` (likewise `opus`→
     `claude-opus-4-7`, `sonnet`→`claude-sonnet-4-6`): through a gateway the
     **bare family alias resolves to an older id**, so a fence must pin the
     explicit id to get the new generation. Never "fix" an anthropic entry by
     switching it to a bare family name.
  2. *A model the pinned registry predates needs an overlay or re-pin* — on 7.3.15 check
     first: the registry already carries every current Anthropic and codex
     wire (claude-opus-5-5, claude-fable-5-1, claude-sonnet-5, gpt-6-*), so
     most generation bumps need **no** overlay. Use `oauth-extra-models` through
     a reviewed registration or an operator pool line for a missing wire (above). When a gateway patch is needed, order it
     in the committed `gateway/UPSTREAM.json` order: loopback-oauth →
     kimi-claude-compat → non-claude-cache-retention →
     management-readonly-allowlist → watcher-parentdir →
     serve-after-initial-auth-load → management-env-only →
     credential-save-report → credentialed-redirects → openai-compat-keyed-safety →
     auth-snapshot-locking → server-config-snapshot → plugin-host-locking →
     claude-metadata-locking → oauth-model-overlay → no-antigravity-egress →
     codex-client-identity → antigravity-loopback-callback →
     codex-api-key-safety. Retry-After (11) is not
     admitted. New hunks must be generated against the
     **sequentially patched** source. Generate it offline against
     the pinned tag (`git show v<baseline>:internal/registry/models/models.json`,
     apply the preceding patches, diff) and prove: it refuses to apply without
     its prerequisite, applies cleanly in module order, yields valid JSON, and
     does not disturb the patches that follow (`tools/build.py gateway apply`
     refuses any reject or offset). Add it to `gateway/UPSTREAM.json`
     **and** `catalog/gateway.json` together — the parity tests pin the exact
     order.
  3. *Flake evaluation only sees git-tracked files*: a new patch must be
     `git add`-ed (staging is enough) or the switch fails with "is not tracked
     by Git". Evidence fields stay conservative: a docs-only GA gets the
     conservative `validated_tokens` floor, and `minimum_tested.claude_code`
     records the release that introduced the model, added to the separately
     locked `floors` map in `test_catalog.py`.

## 6. Rules of engagement (non-negotiable)

1. **No real-provider calls without explicit user approval, per call.**
   Discovery and qualification require real stdin/stdout terminals and
   no Claude-session marker, even empty. The old in-session y/N and approval
   flag are retired; no flag waives the guard. This is a mitigation, not a
   hard boundary: a pseudo-terminal is not itself evidence of human consent.
   The approval rule remains the operator's discipline.
2. **Never touch the live Claude daemon/supervisor; never read user
   transcripts; never delete anything under `~/.claude` or a
   transcript-bearing root.** Rollback never deletes state.
3. **Tests before claims.** The tests that cover a change pass, and the
   full suite (`python3 tools/test.py`) passes before a larger change is
   called done; the Nix sandbox check and the package build run at the
   release candidate.
4. **Commit discipline:** coherent commits in the Conventional Commits form
   (`CONTRIBUTING.md`). A launcher-only change reloads a running gateway; a
   gateway binary, patch, `gateway.json` or unit-text change needs a
   restart (`claude-multi gateway service install` tells the two apart). A
   suspected auth-persistence failure is exempt: keep the gateway running,
   free space, wait for a `persisted` save and re-authenticate only if none
   appears, before any restart.
5. **Doctor is the truth surface:** real damage must BLOCK; by-design lazy
   state is Attention with the exact fix command. Never demote damage to
   attention, never let lazy state block.
6. **Keep the map current:** update `AGENTS.md`, the page under `docs/`
   that owns the topic (`docs/USAGE.md` maps them; `docs/STANDALONE.md` for
   facts about plain Claude Code) and `CHANGELOG.md` with any behaviour
   change; keep one canonical home per topic — pointers elsewhere, never
   copies.

### Credential-save observations

Doctor's `gateway credential save: <outcome>` line contains only provider, auth
index, written/attempted bytes, numeric errno, `credentials_changed` and fixed
operation/stage/category. `persisted` alone means verified durability; `unchanged`,
`skipped`, `unverified` are Info but never verified saves. `failed` is Attention
(keep the gateway running, free space, wait for a `persisted` save, re-authenticate
only if none appears, before restart/rollback) unless a later event of the same
(instance, provider, auth index) at the same or a newer epoch/generation repairs it:
`persisted`, or non-superseded `unverified` with `credentials_changed=true`.
A repaired failure stays visible as Info (no re-authentication); repair evidence is
never backup-retirement evidence. The one journald JSON pass runs newest first
(`--reverse`) over 24 h, capped at 10 s, 32 MiB and 65,536 records, so a cap drops
the oldest records and the coverage lines say "partial journal read since …"; only
the collector refuses messages over 8 KiB; its output ≤256 events. Source cursors
are optional; timestamp or boot/invocation identity absent means incomplete, never
a guessed current PID.
Neither this window nor a current-binary match attests historical receipts to the
installed build. No raw/basename/path-bearing event text reaches doctor. The
redirect-refusal log is fixed text.
`snapshot-auth` skips only patch 8's `.<base>.cm-save-<16 hex>` file names before
stat/open (never `*.json`); the normal checks still apply to credential records.
Keep `auth.pre-*` backups until every identity has a persisted receipt from the
installed gateway with no later failure, and retire them one name at a time with
the operator's approval. Doctor never retires backups.

## 7. Debugging tools

- `claude-multi doctor` / `--repair UUID` / `--repair-all` / `--prune` —
  health, converge to record authority, bulk converge + snapshot refresh
  (the record's `launcher_version` is preserved — repair never relaunches),
  stale-file sweep (never transcripts).
- `claude-multi quota` — read-only passive local report, no state cleanup
  or shim refresh; the user-facing description: `docs/guides/gateway.md`.
  It has closed quota fields, JSON and the read-only `/cm quota` (the
  same Runtime collector and formatter on the session surface); unknown values
  remain unknown.
- `claude-multi doctor --rotate-token` — the only sanctioned gateway token
  rotation (tty only, confirmations on stdin). Rerunning resumes
  any interrupted phase; a `previous-key` in `~/.config/claude-multi` is
  the phase marker (doctor shows it as Attention). Tests drive it through
  `Runtime(served_models_callback=…)` plus injected clock/sleep — never
  the live gateway.
- `claude-multi doctor --prune-aliases [<alias>…]` — remove gateway
  continuity aliases (`~/.config/claude-multi/continuity.json`) that no live
  session references, then re-render and verify the reload. Named aliases
  referenced by a live record (last event not `end`) refuse the whole run;
  all-mode keeps them (and the refusal-fallback wires) with a reason; an
  unreadable record, an unlistable sessions dir, a corrupt continuity file,
  a token rotation in progress or a newer state marker refuse. Records are
  never modified. Removed aliases are tombstoned (never re-seeded at the
  same catalog version).
- `claude-multi migrate --dry-run` / `claude-multi migrate` — convert
  legacy records to v4 (dry run writes nothing; the real run backs up every
  record byte-exactly as `sessions/<id>.v3.json`). `claude-multi restore-2x
  [--not-running <id>…] [--assume-dead <id>…]` — the rollback to a launcher
  that reads legacy records only: refuses while a session may be live, restores
  the backups with the current lifecycle fields, quarantines v4-born records,
  removes the marker last. On live state only as a rollback.
  `claude-multi sessions mark-ended <id>` (or `--all-dead`) records a synthetic
  `end` for a session known to have exited without a SessionEnd. Record
  liveness is "no recorded end", so a launch that exited before SessionStart
  looks live; the refusals that a live record causes (`models rm`,
  `providers rm`, a render that would drop live aliases) name this command
  through `sessions.mark_ended_remedy`.
- `claude-multi-proxy snapshot-auth [--restore <file>]` — the auth-dir
  snapshot for a gateway rollback between 7.3.15 and 7.2.80.
- `claude-multi -r <id> --print-launch` — exact argv + env keys; the six
  credential keys appear only as `unset` lines and the gateway auth line
  names the scope apiKeyHelper. `--print-launch` never persists state.
- `claude-multi sessions show <id>` — the record (identity, epochs, aliases).
- `claude-multi sessions relink-runtime <id> <runtime-id> [--cwd <dir>]` — repair
  pre-hook UUID drift using native `/status`.
- Hook shim: `~/.local/state/claude-multi/bin/claude-multi-hook` — invoke it
  only with the record's exact `--launch-epoch`; **a higher epoch is accepted
  as a newer launch and will reject the real session's hooks as stale** (this
  mistake has been made once; the fix is a launcher resume,
  `claude-multi -r <id>`, which writes `launch_epoch + 1` — never a hand
  edit of the record). Shim-3 (`bin/claude-multi-hook-3`, v2 scopes) takes the
  same argv plus `--hook-protocol 3`; its prompt fast path answers from
  `scopes/<id>/lineup.gen` vs `notice/<runtime-id>.seen` without starting
  the launcher, and a failing protocol-3 hook leaves one metadata line in
  `<state>/hook-errors.log`.
- **Routing truth:** what each spawn was bound to is in
  `<state>/lineup-log/<id>.log` (`subagent-start` lines: `agent_type`,
  `lineup_gen`, `scope_selector`, label "binding at gen N (reload
  unconfirmed)" — the scope binding, not proof). What the gateway served is
  its log (the supervised unit's journal, or the on-demand instance logs): the selector
  lines' `model=<alias>`, counted, and the silent-substitution warn line
  `upstream served model "<served>" for requested model "<requested>"`.
  Read the journal only through filtered commands (no `auth=` value in the
  output; the selector line's `session=` is truncated and cannot tell
  sessions apart) and join the lines to one session by time window. A
  `cm-*` spawn whose journal
  `model=` is the lead's alias is degraded dispatch.
- **Process forensics (metadata only):** `ps -eo pid,etime,args | grep
  "[c]laude.*<uuid>"` for launch binding; `/proc/<pid>/environ` by name/count
  only (`grep -cE '^ANTHROPIC_(BASE_URL|AUTH_TOKEN)='`), never values — a
  helper-only launch has `ANTHROPIC_BASE_URL` only; an
  `ANTHROPIC_AUTH_TOKEN` there marks a process an older launcher started
  with the token in its environment.
- **`Cannot enter worktree … is the repository root`:** a read-mostly
  subagent (reviewer/analyst) called the native `EnterWorktree` tool to
  inspect an implementer's worktree — the pinned binary refuses
  root→worktree switching. The role prompts steer agents to
  the outside-in pattern (direct reads, `git -C <path>`, subshell `cd`)
  instead; on older scopes, resuming the session regenerates the scope
  from the installed catalog (`doctor --repair-all` covers sessions that
  are never resumed). Never "fix" it with tool denies or by spawning
  reviewers with worktree isolation.
- **Resume gate:** every resume is pre-checked by
  `_evaluate_resume_gate` (pure, metadata-only, no locks) with
  `Runtime.perform` as the mandatory backstop: repair-needed (cwd
  evidence) → the exact `relink-runtime` command (bare relink re-asserts
  the recorded cwd); transcript missing/elsewhere → restore-or-forget /
  relink-`--cwd` guidance (checked BEFORE liveness, so force can never
  bypass it); ● daemon-owned → stop-first guidance (`-r --force`
  bypasses ONLY that branch; TUI modal offers Stop & resume / Resume
  anyway / Cancel). A relaunch is a resume: the gate applies in
  full, and a launcher-driven stop waits out the compact-boundary
  window first. Model-only drift intentionally follows
  the allow-model-relaunch path, not the gate. Rows mark repair-needed
  with `!` next to ●/⚠.
- `claude-multi-dev probe <options>` — dev-only disposable-fixture harness; loopback
  fake provider; daemon-domain gate; live-domain tripwire. The delegation
  probe asserts subagent wire models against the production-shaped fence
  (the degraded-dispatch radar).
- **`/cm`** (the scope skill `.claude/skills/cm/SKILL.md`) runs
  `claude-multi lineup --session <runtime-id> <request>` with the session's runtime id, so `claude-multi`
  must be on the session's PATH; it is user-only
  (`disable-model-invocation: true`) and takes everything after `/cm` as
  one argument. From a terminal: `claude-multi lineup --session <runtime-id> show`
  (`claude-multi lineup --help` lists the requests and options).
- **Symptom index:** `CHEATSHEET.md` (installed next to `USAGE.md`; its path is
  printed at the end of `claude-multi --help`). Follow them before inventing
  ad-hoc procedures.

## 8. Known limitations / open items

- **Gateway sandbox route and privacy:** the gateway checks run in the
  nested bwrap sandbox route; there is no fallback route.
  Session, agent and parent ids can reach Claude-compatible direct upstreams
  on the identity branch, and the preserve branch forwards all six tested
  hint names. The native hint-client check sampled main, subagent and
  auxiliary helper on that direct route only; neither native OAuth coverage
  nor the measured no-beta Haiku profile is established.
  `CLAUDE_CODE_GATEWAY_HINT_HEADERS` stays OFF. The interval before the
  gateway's listener starts is not exercised. Reproducers and race runs are
  observations, not claims that the defects are repaired.

- **Development versions:** see §4 "Version" for the `-dev` convention and update guard.

- **Gateway unit:** the supervised unit is verified with `ProtectHome=tmpfs`
  and all 17 Tier-B hardening keys, none relaxed or removed.
  `gateway-unit.json` (a packaged resource) is the key/path authority,
  `platform/systemd_unit.py` its pure renderer (held to the reference unit,
  `tests/fixtures/service/reference.service`, by `test_gateway_service`). Python `+` preparation/reload escapes
  namespacing; Go sees the whole policy directory read-only and only auth,
  traces and gateway workdir/logs writable. `run --prepared` verifies without
  config writes; `init --reload-check` exits 0/3/4/5/1 for reloaded/restart
  required/token mismatch/down/error. Explicit `init --start-check` has a
  45 s budget, exits 0 ready / 6 not ready / 1 error. The unit does not run
  it.

- **Probe verdicts (2.1.281, fake provider, isolated network):** CE-A and
  DAH-override are essential and asserted: compaction env is mirrored into
  flag settings, and `disableAllHooks:false` is compiled. U1-proc, X5-silent-hours,
  SC-caps, SX-open-continue-dies, SF-lead-heavy, RL-none, RET-ok and
  R19-open (2.1.286: a missing PreModelSwitch target is a non-blocking hook
  error and the switch applies, hence the launch's hook-target BLOCK) are
  observations. SC/SX/X5 inform the lead's failure rules;
  a changed diagnostic emits an update note, not a runtime hint switch.
  The managed skill policy denies `Skill` for read-only roles (a frontmatter `Skill(name)` drops
  the whole tool on this client). Workflow-owned agents are retried by their
  workflow, never revived after completion by SendMessage; round 2 uses a
  fresh same-type reviewer with round-1 findings. Agents never wait for a reply.
  The recorded classes are `tests/fixtures/probe-baselines.txt`. An agent's
  context class follows the session window (§9, "Agent context class").
  Every fake reply of `test_scope_probe_client._reply` carries its own message
  id: the pinned client merges assistant messages that share one, so a
  reused id collapses the conversation a compaction probe needs.
- **Native-client doctor check:** only a known native-client inode mismatch reports Attention;
  all existing pinned/retained references are accepted, unreadable/deleted or
  non-client executables are unknown. Never manipulate the live supervisor to
  manufacture evidence. Whether the session appendix survives a supervisor
  takeover is not yet verified.
- **Supervisor takeover proof**: durable `--add-dir` carry is documented +
  binary-consistent, and the supervisor has taken over backgrounded
  managed sessions since 2.1.218; a post-takeover check that the lineup
  (agent files, `lineup.gen`) is still in effect is still to be watched
  once.
- **Hook-failure invisibility**: if Claude never invokes a hook (observed
  once: transcript written, zero events), the record is indistinguishable
  from healthy-at-rest. Hook stderr is not captured anywhere; detection
  requires Claude-side logging. Document, don't fake a fix.
- **Wide chars in the TUI**: cell-width is `len()`-based; CJK/wide strings
  misalign tables (cosmetic; escape injection is sanitized separately).
- **`--legacy`** is a retired flag: it is refused before any state
  is touched (exit 2). Legacy (v1/v2) **records** keep full support and
  upgrade to a durable scope on first resume. Do not reintroduce an argv
  mode: its env token was the credential path helper-only auth removed.
- **Stale shim-3 after rollback:** prompt/postmodel/subagent failures
  from a missing or older launcher are neutralized (stdout only on success,
  exit 0); start/end retain their status. `premodel` instead emits the fixed
  denial on a failed target, so the fence never opens. See `test_scope_v2`, `test_hooks` and
  `test_scope_probe_seeds.HookShim3RealBinaryTests`; resume/repair regenerates from the installed
  catalog. The legacy shim's frozen bytes are not a shim-3 recovery procedure.
- **Forced-resume relabel:** a process launched with the gateway token in its
  environment (`sessions.helper_only_launch` is false for its record) may
  still be running; helper-only launches never carry one. Rolling the
  launcher back cannot erase such a process's environment, so token
  rotation still requires confirming that every session which may hold an
  environment token has exited.
- **Gateway hot-reload residuals:** a config replaced in the brief
  window before the watcher starts is not seen until the next change
  (init's sentinel wait reports it as "restart required", which is the
  right operator action); deleting the config directory itself is out of
  scope; a blank, whitespace-only or api-key-dropping config never loads
  (restart to apply deliberately).
- **Record and preset migration:** `claude-multi profile migrate` (dry run by
  default) turns legacy presets into profiles and `claude-multi migrate` the
  records into v4; a rollback to a launcher that reads legacy records runs
  `restore-2x` first. The line-mode confirm/chooser stay the non-curses
  path. The resume gate checks transcripts by metadata only; the current
  explicit `CLAUDE_CONFIG_DIR` is recognized, historical roots remain
  unknown.
- **New-line admission** is an optional badge stored in `settings.json`
  `admitted_lines` (the record snapshot, the Models screen's badge, doctor's
  warning); it never decides whether a line is offered or fenced. The shipped catalog has no New line.
- **Continuity window:** the first `claude-multi-proxy init`/`run`
  after an install creates `continuity.json` (seed from `retired.json` +
  live records); until then doctor shows config drift, as for any catalog
  change. The file only grows until `doctor --prune-aliases`. A record
  scan cannot see sessions under another state root: doctor flags a
  persisted set extended from a different root.
- **GPT-6.1 Sol:** the stable `sol` key is at generation
  6.1 with generation-tagged aliases; `sol@6` keeps all GPT-6 aliases on their
  old wire until resume. Lead, agent and workflow-default selectors are `[1m]`
  (the route bound is above the session window). The 0.159.1 operator listing proves
  visibility only. Declared context is 1,050,000; the inherited GPT-6 route bound
  872,000 is unverified for 6.1; validated floor is 200,000, not near-limit
  acceptance. Output 128,000 is API-documented, not measured on the Codex route.
  No real acceptance is claimed. Shared discover identity is 0.159.1; patch
  #18 must use that version too. The move bumped the quality/max seeds to
  version 2 and balanced/economy/openai to 3; role/effort adoption needs
  explicit reseeding. Stable-key generation movement does not reseed
  installed profiles; the quality seed's Sonnet row relies on its
  seed-version bump.
- **GPT-6 lines (catalog 33; current for Luna/Astra):** the approved 2026-09-26 acceptance probe
  (through the gateway) set the context: 868,925 input tokens accepted on
  gpt-6-sol/luna/astra, ~985K rejected on luna, so `provider_tokens` =
  `provider_stated_limit_tokens` = 872,000 (the codex client template's
  `max_context_window`) and `validated_tokens` = 868,925; the 1M client
  class stays (`[1m]`). Operating capacity is unchanged:
  min(800K operating ceiling, 872K) = 800K, so managed sessions compact
  before the provider limit. Near-limit retrieval was unreliable in the probe (wrong
  answers at 868,925). Tests assert relations, never these numbers.
  The route check accepted the pinned gateway's codex UA 0.154.0 although the wire
  lists `minimal_client_version` 0.155.0; nothing enforces that minimum — a
  refusal would surface as an upstream error on every GPT-6 line.
- **gpt55 continuity after 2026-10-14:** `gpt-multi-gpt55-high` stays a
  served continuity alias on `gpt-5.5` (reasoning-effort-high); the pinned
  codex metadata marks `gpt-5.5` retiring 2026-10-14T19:00:00Z, after which upstream
  may refuse it and resume maps the key to `sol`.
- **Continuity effort rules are the recorded legacy rules**: e.g.
  `claude-multi-qwen38-max` keeps `reasoning-effort-xhigh`, not the rule its
  name implies.
- **Preview→production flips**: `qwen3.8-max` went through the sequence
  (wire_model → context re-verify → effort tiers → one live call → display)
  with a verified live call; it is the template for any future preview flip.

## 9. Discovery, qualification and TUI onboarding

- `discovery.py` is the pure registry/served/listing-plan/mark/prefill owner.
  Runtime served snapshots retain minimized metadata; models candidates and doctor
  use one shared known index (T1, T2, legacy, continuity and captures). Observation
  never declares/admit/binds. Discovery fetches only on explicit guarded request;
  T2 routes are revalidated before secret resolution and every send. Parser/text
  stay import-light. The old approval flag only prints migration guidance.
  `declaration_efforts` is the one effort default of a declared line (listing,
  `discover --add`, the TUI form): advertised levels its reviewed contracts
  match, else `{high}` with a high contract, else the plain `["high"]` line
  `models add` declares; only advertised levels no contract matches refuse.
  The shipped keyed providers without lines (`test_shipped_first_run`'s
  singleton journeys) reach a usable profile through the Providers screen:
  key → listing or a model by hand → starter; admission is optional.
- OpenRouter discovery uses one immutable `ListingCall` per request. Public and
  optional account-filtered listings get separate consent. The latter resolves
  a key by name into the Bearer header only and revalidates before secret access
  and send; limits remain 20 s/4 MiB with no redirects or retries. Merging
  preserves public rows and marks account-only additions. CLI and TUI share
  classifications and normalized facts: `stealth/` defaults to unknown family
  with maker/privacy/availability cautions, while `openrouter/` marks routers.
  Anonymous per-model lookup prefills an explicit declaration only; endpoint
  facts must agree, missing facts stay unknown, and suffixed ids remain visible
  but non-addable. No grants, bindings or defaults are automatic.
- `qualify.py` owns bounded smoke/effort/tools/stream/context plans. Forced and
  strict-auto tools are chosen and disclosed before consent, with no weaker retry.
  Evidence belongs to the current definition digest and exact client/gateway
  contracts; the immutable packaged `gateway-contract.json` carries patch hashes.
  Missing manifests yield contract-stale, not an invented pass. Inconclusive
  network outcomes never replace a pass. Context evidence keeps the highest
  successful measured bound (`checks.context.floor`) apart from the latest
  verdict, so a smaller pass or a failure never lowers the validated floor. Qualification edits no declaration or
  admission. Pool agents also run the explicit offline `probe.py` exact-client
  check; unavailable executable/isolation leaves the corresponding optional
  evidence unavailable and produces a warning, not a qualification-based block.
- `profile.agent_eligibility` decides new bindings, named bindings, workflow
  defaults, pickers and bound T2 fence selectors. Hard failures include an
  unapproved route, current provider disablement, an unusable operator ledger,
  an effort that is not a representable native agent effort, and no selector
  for the effort. Record-authority compiles preserve unchanged recorded bindings;
  new or rebound slots must satisfy current availability independently of badges. Everything else is a warning (`AgentEligibility.warnings`, shared
  with `binding_warnings` for leads and workflow defaults and
  `qualification_warnings`): a missing capability or role recommendation (an
  explicit binding overrides it), a family label outside the T1 families
  (review independence unknown), a context-window risk naming the agent class,
  shared window and trigger, and missing, failed or stale admission or
  qualification. Only transient admission, qualification, exact-client and context
  diagnostics stay out of the durable scope identity; family/independence warnings
  and the review-table text remain in it. Any printable family label is accepted;
  unknown is never independent. Agent descriptions contain role text and the
  sentinel, not operator origin or family, with YAML printable exclusions at the
  literal boundary.
- Agent context class: one rule, `profile.role_window`, decides each role's
  class and effective window from the session's compiled window/percent policy
  (`profile.WindowPolicy`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` = min(window
  ceiling, the lead set's smallest provider bound), `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
  = the percent, both in the process env and the compiled settings `env`; the
  pinned client applies them to a `[1m]` subagent like the lead). An agent on a
  1M-class line whose provider bound is at least the session window keeps
  `[1m]` and compacts at the window; one whose bound is below it uses the same
  alias without `[1m]` (the 200K class, or the exported scalar), so it never
  overflows its route; lines below the 1M class keep their own class.
  `profile.evaluate` decides it once the lead resolves (`session_policy`,
  `ResolvedLineup.policy`) for agent bindings, the agent gate's window
  condition and (through `scope`) the agent set and the workflow default;
  `compile_lineup_launch` refuses a lineup resolved for another window. The
  ceiling is `settings.Effective.window_ceiling` (in memory only: never
  `settings.json` or a record's Settings snapshot); a record-authority compile
  reads the window the record launched with (`sessions.recorded_window`), and
  an unchanged agent whose recorded selector differs from the decided one only
  by `[1m]` keeps it until resume (`profile.keep_recorded_agent_class`: doctor
  Attention "agent context class changes at the next resume", the resume diff
  row "<agent> agent class 200K → 1M"). Per-agent windows do not exist.
  The ceiling is one shared choice (`choices.json` `window_ceiling`,
  200K..800K, default 800K): `claude-multi window-ceiling` shows it,
  `claude-multi window-ceiling 400K` sets it,
  `claude-multi window-ceiling --reset` restores the default, and the
  Settings row edits it; `Runtime.current_effective` feeds it to every
  evaluation, compile, readiness and doctor pass, and a change applies at the
  next launch or resume (the resume diff row "context window 800K → 400K").
  A preparation reads it once and states that read
  (`PreparedLaunch.window_ceiling`); `Runtime.perform` and the commit
  barrier (`Runtime.revalidate_ceiling`) refuse the plan when the ceiling
  changed or `choices.json` cannot be read, so a card or confirm shown
  before a change never launches.
  The card, the lineup summary and `--print-launch` show each role's
  effective window. The exact-client probe `tests/test_agent_window_probe.py`
  proves it on the pinned client (the lead and a `[1m]` agent compact at the
  session window's trigger, the same agent without the window does not, a
  bound below the window compacts in the 200K class); its one output line
  is a stable interface: `agent window probe: PASS class=window-reaches-agents`
  only on positive evidence from all four runs, each on its intended wire
  model (an agent on the lead's model is a fallback, never evidence) and
  every agent run completed (`BOUNDARY` without the pinned client or
  isolation).
  Re-pin evidence, not a hand-maintained model list, proves dispatch, context,
  compaction and generation behavior.
- `readiness.py` owns per-slot readiness and starter planning. Unready agents warn
  (never dropped); unserved/unknown leads add no new block. Existing credential
  authority remains. LAN failures are network-scoped Info, Attention for a live
  binding, and a bounded fast refusal for a selected unreachable slot. No polling
  or SSID logic. Seed selection uses whole-lineup readiness; starter preview/apply
  never overwrites an existing profile.
- `cli/onboarding.py` adapts draft form intents to the canonical command service
  owners, not a subprocess or duplicate transaction. Screens use the adapter;
  `consent.confirm` accepts a presentation callback but **require_human remains
  mandatory** and consults real process stdio/session markers. No navigation
  writes. Providers N/E/A, Models Enter/q/e reuse validation, lock order,
  revalidation, render rollback and evidence commits. Providers E is a
  prefilled form (`views.provider_edit_form_fields`) whose answers become
  edited bytes (`commands/providers.edited_declaration`) and run the one
  edit transaction (`edit_declaration`, also the editor's); no editor runs.
  A key saved for a provider without models continues into adding one and
  offering its admission (`ConnectActions.continue_to_models`, the shared
  `admit` with Apply and retry); the command line names those steps
  (`setup.texts.NO_MODELS_NEXT`, `setup.status.without_models`). T2 K (and
  the keyed-N offer) runs the same guarded `ConnectActions.set_key` with a
  masked in-screen prompt; L retains external login guidance and lists
  saved accounts whichever transport is active (the row's other actions
  follow the active connection). Selected transports are
  used consistently by provider facts, credentials, the direct-provider
  credential check and smoke consent.
- `views.py` owns pure form fields, origin/lifecycle labels and picker eligibility;
  `tui.OnboardingForm` and `TextView` own drafting, scrolling, preview and consent
  rendering; a form field may carry its check at the field's boundary (`checks`:
  Enter stays on a refused value and says why) and whether its value may be drawn
  (`shown`: the new-provider form's key name draws only a variable name's
  characters, `secret_store.name_field_shown`, so a pasted key is never drawn). UI views do not store secrets. Anthropic-compatible is the recommended
  new-provider default; explicit kinds/presets remain authoritative; listing shape
  never selects generation protocol. Session C labels match the current scope,
  GP is compact, V preserves full details, and Direct records a resumable session
  without saving a profile. Quota guidance has one provider/action formatter.
- `OperatorProblem.error_id` maps conditions, not codes one-to-one. E14
  concerns unauthorized pool creation/override only; malformed supported-pool
  lines are E02. Action-owned E11/E12/E13/E16/E19 keep their canonical owners.
  Message changes do not change refusal semantics. Tests pin all nineteen IDs.
- Analyst/designer final text is the report deliverable; the lead persists
  it. Prompts and fixture copies stay byte-equal; reports are never agent file
  writes. The content-addressed lead-prompt argv and prompt-derived bundle hashes
  change only as consequences of these prompt bytes.
- Offline acceptance: `test_onboarding_isolation`, `test_tui_pty.OnboardingJourneyPTYTests`,
  `test_operator.OnboardingRefusalCatalogueTests`, installed-page link resolution
  driven by pyproject.toml's installed documents, and the 80×24 builders in `_tui_render`.

## 10. Operator tools and health

Read-only `explain`, `usage`, `quota --json`, `doctor --json`, `doctor
--first-run` (text and JSON), `plan`, stdout
export/import preview and `doctor --prune --preview` do not create stores or
refresh shims. Reports use the read-only SessionStore seam. `restore-2x --check`
is dispatched before Runtime and checks metadata only.
`paths.native_projects(home, environ)` recognizes the current
explicit native config root; historical originating roots remain unknown.
No transcript content is read. Generated-state preview is advisory, not a grant
to delete records, unadmitted declarations, credentials or rollback backups.

Durability is per-operation: `state`/POSIX, scope publication, proxy snapshots,
upgrade and dev publication **raise** on directory-sync failure. Upgrade
syncs the parent after rename (including rollback writes). Bytes can already be
published when that fails; transaction-specific recovery must retain this fact.
Retention is advisory: failed verified copy/rename preserves the source, and
post-publication durability failure reports failed without erasing the pin.
`lineup_log._fsync_dir` stays best effort, so unsupported directory sync does not
fail a hook. Do not merge these helpers mechanically. Linux fault injection
is no evidence for macOS; native macOS durability is not yet verified.

Typing is advisory: the code is annotated for pyright (run offline with its
bundled typeshed and no inherited `PYTHONPATH`). There is no zero-warning
gate, and a change never silences it with blanket casts, `Any` or reduced
checking. `_Heartbeat.__exit__` is explicitly `Literal[False]`: it never
swallows exceptions from the build.

Recovery policy is backend-neutral (`gateway_events.recovery_hold`), with
bounded journal/stat and manager adapters. Same-instance, identity and
non-older epoch/generation repairs do not authorize retiring auth backups.
A release candidate compares its ordered patch series patch by patch with
the previously shipped series (never whole-binary equality), re-runs the
full shipped-series own-red proof and requires the host signed-thinking
probe's PASS line with `gateway=` and `replay=` (the history matrix); the 26
essential prefixes stay unchanged. Race results are reused only for
unchanged race inputs, never across gateway binary identities.

Evidence limits on 2.1.286 (fake upstreams): the managed skill-policy probe re-derived the
alias/display-name matching, lead/subagent denies, 29 mode/allow cells and
fresh/resume precedence; typed user commands remain available. FM/FM-layers
prove zero penguin-mode and no fast speed with positive controls, not zero
credential egress: `/api/claude_cli/bootstrap` and `/api/eval/<id>` still receive
the gateway key, unchanged from 2.1.281. Rotation does not close that residual.
Auto-mode classifier traffic on the gateway is uncharacterized (two
fixture designs, no stable classifier class), and there is no positive tiny
probe capture for it either. The carried Haiku-title cloak exception and the
`/v1/alpha/search` pass-through route are open questions, not authorization
to enable hint headers. Explore's cap is meaningful only under Fable leads
on 2.1.286. A downgrade to a launcher that predates the permission default
mode needs a resume or repair to remove the compiled `defaultMode`, and
renamed Sonnet/Sol selectors need a resume there. A launcher that predates
the session-window agent rule (§9, "Agent context class") re-evaluates an
operator agent at resume with its line's own class: a `[1m]` operator line
whose provider bound is below that class's trigger (931,000 tokens at
95 %) is not agent-eligible there, so a session that binds one is refused
at resume with that reason and its record kept unchanged (no record field
can carry the newer rule to it); relaunch the session there onto a lineup
without that agent. Catalog lines resume under the older catalog's own
classes.

## 11. Keyed chat audit

Messages-first onboarding is unchanged. The trusted
`catalog.keyed_compat_audited` flag permits only HTTPS/bearer `direct-openai`
through `cliproxy-openai-compat-v1`; T2 `openai-compatible` and equivalent T1
shapes share the metadata-only closed-gate preflight and render precondition.
It is not an approval, admission, qualification or LAN-agent/Platform grant.
No new state version, catalog version, seed or gateway patch is introduced.

The shared operator/secret-store/served-plan/qualification owners remain §3.2/§9/§10.
One provider/key/nonempty model list, optional static canonical X-Client only,
the auth-aware direct-provider credential check and missing-key omission apply
to captures too. New keyed models
render canonical exact thinking levels and per-model force-mapping; alias-level
`thinking_levels` is mandatory in a keyed capture (not OAuth overlay metadata).
Missing metadata is unservable. The served projection includes safe route/capability
metadata, never key/header values. Keyed capture rendering applies to new keyed
lines only; existing catalog-direct and legacy/LAN rendering stays unchanged.

`qualify` carries bounded returned assistant history in memory only, preserves
forced versus strict-auto plans and revalidates authority at each send. No body,
reasoning, signature or key is evidence. Keyed admission smoke retains the same
verdict/reason vocabulary. The pinned client sends adaptive thinking; effort
transmission is proven at the `K12` row's cells, not at arbitrary provider capability sets.
Default unsigned-reasoning replay loss remains; is-compat and
use-max-completion-tokens are not exposed. `docs/providers/openai-compatible.md`
states the user-facing caveats.

Audit=true is guarded by `tests/fixtures/keyed-compat-audit.json` and
`test_packaging.KeyedAuditInvariantTests`: full ordered basename@sha256 manifest
(series order, 21 entries numbered 1–10 and 12–22), mandatory 3/9/10,
no 11, build definition, recipe and build tool,
source/vendor, client and exact whole-file relevant-source identities plus
complete core/audit/client/`J1` proof. The fixture is test provenance, never runtime policy. Closed and
absent flags remain regression inputs; changing the flag creates no operator state.
Any listed-source change requires fresh complete keyed proof and fixture refresh,
or audit=false; patch basenames/version equality alone are insufficient.
**Available in this release:** the regenerated complete proof passes all 34
required rows (5 core, 11 audit, 9 client and 9 journeys) against the 21-patch
gateway and pinned client. The shipped flag is true. Source paths in the
fixture are repository-relative; immutable store paths and binary digests
remain exact. The package-containment journey checks that the standalone
package payload excludes operator credentials and grants. It replaces the
removed module-export surface, not any route-safety row.
`tests/_gateway_harness.py --validate-keyed-evidence DIR` validates gateway and
client evidence; the complete inventory also requires all nine journeys.
The binding tests check the intact open proof before mutation controls, so
stale identities cannot masquerade as successful rejection tests.
Host proof uses separate private evidence dirs from gate runs; host finalizers
reject missing/skipped rows and failed subTests cannot record passed evidence.
The host gate is not the release battery or real-provider acceptance.
