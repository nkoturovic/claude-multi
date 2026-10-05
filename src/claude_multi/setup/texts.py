"""The setup layer's texts: result lines, line-mode prompts and command texts.

``{name}`` marks a format field. The full-screen texts that only the TUI
shows live in :mod:`claude_multi.cli.text`; the strings here are shared by
the command line, ``claude-multi setup`` and the full-screen results.
"""

from __future__ import annotations

from claude_multi import account_pools

# ------------------------------------------------------------------ general
SETUP_HEADER = ("claude-multi setup\nclaude-multi is an independent tool, not affiliated with Anthropic or OpenAI. "
                "Nothing is saved until you confirm.\nTip: claude-multi opens the same steps full-screen (W).")
SETUP_DONE = "Ready. Start a session: claude-multi"
SETUP_CANCELLED = ("cancelled (Ctrl-C) — finished steps are kept; claude-multi setup continues where you "
                   "stopped")
SETUP_NEEDS_TERMINAL = "claude-multi setup: needs a terminal — or --status, or --answers FILE"
# A launch in line mode while nothing is connected.
LINE_NOT_CONNECTED = "Nothing is connected yet: no provider has an API key or a sign-in."
LINE_SETUP_NOW = "Set up now (claude-multi setup)? [Y/n] "
LINE_SETUP_FIX = "claude-multi setup"
SETUP_PREFLIGHT_STOP = "Fix this first, then run claude-multi setup again."
SETUP_GATEWAY_STARTING = "Starting the local gateway…"
SETUP_CLAUDE_QUESTION = "Install it? [y/N] "
SETUP_PROVIDERS_QUESTION = "Choose a number (Enter: next step, q: stop): "
SETUP_TEST_QUESTION = "Test your connected providers (one request each; may be billed)? [y/N] "
SETUP_KEEP_PROFILE = "Keep {name}? [Y/n] "
SETUP_SAVE_STARTER = "Save as {name} and make it your default? [y/N] "
SETUP_STARTER_SAVED = "Saved {name} and made it your default profile."
SETUP_STARTER_INTRO = ("Nothing shipped fits what is connected. claude-multi can build a profile from what is "
                       "connected (no request is sent; nothing is saved yet):")
SETUP_STARTER_NO_LEAD = ("No connected provider has a model that can lead yet — add models (claude-multi "
                         "discover ID --add), or connect another provider.")
# A connected provider without models: the command-line steps that make it
# usable (the TUI's Providers screen walks the same steps after a key).
NO_MODELS_NEXT = ("next: {id} has no models yet — list them (claude-multi discover {id}), add one "
                  "(claude-multi discover {id} --add WIRE, or claude-multi models add {id} WIRE …), admit it "
                  "(claude-multi models admit KEY), then make a profile (claude-multi setup --step profile)")
SETUP_PROFILE_FIT = "Your profile: {name} ({why})"
SETUP_PROFILE_WAITING = "Connect a provider first: claude-multi setup --step providers"
SETUP_NOT_INTERACTIVE_STEP = "{step}: skipped — needs a terminal outside Claude Code sessions"
SETUP_MENU_UNAVAILABLE = "     {label} — {note}"
SETUP_MENU_ITEM = "  {n:>2}. {label}  [{state}]"
SETUP_MENU_GROUP = "  {title}"
SETUP_WRONG_CHOICE = "Not one of the numbers shown — nothing changed."

STATUS_ROW = "  {n} {step:<10} {mark} {text}"
STATUS_TEXT = {
    ("preflight", "done"): "ready",
    ("preflight", "blocked"): "{n} problem(s) — claude-multi doctor --first-run",
    ("claude", "done"): "Claude Code {version}, verified copy",
    ("claude", "todo"): "Claude Code {version} not installed — claude-multi setup --step claude",
    ("claude", "blocked"): "{problem}",
    ("gateway", "done"): "running",
    ("gateway", "todo"): "starts when needed",
    ("gateway", "setup"): "not set up yet — claude-multi setup --step gateway",
    ("gateway", "blocked"): "{problem}",
    ("providers", "done"): "{connected}",
    ("providers", "todo"): "nothing connected yet — claude-multi setup --step providers",
    ("test", "optional"): "optional",
    ("profile", "waiting"): "after a provider is connected",
    ("profile", "done"): "{name} — {why}",
    ("profile", "todo"): "no profile fits yet — claude-multi setup --step profile",
    ("check", "done"): "ready",
    ("check", "todo"): "first-run checks — claude-multi doctor --first-run",
}
STATUS_MARKS = {"done": "✓", "todo": "·", "setup": "·", "waiting": "·", "optional": "—", "blocked": "✗",
                "attention": "!"}
STATUS_MISSING = {"providers": "✗"}

