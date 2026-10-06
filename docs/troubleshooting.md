# Troubleshooting

Start with `claude-multi doctor`. It reports **Ready**, **Attention**
(a warning to assess, with a remedy or optional diagnostic) or **BLOCKED** (something is
broken, and what). Plain `doctor` refreshes the helper shims as routine
housekeeping; repair, pruning and token rotation require their explicit
options. Use `claude-multi doctor --json` for a read-only report.
In the launcher, **H** shows the same report. The tables below go from a
symptom to the doctor finding, the safest fix and how to check it worked.
[CHEATSHEET.md](CHEATSHEET.md) has the short version.

Two rules come first:

- A signed-in account's credentials may be mid-save: when doctor reports a
  gateway credential save that failed or is unconfirmed, keep the gateway
  running and follow [the persistence hold](guides/gateway.md#the-persistence-hold)
  before any restart, update or uninstall.
- Never delete files under `~/.claude` to fix a session, never edit
  generated files or records by hand, and never turn TLS verification off.

## The gateway

| Symptom | Doctor says | Fix | Check |
| --- | --- | --- | --- |
| sessions cannot connect; Claude Code reports a connection refused | “local gateway: <error> — <fix>” | `claude-multi gateway start`; if it does not come up, `claude-multi gateway logs` shows why it stopped | `claude-multi gateway status` says ready; the session's next request succeeds (its token helper reconnects by itself) |
| a new installation: nothing renders or starts | the gateway is not set up yet | `claude-multi setup --step gateway` | `claude-multi gateway status` |
| automatic starts stopped after repeated crashes | three starts in ten minutes | read `claude-multi gateway logs`, fix the cause, then `claude-multi gateway start` | status shows one instance running |
| a change did not take effect | “gateway did not reload the current render (sentinel missing) — restart required” | between turns, `claude-multi gateway restart` (not while the persistence hold is active) | `claude-multi doctor` |
| the configuration on disk is stale | “the on-disk gateway config differs from a fresh render of the installed catalog” | `claude-multi providers apply` | doctor no longer reports it |
| a model is missing from the gateway | “the running gateway does not serve rendered selector” | `claude-multi gateway restart` between turns | `claude-multi doctor` |
| sessions are refused with 401 | “token mismatch” | `claude-multi-proxy init` (verifies the reload); if it persists, `claude-multi gateway restart` | doctor |
| the gateway's key files are unusable | “gateway key slots unusable” | `claude-multi doctor --rotate-token` in a terminal | doctor |
| every change is refused while a transaction runs | “the gateway is inhibited by <owner> (…)” | wait for that installer, update or service hand-off; if it was interrupted, run the command the message names | `claude-multi gateway status` shows no inhibition |
| commands refuse: another state directory | “gateway managed for root <A>; this command used <B>” | run with the managed state directory (check `XDG_STATE_HOME`) | doctor |
| `claude-multi gateway service install` on Ubuntu 23.10 or later: the gateway exited during its start | the on-demand gateway is kept; doctor does not diagnose the AppArmor restriction | keep the on-demand gateway, or ask an administrator to assess the system-wide security trade-off of allowing unprivileged user namespaces ([service requirements](guides/gateway.md#the-supervised-service-linux)) | `claude-multi gateway status`; after an approved system change, retry service install and check `claude-multi gateway service status` |
| the gateway service keeps restarting | “gateway working directory <path> holds a .env file” | move that `.env` aside, then `claude-multi gateway restart` | `claude-multi gateway service status` |
| doctor: the installed service unit differs, or a restart is pending | the unit or the package it runs changed | `claude-multi gateway service install` (refreshes it), then follow its line | `claude-multi gateway service status` |
| a held gateway refuses stop, restart, update | “gateway persistence hold” | keep it running; once you checked the credentials, `claude-multi gateway clear-hold` | `claude-multi gateway status` shows no hold |
| HTTP 400 `prompt_cache_retention is not supported on this model` | the running gateway predates the installed release | `claude-multi gateway restart` between turns | the next request succeeds |
| continuity set unreadable | “gateway continuity set unreadable” | move `~/.config/claude-multi/continuity.json` aside, then `claude-multi-proxy init` (it re-seeds the set) | doctor |

## Providers, keys and sign-ins

Connected, entitled and working are different: a key can be set but
revoked, a sign-in saved but expired, an account valid but not entitled
to a model.

| Symptom | Doctor says | Fix |
| --- | --- | --- |
| a provider shows not connected | its key is missing, or no sign-in is saved | `claude-multi providers set-key <provider>`, or `claude-multi providers sign-in anthropic` |
| an account's requests fail after a while | `invalid_grant` lines since the sign-in, or the pool has no credential record | sign in again: `claude-multi providers sign-in anthropic` (or `openai`) |
| 401 or 403 from a keyed provider | the provider refused the key | create a new key in its console and set it again |
| 402 or 429, agents of one provider stall | per-model counts of 402/429/403 from the gateway's log | in the session, `/cm fallback <provider>`, then `/reload-plugins`; for new launches pick a fallback profile |
| a provider has no models | its key is set but no model is declared | **G** → **K** continues into adding a model, or `claude-multi discover <provider>` |
| a provider or model command: “needs a terminal outside Claude Code sessions” | the command itself refuses | run it yourself in a terminal outside Claude Code; no flag waives this |
| a model answered like another one | “gateway substitution:” | the provider served a different model; check `claude-multi models` and rebind the agent if it matters |
| requests fail saying a spending or usage limit was reached | per-model counts of 402 or 429 (a limit you set yourself can answer 400) | raise or wait out the limit in the provider's console ([costs per provider](providers/api-keys.md#6-costs-and-limits)); meanwhile `/cm fallback <provider>` |
| Anthropic or OpenAI on its API key: nothing of it is served | “its selectors are not served”, with the reason: the key is not set, the route is not approved, or no model is reviewed for it | a missing key: `claude-multi providers set-key openai` (or `anthropic`); a route not approved: `claude-multi providers transport openai api-key`; or back to the account: `claude-multi providers transport openai oauth-pool` |
| OpenAI on its API key: some OpenAI models are gone | doctor lists the lines reviewed for the key route and those not served | expected: the key serves only the reviewed models; bind one of those, or switch back to the ChatGPT account |
| OpenAI on its API key: long answers cost more than expected | none: the client's output cap is not applied on that route | set a hard project spend limit with OpenAI ([providers/api-keys.md](providers/api-keys.md#openai)) |
| Qwen answers 401 | the key does not match the address | the Qwen provider takes a Token Plan key; a pay-as-you-go Model Studio key belongs to the `model-studio` preset |
| a preset or an OpenAI-compatible endpoint: “not available in this release” | a build whose keyed OpenAI-compatible audit is closed (the route is available in 1.0.0) | use an audited release, the vendor's Anthropic-compatible endpoint, OpenRouter, or a server on your network ([providers/openai-compatible.md](providers/openai-compatible.md)) |
| adding a preset again: “is already the API key of …” | another provider uses that key name | follow the message: another provider name, or share or replace the key on purpose ([the same preset twice](providers/anthropic-compatible.md#the-same-preset-twice)) |
| setting a key asks to replace it “for each of them” | the key name is shared: other providers (a preset added twice, or a provider of the earlier custom registry) use the same key | answer y to replace it for every provider named, or N to keep it ([replace a shared key](providers/api-keys.md#4-replace-remove-disconnect)) |

## Models, evidence and bindings

Route availability, the optional admission badge and optional qualification
are separate. Local readiness means configured and served, not upstream
verified. Doctor never sends model qualification requests automatically.

| Symptom | Meaning | What to do |
| --- | --- | --- |
| New · not admitted, or stale admission | the optional definition-bound badge is absent or stale; not a use block | bind the model on its usable route, or optionally use **M** → Enter / `claude-multi models admit <line>`; admission sends no inference request |
| qualification not run, failed or stale; exact-client evidence unavailable | Attention, not a lead/agent/workflow ban; the failed check remains a failure | assess the named limitation; optionally run `claude-multi models qualify <line> --agents` at a terminal outside Claude Code, with explicit human consent to the plan, default No |
| tools evidence covers auto but not forced | automatic tool use is not evidence of forced-tool support | keep the warning or explicitly test forced tools; no automatic weaker-contract retry |
| a model remains usable after `models revoke` | expected: revoke removes only the badge, leaving qualification evidence unchanged | remove its binding/declaration or disable the provider if you want to stop using it |
| a model is disabled or its route is unavailable | a real configuration restriction, not missing qualification | enable the provider deliberately or approve/fix the exact route named; there is no implicit transport or credential fallback |
| Direct can launch despite a missing credential | expected: CLI Direct warns "requests may fail"; TUI Direct asks **Launch anyway?**, default **No**, and continues only on Yes | set the key or sign in before expecting requests to work; profile/binding credential refusals remain, and neither Direct path bypasses route approval, provider disablement or an unusable selected transport |
| a family says independence unknown | an unknown or unrecognized label cannot certify independent review | keep the model if wanted; use a recognized different family if review independence matters |
| a capability/role or missing companion-grade warning | the binding overrides a recommendation | assess it; the explicit lead/agent binding remains allowed, including LAN and legacy custom lines |
| client window or compaction trigger exceeds provider bound | overflow is possible; a small provider limit does not shrink a shared client window | inspect the actual class, process window, trigger and provider bound; reduce the session ceiling where appropriate or choose another binding, never inflate declared capacity |
| LAN server observed unreachable | Attention rather than a launch veto; requests may fail | connect to its network or choose another model; unknown observations are not promoted to ready |
| nondefault effort refused for a client-effort workflow default | that separate effort cannot be represented by its one selector | choose its default effort; admission or qualification cannot waive this limit |
| a new binding needs RELAUNCH | its selector is outside the proven launch fence or its class/process policy changes | resume to apply it; being operator-added or unqualified alone does not require relaunch |
| an older release refuses a profile after rollback | unchanged storage formats do not imply support for broader family labels or newly allowed bindings | use a compatible binding or return to the newer release; never hand-edit records to bypass the refusal ([rollback limits](guides/move-machines.md#moving-to-an-older-release-or-rolling-back)) |

Unknown roles/models, malformed declarations, missing selector mappings,
unsupported native efforts, unsafe routes and damaged state still refuse.
See [models](guides/models.md) and [profile limits](guides/profiles.md#recommendations-and-technical-limits).

## Claude Code's copy

| Symptom | Doctor says | Fix | Check |
| --- | --- | --- | --- |
| launches refuse: the copy is missing or changed | “is not set up for claude-multi”, or “does not match the verified sha256” | `claude-multi setup --step claude` | `claude-multi doctor` |
| the card looks ready but a launch refuses | the card checks the copy's size and metadata only; doctor and every launch check its full hash | `claude-multi setup --step claude` | the launch |
| after an upgrade a running session still uses the old Claude Code | “runs Claude <version>, not the pinned” | `/exit`, then `claude-multi -r <id>` | the session's next start shows no notice |
| doctor: a newer Claude Code than the pin is installed | the pin was verified some days ago, or your own Claude Code is newer | nothing to do: a claude-multi release brings a newer pin | doctor shows it as information |

A session started before an upgrade can keep its old client without a
prompt-time warning: an existing lineup marker suppresses the per-prompt
client check until the session's next start. Doctor and the next resume
report it.

## Sessions

| Symptom | Fix |
| --- | --- |
| ⚠ fork waiting; “has an unresolved native fork” | Enter on the row (adopt or discard), or `claude-multi sessions resolve-fork <id> <fork-id>` |
| ! repair needed; “identity is repair-needed” | `claude-multi -r <id>`, or `claude-multi sessions relink-runtime <id> <runtime-id>` |
| ● or ◐ but the session is gone | `claude-multi sessions mark-ended <id>` (all: `--all-dead`) |
| “scope differs from the record-authoritative compile” | `claude-multi doctor --repair <id>` |
| “session record <id> is unreadable” | `claude-multi sessions show <id>` names the error; never edit the record; forget a disposable session with `claude-multi sessions forget <id>` |
| resume says the session does not exist | `claude-multi -r <id>` (it resumes from the recorded directory) |
| a session is missing from the list | **C** on the sessions screen (all directories) |

More: [guides/sessions.md](guides/sessions.md).

## Inside a session

| Symptom | Fix |
| --- | --- |
| `/cm` fails: `claude-multi` not found | `claude-multi` must be on the session's PATH |
| `/cm` changed nothing for new agents | type `/reload-plugins`, then start agents fresh |
| `/model` refuses a model | it is outside the lead set: relaunch with a profile whose lead is that model |
| an MCP server cannot find its API key | managed sessions remove every `*_API_KEY` variable except the names you keep: add its name in Settings (**O**) → kept environment; provider keys are never kept. **V** on the card shows kept and removed names, doctor too. Applies at the next launch or resume |
| implementers “unavailable here” | start the session in a Git repository (`git init`) |
| Remote Control, `/schedule` or claude.ai connectors are missing | expected in managed sessions; use plain `claude` for these |

## Platforms and files

| Symptom | Fix |
| --- | --- |
| WSL: the gateway is gone after closing every terminal | expected: WSL stopped; the next launch starts it ([install/windows-wsl2.md](install/windows-wsl2.md#when-wsl-shuts-down)) |
| WSL: Claude Code on Windows and in WSL see different sign-ins | WSL: the Windows-side and WSL-side Claude configurations are separate — sign-ins, settings and sessions of Claude Code on Windows are not seen here, and the reverse |
| WSL: “is on a Windows drive” | move the state directory into the Linux file system |
| this account's state belongs to another installation | use that installation, or follow the printed fix to move the account ([install/linux.md](install/linux.md#one-installation-per-account)) |
| a certificate error | [guides/networking.md](guides/networking.md#certificate-errors) |
| “session record directory is unavailable; records are unknown” | the state directory must be yours, mode 0700; fix ownership or mode, then `claude-multi doctor` |
| a write fails on a full or read-only disk | free space or make the path writable, then rerun the command; writes are atomic and leave the old file in place |
| the full-screen interface misbehaves | `claude-multi --line` uses plain lines; `--no-color` (or `NO_COLOR`) turns colour off |

## The launch card

A card row that does not fit keeps its remedy (the part after the dash)
and shortens the middle, so an 80-column terminal still shows what to do.
**V** shows every row in full.

| The card shows | Fix |
| --- | --- |
| “! CLAUDE_CONFIG_DIR ignored: settings, MCP, memory, resume use ~/.claude — V” | managed sessions read settings, MCP servers, memory and resume from `~/.claude`; **V** says which values that affects. Unset the variable, or keep it for your own `claude` |
| a new model is not admitted | the badge is optional: bind it on a usable route, or **M** → Enter to attest locally; see any separate route/credential remedy |
| “implementers unavailable: not a Git repository here (git init enables them)” | launch from a Git work tree, or `git init` |
| the lead or agents with “no <pool> OAuth credential record” | sign in where the row says: **G**, then **L** on the account provider (on a resume card, the row names the way back to a launch card first) |
| **H**: a key to save, “G (providers) → K on <provider>” | **G**, then **K** on that provider (from a terminal: `claude-multi providers set-key <provider>`); on a resume card, the report first names the way back to a launch card, then **S** resumes |
| the profile editor: what a role is for | the row of the selected lead or agent shows its role, grade and description; the model picker shows it as its subtitle and in **V** |