# ------------------------------------------------------------------ results of a gateway reload
RELOAD_TEXT = {
    "reloaded": "the gateway reloaded",
    "reloaded+models": "the gateway reloaded; {n} {display} model(s) available",
    "reloaded+none": "the gateway reloaded; no {display} models yet — A adds models",
    "restart_required": "the gateway needs a restart to use it — {restart_hint}",
    "down": "the gateway is not running; it uses this when it starts",
    "token_mismatch": "the gateway did not accept the local gateway key — H shows the fix",
    "not-needed": "nothing to reload",
}
RENDER_REFUSED = "Nothing changed — the gateway configuration was refused: {reason}"
UNDO_INCOMPLETE = ("Putting the previous state back did not complete ({detail}) — check with "
                   "claude-multi setup --status and claude-multi doctor")

# ------------------------------------------------------------------ API keys
KEY_SAVED = "{display} API key saved ({n} chars) — {reload}"
KEY_REMOVED = "{display} API key removed — {reload}"
KEY_EMPTY = "Nothing saved — the key was empty."
KEY_SHAPE = "Nothing saved — a key holds only letters, digits and . _ ~ + / = @ : -"
# An account provider that also takes an API key (Anthropic, OpenAI): the
# key is a transport of its own, chosen with Enter or `providers transport`.
KEY_ACCOUNT_TRANSPORT = "{display} uses your {account} or an {display} API key — Enter on {display} chooses."
KEY_ACCOUNT_TRANSPORT_LINE = ("{display} uses your {account} or an {display} API key — "
                              "claude-multi providers transport {id} api-key")
KEY_ANTHROPIC_TRANSPORT = KEY_ACCOUNT_TRANSPORT.format(display="Anthropic", account="Claude account")
KEY_ANTHROPIC_TRANSPORT_LINE = KEY_ACCOUNT_TRANSPORT_LINE.format(display="Anthropic", account="Claude account",
                                                                 id="anthropic")
# Its API-key transport when this build does not offer it yet.
KEY_TRANSPORT_CLOSED = ("{display} API keys are not available in this build yet — sign in with your {account} "
                        "(Enter) or use OpenRouter.")
KEY_TRANSPORT_CLOSED_LINE = ("{display} API keys are not available in this build yet — sign in with your "
                             "{account} (claude-multi providers sign-in {id}) or use OpenRouter.")
KEY_NOT_NEEDED = "{display} needs no key"
KEY_UNKNOWN_PROVIDER = "{id} is not a provider here — claude-multi providers list"
KEY_PROMPT = "{display} API key (input hidden): "
KEY_REPLACE_PROMPT = "A {display} API key is set ({n} chars). Replace it? [y/N] "
# A saved key that other providers use too: replacing it changes theirs, so
# every provider using it is named first.
KEY_SHARED_REPLACE_PROMPT = "{name} ({n} chars) is the API key of {providers}. Replace it for each of them? [y/N] "
KEY_SHARED_REPLACED_NOTE = "{name} is the API key of {providers}: the new key replaces it for each of them."
KEY_KEPT = "{display} API key kept — nothing changed"
KEY_NOT_SET = "{display} has no saved API key"
KEY_FILE_WHAT = "the API-key file in use"
KEY_FILE_SELECTION_WHAT = "what the gateway serves with the selected key file"
KEY_NAME_SHAPE = "a key name uses A–Z, 0–9 and _ only"
KEY_NAME_NOT_ITS = "{id} uses the API key {key}, not {name} — claude-multi providers remove-key {id}"
KEY_NAME_IN_USE = "{name} is the API key of {other} — claude-multi providers remove-key {other}"
KEY_IN_USE = "switch {display} back to your {account} first (Enter on {display}), then remove the key"
KEY_IN_USE_LINE = ("switch {display} back to your {account} first (claude-multi providers transport {id} "
                   "oauth-pool), then remove the key")
KEY_ANTHROPIC_IN_USE = KEY_IN_USE.format(display="Anthropic", account="Claude account")
KEY_ANTHROPIC_IN_USE_LINE = KEY_IN_USE_LINE.format(display="Anthropic", account="Claude account", id="anthropic")
REMOVE_KEY_BODY = ("Deletes {name} from {path}; the gateway reloads without it.\n"
                   "{profiles_line}\n"
                   "If the key may have leaked, also revoke it in your {display} account.")
PROFILES_LOSE = "Profiles that use {display} stop being connected: {names}."
PROFILES_NONE = "No profile uses {display}."
REMOVE_KEY_QUESTION = "Remove it? [y/N] "

# ------------------------------------------------------------------ account or API-key transport
TRANSPORT_TO_KEY_BODY = ("{n} model selector(s) move to {origin} — same names, never both at once.\n"
                         "The key is sent only to {origin} (header {header}); its value is never shown.\n"
                         "Your {account} sign-in stays on this computer but is not used.\n"
                         "{live_line}")
# A key route that serves only the models reviewed for it.
TRANSPORT_KEY_REVIEWED = ("Only models reviewed for {display} API keys are served on it: {names} ({n} of {total} "
                          "{display} model lines); the others stop being served until you switch back.")
TRANSPORT_TO_ACCOUNT_BODY = ("{n} model selector(s) move back to your {account} sign-in.\n"
                             "The {display} API key stays saved unless you remove it next.\n"
                             "{live_line}")
LIVE_LINE = "Running sessions switch at their next request: {ids}."
TRANSPORT_QUESTION = "Switch? [y/N] "
TRANSPORT_SWITCHED_KEY = "{models} now use your {display} API key — {reload}"
TRANSPORT_SWITCHED_ACCOUNT = "{models} use your {account} sign-in again — {reload}"
TRANSPORT_ALREADY = "{id} already uses the {choice} transport"
TRANSPORT_REMOVE_KEY = "Also remove the saved {display} API key ({name})? [y/N] "
# What a transport moves, by provider (the default is "<display> models").
TRANSPORT_MODELS = {"anthropic": "Claude models"}

# ------------------------------------------------------------------ route approval
APPROVE_BODY = ("Your API key {name} ({present}) is sent only to:\n  {origin}\n  as {auth_text}\n"
                "Model list: {listing}\nNothing is sent now; the gateway reloads with this route.")
APPROVE_PREVIOUS = "Before: {previous_origin}"
APPROVE_QUESTION = "Approve? [y/N] "
APPROVED = "Approved {id} → {origin} — {reload}"
APPROVE_KEYLESS = "{id} needs no key, so there is no route to approve"
APPROVE_ALREADY = "{id}'s route is already approved ({origin})"
APPROVE_PRESENT = {True: "saved", False: "not saved yet"}
AUTH_TEXT = {"header": "header x-api-key", "bearer": "Authorization: Bearer"}

# ------------------------------------------------------------------ apply
APPLY_BODY_TAIL = "Nothing is sent to a provider; the local gateway reloads."
APPLIED = "Applied — {reload}"
NOTHING_TO_APPLY = "Nothing to apply — the gateway already serves your setup."
# Apply when the configuration on disk is current but the gateway does not serve it.
APPLY_NOT_SERVED = "Your setup is unchanged, but the running gateway does not serve all of it yet."

# ------------------------------------------------------------------ the gateway's outbound proxy
PROXY_PLAN_SET = "The gateway's outbound proxy becomes {proxy}."
PROXY_PLAN_DIRECT = "The gateway connects directly (no outbound proxy)."
PROXY_PLAN_RELOADS = "The running gateway reloads with it; nothing is sent to a provider."
PROXY_PLAN_NEXT_START = "The gateway uses it from its next start."

# ------------------------------------------------------------------ remove a provider you added
BLOCKER = {
    "admitted": ("model {key} is admitted — M, select it, Enter revokes",
                 "claude-multi models revoke {key}"),
    "bound-profile": ("model {key} is used by profile {p} ({slots}) — P, select {p}, E rebinds",
                      "claude-multi profile edit {p}"),
    "bound-binding": ("model {key} is used by named binding {b} — E (edit profile) → N",
                      "rebind named binding {b} in the profile editor"),
    "live": ("session {id8} is running on it — S, select it, E ends it",
             "claude-multi sessions stop {id8}"),
    "unknown-liveness": ("session {id8} may still be running (◐) — S shows it; end it where it runs",
                         "claude-multi sessions mark-ended {id8}"),
}
REMOVE_PROVIDER_BODY = ("Deletes your declaration {file} and withdraws its route approval; the gateway "
                        "reloads. Running sessions are not affected.")
REMOVE_PROVIDER_QUESTION = "Remove {file}? [y/N] "
REMOVE_PROVIDER_REFUSED = "refused — {fixes}"
PROVIDER_REMOVED = "Removed {id} — {reload}"
REMOVE_KEY_TOO = "Also remove its API key {name}? It is no longer used."
REMOVE_KEY_TOO_QUESTION = "Also remove its API key {name}? [y/N] "
KEY_KEPT_AFTER_RM = "the API key {name} was kept — claude-multi providers remove-key {id} --name {name}"
NOT_YOUR_PROVIDER = "{id} ships with claude-multi and cannot be removed — claude-multi providers disable {id}"
NO_DECLARATION = "{id} is not a provider you added — claude-multi providers list"

# ------------------------------------------------------------------ remove a model line you added
REMOVE_LINE_BODY = "Deletes it from {file}; the gateway reloads. Sessions that already use it keep it until they end."
REMOVE_LINE_QUESTION = "Remove {key}? [y/N] "
REWRITE_LINE = "{kind} {name} · {slot}: {key} → {successor}"
REWRITE_TAIL = "The model {key} is removed after these changes."
REWRITE_QUESTION = REWRITE_TAIL + " Proceed? [y/N] "
LINE_REMOVED = "Removed {key}{rewrites_note} — {reload}"
LINE_REWRITES_NOTE = " and moved {n} binding(s) to {successor}"
LINE_NO_SUCCESSOR = "no other model can take {key}'s place in {places} — E edits those profiles first"
LINE_NEEDS_SUCCESSOR = "{key} is used by {places} — rerun with --successor KEY ({candidates})"
LINE_NOT_YOURS = "{key} ships with claude-multi and cannot be removed — claude-multi models revoke {key}"
LINE_UNKNOWN = "no providers.d declaration of {key}"
LINE_BAD_SUCCESSOR = "{successor} cannot take {key}'s place: {reason}"

# ------------------------------------------------------------------ your own endpoint and LAN server
ENDPOINT_PREVIEW = ("{id}: {kind_label} at {base_url}\nThe key {secret_name} is sent as {auth_text} "
                    "only to {origin}. Family: {family}.\nNothing is sent to {host}. Next: approve where "
                    "the key goes, then type the key.")
ENDPOINT_ADDED = "Added {id} — {reload}"
LAN_PREVIEW = ("{id}: OpenAI-compatible server at {base_url} (no key). Its models are lead-only in this "
               "release: agents need a reviewed route. Nothing is sent now.")
KIND_LABELS = {"anthropic-compatible": "Anthropic-compatible, API key",
               "openai-compatible": "OpenAI-compatible, API key",
               "openai-compatible-lan": "server on your network"}
OPENAI_COMPAT_CLOSED = ("an OpenAI-compatible endpoint with an API key is not available in this release — use "
                        "Anthropic-compatible if your vendor offers it")

# ------------------------------------------------------------------ on and off
TOGGLED = "{id} {state} — applies at the next launch or resume; running sessions keep their routes"
TOGGLE_ALREADY = "{id} is already {state}"

# ------------------------------------------------------------------ provider picker
PICKER_LABELS = {
    "anthropic:account": "Claude account — sign in (personal use)",
    "anthropic:api-key": "Anthropic API key — billed per token",
    "openai:account": "ChatGPT account — sign in (personal use)",
    "openai:api-key": "OpenAI API key — billed per token",
    "openrouter:api-key": "OpenRouter API key — models from many vendors",
    "api-key": "{display} API key",
    "api-key-no-models": "{display} API key — no models yet",
    "own": "{display} — {kind_label}",
    "other:anthropic-compatible": "Anthropic-compatible endpoint (your vendor, with an API key)",
    "other:openai-compatible": "OpenAI-compatible endpoint (with an API key)",
    "other:lan": "Server on your network (OpenAI-compatible, no key)",
    "preset:lan": "{display} server (OpenAI-compatible, no key)",
    "preset": "{display} API key — {kind}",
    "preset-closed": "{display} API key — {kind}, not available in this release",
}
# A reviewed preset: a vendor's documented endpoint over the generic route,
# never validated by claude-multi (its support label).
PRESET_STATE = "preset"
PRESET_KIND_TEXT = {"anthropic-compatible": "Anthropic-compatible", "openai-compatible": "OpenAI-compatible"}
PRESET_PREVIEW_HEAD = "{display}: a preset from the vendor's documentation, not tested by claude-multi."
PRESET_NAME_PROMPT = "Name for this provider (a–z, 0–9, -)"
# One more provider from a preset whose API key another provider already
# uses: its own key (the default), the saved key shared, or that key replaced
# for every provider using it (confirmed, naming them).
PRESET_KEY_SHARED = "{name} is already the API key of {providers}."
PRESET_KEY_CHOICES = ("A new key for {id}, saved as {own}",
                      "The saved key {name}, shared with {providers}",
                      "Replace the saved key {name}, for {providers} too")
PRESET_KEY_ITEM = "  {n:>2}. {label}"
PRESET_KEY_QUESTION = "Which API key does {id} use?"
PRESET_KEY_OWN = "{id} gets its own API key {name}; {preset_name} stays the key of {providers}."
PRESET_KEY_REUSE = "{id} uses the saved key {name}, shared with {providers}; no key is typed."
PRESET_KEY_REPLACE = "The key you type replaces {name} for {providers} too."
PRESET_KEY_REPLACE_QUESTION = "Replace {name}, the API key of {providers}? [y/N] "
PRESET_KEY_NOT_SHARED = "no other provider uses {name} — {id} takes it as its own key"
PRESET_KEY_NOTHING_TO_REUSE = "no key is saved as {name} — type a new key for {id} instead"
PRESET_KEY_NAME_PROBLEM = ("{id} needs its own API key name and cannot have one ({problem}) — choose a shorter "
                           "provider name")
PRESET_KEY_REUSE_TAKES_NO_KEY = "sharing the saved key takes no new key — nothing changed"
PRESET_KEY_REPLACE_NEEDS_KEY = "replacing the shared key needs the new key — nothing changed"
KEY_NAME_TAKEN = "{name} is already the API key of {providers} — choose another name for this provider"
KEY_USERS_WHAT = "the providers using {name}"
KEY_TARGET_WHAT = "{id} and its API key {name}"
KEY_SAVED_WHAT = "the saved key {name}"
# An account provider's API-key entry this build does not offer yet, or one
# with no model reviewed for its key route.
PICKER_KEY_CLOSED_LABEL = "{display} API key — not available yet"
KEY_CLOSED_NOTE = "use your {account} or OpenRouter"
OPENAI_KEY_NOTE = KEY_CLOSED_NOTE.format(account="ChatGPT account")
KEY_ROUTE_NO_MODELS_NOTE = "no model is reviewed for it yet"
OPENAI_COMPAT_CLOSED_LABEL = "OpenAI-compatible endpoint with a key — not available in this release"
OPENAI_COMPAT_CLOSED_NOTE = "use Anthropic-compatible if your vendor offers it"
SIGNIN_CLOSED_NOTE = "not offered by this build — use an Anthropic API key"
PICKER_OTHER = "Other"
PICKER_YOURS = "Your providers"
FAMILY_PER_MODEL = "per model"
NO_MODELS_NOTE = "no models yet"
IN_USE = "in use"

# The credential vocabulary.
STATE_TEXT = {
    "connected": "connected",
    "key-set": "key set",
    "key-missing": "key missing",
    "key-invalid": "key invalid",
    "signed-in": "signed in",
    "not-signed-in": "not signed in",
    "sign-in-unknown": "sign-in unknown",
    "keyless": "keyless",
    "route-unapproved": "not approved",
    "route-changed": "changed",
    "off": "off",
}

# ------------------------------------------------------------------ account sign-in
# Each account pool's acknowledgement text, account kind and sign-in host
# are its data (claude_multi.account_pools; the acknowledgement through
# setup.signin.ack_text).
ACK_WORD = "personal"
ACK_WRONG = "Not signed in — type personal to continue (nothing changed)."
ACK_PROMPT = "Type personal to continue: "
ACCOUNT_KINDS = {name: pool.display for name, pool in account_pools.pools().items()}
SIGNIN_HOSTS = {name: pool.sign_in.host for name, pool in account_pools.pools().items()}

# What the terminal shows before a sign-in runs, per method ({kind}: the
# pool's account kind, {host}: its sign-in host).
SIGNIN_HEADER_BROWSER = (
    "claude-multi — {kind} sign-in (personal use)\n"
    "A browser page opens at {host}. Approve the sign-in there; it finishes here.\n"
    "If no page opens, open the address printed below. If the browser ends on a page that cannot\n"
    "load, copy that page's full address and paste it here when asked.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")
SIGNIN_HEADER_ADDRESS = (
    "claude-multi — {kind} sign-in (personal use)\n"
    "Open the address printed below in a browser on any device and approve the sign-in. The browser\n"
    "then ends on a page that cannot load: copy that page's full address and paste it here when asked.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")
SIGNIN_HEADER_DEVICE = (
    "claude-multi — {kind} sign-in (personal use)\n"
    "An address and a one-time code are printed below. Open the address on any device, enter the code\n"
    "and approve; the sign-in finishes here.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")
SIGNIN_OK = "Signed in: {account} ({kind}). Its models are available within a few seconds."
SIGNIN_MULTI = ("{n} {kind}s are signed in on this computer; requests may use any of them. L → Sign out "
                "removes all of them.")
SIGNIN_UNCHANGED = "Not signed in — the sign-in did not finish. Nothing changed. L tries again."
SIGNIN_UNCHANGED_LINE = ("Not signed in — the sign-in did not finish. Nothing changed. Try again: "
                         "claude-multi providers sign-in {id}")
SIGNIN_CANCELLED = "Sign-in cancelled — nothing changed."
SIGNIN_PORT = ("Not signed in — the sign-in's local port is in use (another sign-in still open?). Close it "
               "and try again.")
SIGNIN_FAILED = "Not signed in — {detail}"
SIGNIN_NOT_ACCOUNT = "{id} uses an API key — claude-multi providers set-key {id}"
SIGNIN_STRICT = ("Claude account sign-in is not offered by this build of claude-multi — use an Anthropic "
                 "API key: claude-multi providers transport anthropic api-key")
SIGNIN_WSL_OPEN = "Opening the address in your Windows browser…"
SIGNIN_WSL_OPEN_FAILED = "Could not open a browser — open the address printed above yourself."
LOGIN_REFUSED = ("claude-multi-proxy {command}: sign in with claude-multi providers sign-in {provider} (it "
                 "asks for the personal-use confirmation first)")
LOGIN_STRICT = ("claude-multi-proxy {command}: Claude account sign-in is not offered by this build of "
                "claude-multi")

# ------------------------------------------------------------------ sign-out
SIGNOUT_BODY = ("Signed in: {accounts}\n"
                "The sign-in record(s) move to {backup} (kept, not deleted); the gateway stops using "
                "them within a few seconds.\n{profiles_line}")
SIGNOUT_QUESTION = "Sign out? [y/N] "
SIGNED_OUT = "Signed out of your {kind}; the record(s) are kept in {backup}."
SIGNED_OUT_STILL = ("Signed out; the gateway still lists {kind} models — it stops using them at its next "
                    "reload.")
SIGNED_OUT_DOWN = "Signed out; the gateway is not running — it starts without this sign-in."
SIGNED_OUT_REAPPEARED = "A record that came back during sign-out was moved too: {names}."
SIGNED_OUT_LEFT = ("Records that came back during sign-out could not be moved ({reason}) and stay signed in: "
                   "{names}. Sign out again: claude-multi providers sign-out {id}.")
SIGNED_OUT_KEPT_BACK = ("Records that came back during sign-out could not be put back and stay in {backup} "
                        "(kept): {names}. To sign in with them again, move them from {backup} back into "
                        "{auth_dir} where no newer record took their place.")
SIGNOUT_UNDO_INCOMPLETE = ("Not signed out — the sign-out stopped part-way ({reason}) and could not put every "
                           "record back: {names} stay in {backup} (kept; a record written again meanwhile keeps "
                           "its place). Sign out again with claude-multi providers sign-out {id}, or move them "
                           "from {backup} back into {auth_dir} where no newer record took their place.")
SIGNED_OUT_UNDO = ("To sign in again: L. To restore this sign-in instead: move the files from {backup} back "
                   "into {auth_dir}.")
SIGNED_OUT_UNDO_LINE = ("To sign in again: claude-multi providers sign-in {id}. To restore this sign-in "
                        "instead: move the files from {backup} back into {auth_dir}.")
NOT_SIGNED_IN = "Not signed in on this computer."
SIGNOUT_HOLD = ("Not signed out — a sign-in save is not confirmed yet ({reason}); wait a minute and try again. "
                "Nothing was moved.")
SIGNED_OUT_RELOAD = "The gateway did not reload cleanly: {detail}."
SIGNED_OUT_INCOMPLETE = ("Sign-out is not complete yet: once the gateway runs and reloads, check with "
                         "claude-multi providers sign-out again (it reports when nothing is left).")

# ------------------------------------------------------------------ connection test
TEST_CONSENT_HEAD = "claude-multi will make {n} request(s), one per provider:"
TEST_CONSENT_ITEM = ("  {i}. {display}: model {alias} → {upstream}\n"
                     "     auth: {auth_text}\n")
TEST_CONSENT_TAIL = ("  why: connection test · about 40 input tokens each · 120 s and 256 KiB caps · no retries\n"
                     "  Providers may bill these requests.\nProceed? [y/N] ")
TEST_NO_SERVED = "{display}: no served model to test — A adds models, or P applies"
TEST_NO_SERVED_LINE = ("{display}: no served model to test — claude-multi discover {id} --add, or "
                       "claude-multi providers apply")
TEST_RESULT = {
    "pass": "{display}: works ({ms} ms)",
    401: "{display}: the key was refused — K replaces it",
    403: "{display}: the key was refused — K replaces it",
    "account-refused": "{display}: the sign-in was refused — sign in again with L",
    402: "{display}: the provider reports no credit — add credit in your {display} account",
    404: "{display}: this model is not available to your key",
    429: "{display}: rate limited — try again later",
    "5xx": "{display}: the provider had an error — try again later",
    "timeout": "{display}: no answer within 120 s — try again later",
    "down": "{display}: the gateway is not running — step 3 (or H) starts it",
    "other": "{display}: the test did not pass ({reason})",
}
TEST_AUTH_KEY = "header from {name} (value not shown)"
TEST_AUTH_ACCOUNT = "your {kind} sign-in, held by the gateway (not shown)"
TEST_AUTH_KEYLESS = "none (a server on your network)"
TEST_NOTHING = "Nothing to test — no connected provider serves a model yet."
TEST_NOT_CURRENT = "Nothing sent — the gateway is not serving your current setup ({reason}). P applies it."
TEST_NOT_CURRENT_LINE = ("Nothing sent — the gateway is not serving your current setup ({reason}). "
                         "Apply it: claude-multi providers apply")
TEST_STALE = ("{display}: the connection changed after you agreed to the test — nothing was sent to it; "
              "run the test again")

# ------------------------------------------------------------------ key file
KEYS_FILE_QUESTION = "Use {path} for API keys (it stays where it is; claude-multi never copies it)? [y/N] "
KEYS_FILE_SET = "API keys now come from {path} ({n} key name(s) found)."
KEYS_FILE_OVERRIDDEN = ("CLAUDE_MULTI_SECRET_ENV is set in this environment; it is used instead of the "
                        "selected file until it is unset.")
KEYS_FILE_SERVICE = ("The gateway service reads {path} too, whenever it starts or reloads (it never reads "
                     "CLAUDE_MULTI_SECRET_ENV).")

# ------------------------------------------------------------------ answers file
ANSWERS_SECRET = "answers file holds what looks like a key at {path} — use api_key_file"
ANSWERS_SIGN_IN = "account sign-ins need a person at the terminal: claude-multi providers sign-in {id}"
ANSWERS_TEST = ("tests send requests to providers and need a person at the terminal: claude-multi providers "
                "test {id}")
ANSWERS_INLINE_KEY = "inline keys are refused — use api_key_file"
ANSWERS_SKIPPED = "skipped {id}: needs a terminal — rerun on one, or claude-multi providers {verb}"
ANSWERS_QUESTION = "Apply this plan? [y/N] "
ANSWERS_PLAN_HEAD = "claude-multi setup --answers — plan (nothing has been written yet)"
ANSWERS_SUMMARY = "applied {applied}, skipped {skipped}, failed {failed}"
ANSWERS_NOT_JSON = "the answers file is not valid JSON: {reason}"
ANSWERS_KEY_FILE_BAD = "providers[{index}] ({id}): {reason}"
ANSWERS_KEYS_FILE_BAD = "keys_file: {reason}"
ANSWERS_ITEM_HEAD = "{id}: {what}"
ANSWERS_ITEM_WHAT = "what the plan showed for {id}"
ANSWERS_CANNOT_RUN = "✗ cannot run: {reason}"
ANSWERS_WRITES = "  writes: {files}"
ANSWERS_CLAUDE_COPY = "Claude Code {version}: copy {source} (checked by size and sha256)"
ANSWERS_CLAUDE_ANY = "a matching install on this computer"
ANSWERS_CLAUDE_DOWNLOAD = "  if none matches, download it:"
ANSWERS_CLAUDE_NO_DOWNLOAD = "  no download (accept_download is not true)"
ANSWERS_DOWNLOAD_WHAT = "the Claude Code download the plan showed"
ANSWERS_GATEWAY = "start the local gateway"
ANSWERS_KEYS_FILE_HEAD = "use {path} for API keys"
ANSWERS_KEYS_FILE = "use {path} for API keys ({n} key name(s) found; it stays where it is)"
ANSWERS_KEY = "{id}: import the API key {name} from {source} into {path}"
ANSWERS_KEY_REPLACES = "it replaces the saved {display} API key ({n} chars)"
ANSWERS_KEY_SHARED = "{name} is the API key of {providers}: it is replaced for each of them"
ANSWERS_KEY_SAVED = "uses the saved key {name} in {path}"
ANSWERS_KEY_MISSING = "the key {name} is not saved in {path} — add api_key_file"
ANSWERS_TOGGLE = "turn {id} {state} — applies at the next launch or resume; running sessions keep their routes"
ANSWERS_ENDPOINT = "declare {id}: {kind} at {base_url} (family {family}) and approve its route:"
ANSWERS_TRANSPORT = "{id}: use the {choice} transport"
ANSWERS_STARTER = ("profile: build one from the providers connected by then, save it as starter and make it "
                   "your default")
ANSWERS_STARTER_PLAN = ("profile: save {name}, built from the providers connected now and those this file "
                        "connects with keys, and make it your default:")
ANSWERS_STARTER_DEFAULT_STALE = ("{name} was saved, but your choices changed while you were deciding, so it is not "
                                 "your default — claude-multi profile default {name}")
ANSWERS_PROFILE = "profile: make {name} your default"
ANSWERS_DEFAULT = "default profile: {name}"
ANSWERS_DEPENDS_KEY = ("providers[{index}] ({id}): this key is for {id}, which only providers[{first}] of this file "
                       "declares — put api_key_file in the '{id}' endpoint entry")
ANSWERS_DEPENDS_SERVER_KEY = ("providers[{index}] ({id}): {id} is a server on your network that only "
                              "providers[{first}] of this file declares, and it takes no key — remove this entry")
ANSWERS_DEPENDS = ("providers[{index}] ({id}): {id} is declared only by providers[{first}] of this file, and an "
                   "item never sees what another item of the same file changes — set this in a second setup run "
                   "after this one")
ANSWERS_DEPENDS_TOGGLE = ("providers[{index}] ({id}): enabled is applied before this entry declares {id} — turn "
                          "it on or off after this setup: claude-multi providers enable|disable {id}")

# ------------------------------------------------------------------ first-run checks
FIRSTRUN_INFO = "Managed sessions sign in to the local gateway; your plain claude login and settings are not changed."
FIRSTRUN_READY = "Ready."
FIRSTRUN_NOT_READY = "Not ready: {n} item(s) need a fix (first: {title})."
FIRSTRUN_HEADER = "claude-multi doctor --first-run"
FIRSTRUN_WAITS = "waits for {what}"

# ------------------------------------------------------------------ uninstall
UNINSTALL_PLAN_HEAD = "claude-multi uninstall — plan (nothing has been removed yet)"
UNINSTALL_QUESTION = "Proceed? [y/N] "
UNINSTALL_CREDENTIALS_QUESTION = ("Delete your credentials too? Type delete credentials to delete them, or press "
                                  "Enter to keep them: ")
UNINSTALL_CREDENTIALS_WORDS = "delete credentials"
UNINSTALL_REVOKE = "If this computer is leaving your control, also revoke these keys at their providers: {names}."
UNINSTALL_HOLD = ("a sign-in save is not confirmed yet — wait a minute and run claude-multi uninstall again; "
                  "nothing was removed")
UNINSTALL_NOT_OURS = ("the gateway port {port} is used by a program claude-multi did not start — stop it, then "
                      "run claude-multi uninstall again; nothing was removed")
UNINSTALL_LIVE = ("sessions may still be running: {ids} — end them first, or run claude-multi uninstall --force; "
                  "nothing was removed")
UNINSTALL_FORCED = "Removing although these sessions may still be running: {ids}."
UNINSTALL_NEEDS_TERMINAL = "claude-multi uninstall: needs a terminal to confirm, or --yes; nothing was removed"
UNINSTALL_DONE = "claude-multi is uninstalled."
UNINSTALL_PARTIAL = "Not everything was removed:"
UNINSTALL_REMAINS = "Still on this computer:"
UNINSTALL_CANCELLED = "cancelled (Ctrl-C) — what was removed so far is listed above"
UNINSTALL_NIX = "managed by Nix — remove claude-multi from your Nix configuration"
UNINSTALL_NO_RECEIPT = "no installer receipt — launchers and PATH lines are left as they are"
UNINSTALL_CHANGED_WRAPPER = "{path}: changed since it was installed — kept"
UNINSTALL_PATH_AMBIGUOUS = "{file}: the installer's PATH line is not there exactly once — kept as it is"
UNINSTALL_KEY_FILE_UNKNOWN = "{problem} — which file holds your API keys is not known, so nothing was removed"
UNINSTALL_ROOT_LINK = "{path} is a link — it and what it points to are left as they are"
UNINSTALL_ROOT_NOT_FOLDER = "{path} is not a folder — left as it is"
UNINSTALL_ROOT_UNREADABLE = "{path} cannot be read ({reason}) — left as it is"
UNINSTALL_RELEASE_OUTSIDE = ("{path} is not a folder inside your home folder — the installed program is left as "
                             "it is")
UNINSTALL_RETAINED = "the previous release, kept for rollback"
UNINSTALL_STARTED = ("sessions started while you confirmed: {ids} — run claude-multi uninstall again; "
                     "nothing was removed")
UNINSTALL_BUSY = ("another claude-multi operation is using the session state (a launch, migrate or restore) — "
                  "let it finish, then run claude-multi uninstall again; nothing was removed")
UNINSTALL_STATE_LOCK = "the session state cannot be locked ({reason}); nothing was removed"
UNINSTALL_SERVICE_UNKNOWN = "the gateway service cannot be checked ({detail}); nothing was removed"
UNINSTALL_SERVICE_STRAY = ("the gateway service unit {path} is present but not recorded — finish it with "
                           "claude-multi gateway service install (or remove that unit yourself), then run "
                           "claude-multi uninstall again; nothing was removed")
UNINSTALL_NOT_STOPPED = "the gateway did not stay stopped ({detail})"
UNINSTALL_NOTHING_ELSE = "nothing else was removed"
UNINSTALL_CHANGED_SINCE_PLAN = "{path}: changed since the plan — kept"
UNINSTALL_MOVED = "{path}: a folder on its way changed since the plan — kept"
UNINSTALL_LOCK_BUSY = "{path}: a lock file uninstall does not hold — kept"
UNINSTALL_STORE_BUSY = ("{path} is held by another claude-multi operation (a profile, setting or provider being "
                        "saved, or a launch) — run claude-multi uninstall again when it finishes; {rest}")
UNINSTALL_STORE_MOVED = "{path}: a folder on its way changed since the plan; {rest}"
UNINSTALL_STORE_LOCK = "{path} cannot be locked ({reason}); {rest}"
UNINSTALL_KEPT_HELD = "{path} ({what}; kept)"

# ------------------------------------------------------------------ export and import
IMPORT_RECONNECT = "connect on this computer: {items}"
RECONNECT_KEY = "{display} API key (G → K, or claude-multi providers set-key {id})"
RECONNECT_SIGNIN = "{kind} (G → L, or claude-multi providers sign-in {id})"
RECONNECT_UNKNOWN_KEY = "API key {name} (claude-multi providers set-key PROVIDER)"
