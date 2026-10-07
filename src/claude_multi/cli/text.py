"""Text."""

from claude_multi import termtext


# UX §1 durability badge: new sessions always launch with per-session scope
# files. Text form only (no color-only meaning, UX §12).
DURABLE_BADGE = "durable scope"

# The add-dir carry evidence line, verbatim (a doctor info line; the takeover
# acceptance item U1 stays pending until the user takeover proof runs).
EVIDENCE_ADD_DIR_CARRY = (
    "add-dir carry: backgrounding documented · takeover binary-consistent, "
    "acceptance-pending(U1)"
)

# Retire argv-only launches, not the durable resume of legacy records.
LEGACY_REFUSAL = (
    "--legacy was retired: every launch is durable, and gateway auth "
    "now comes only from the session's apiKeyHelper (the env token was argv "
    "mode's only credential). Drop the flag. Legacy records still resume "
    "normally and upgrade to a durable scope on first resume."
)

# The one apply instruction for a re-rendered gateway config: the gateway
# hot-reloads the replaced config.yaml and the apply verifies it through the
# render sentinel, printing the restart command only when the reload did
# not apply.
APPLY_GATEWAY_COMMAND = (
    "`claude-multi providers apply` (verified reload; it prints the "
    "restart command if one is needed)"
)

# The relaunch preflight (binary verification + gateway readiness)
# runs before prepare/perform; a failure leaves record and scope untouched.
RELAUNCH_PREFLIGHT_REFUSAL = (
    "relaunch preflight failed; the session record and scope are untouched: "
)

# A launch without a terminal names its profile.
NONINTERACTIVE_PROFILE_REQUIRED = (
    "noninteractive launch requires --profile NAME or --profile-file PATH (or "
    "claude-multi direct --model MODEL); no default was selected"
)
PROFILE_IS_COMPOSITION = (
    "{name!r} is a legacy composition, not a profile; run "
    "claude-multi profile migrate --dry-run"
)
RESUME_PROFILE_OVERRIDE_REFUSAL = (
    "resume always uses the session's lineup; --profile {name} does not apply to "
    "{mid}. To change it: claude-multi lineup --session {rid} profile {name} (live "
    "when compatible; add --relaunch otherwise)"
)
RESUME_PROFILE_FILE_REFUSAL = (
    "--profile-file cannot apply to a resume: an unsaved profile is never "
    "verifiably the recorded lineup; save it (claude-multi profile new NAME) and "
    "use claude-multi lineup --session {rid} profile NAME"
)
# A resume whose transcript sits under ~/.claude while this shell exports
# CLAUDE_CONFIG_DIR (managed launches unset it); the retry unsets it for
# that one command.
CONFIG_DIR_RESUME_MISMATCH = (
    "this shell sets CLAUDE_CONFIG_DIR ({configured}), so the transcript was looked for there, but "
    "managed sessions do not use it: every managed launch unsets it, and this session's transcript is "
    "at {found}. Resume with the variable unset for this one command: {retry}. No transcript was read "
    "and nothing was changed."
)
NEEDS_CHOICE_REFUSAL = (
    "session {mid} needs a profile or model choice (lead {key} was removed: "
    "{notice}); run claude-multi -r {mid} interactively, or: claude-multi lineup "
    "--session {rid} --relaunch --its-exited profile <name> | direct <model>"
)
ALIAS_NOTE = (
    "claude-multi: '{old}' is the earlier spelling of '{new}'; the alias stays accepted"
)


CHEATSHEET_NAME = "CHEATSHEET.md"
USAGE_NAME = "USAGE.md"


# The card's help. The /model lines are byte-identical to
# lineup.MODEL_HELP_LINES (DIRECT_HELP repeats the second); the cheat sheet
# hint follows the `?` line; the /cm verbs are the session skill's.
_HELP_MODEL = (
    "/model: press s to switch for this session only — Enter saves the choice "
    "into ~/.claude/settings.json, which plain claude then inherits\n"
    "/model lists only the lead set; another provider family asks first (Alt+P and "
    "/config block instead); a model outside the set is refused — relaunch with a "
    "profile whose lead is that model.\n"
)
_HELP_DOCTOR = (
    "H — doctor: the report claude-multi doctor prints (Ready / Attention / BLOCKED), then the\n"
    "gateway's own actions (start, restart, log).\n"
    "U — update claude-multi, shown when an update applies: an installed release checks, asks and\n"
    "applies; a Nix install updates through its flake. Claude Code itself: W → Claude Code.\n"
)
# The terms and the in-session /cm guidance: the help (?) and the card's
# details (V) show the same lines.
CARD_TERMS_TITLE = "Terms"
CARD_TERMS = (
    "  profile — a saved lineup: the lead model and the cm-* agents bound to roles.",
    "  lead — the model you talk to; agents — the cm-* subagents it delegates to.",
    "  provider — where a model runs: an API key or an account sign-in, through the local gateway.",
    "  follow / pin — a following session takes its profile's edits; a pinned one keeps its lineup.",
    "  GP — Claude Code's own general-purpose agent.",
)
CARD_CM_TITLE = "In a claude-multi session"
CARD_CM_GUIDANCE = (
    "  /cm show — this session's lineup; /cm profiles and /cm profile <name> switch profiles.",
    "  /cm set <agent>=<model>[:<effort>] and /cm unset <agent> bind or unbind one agent.",
    "  /cm direct, /cm pin, /cm follow, /cm fallback <provider>, /cm review, /cm quota.",
    "  A change applies after /reload-plugins in that session; running agents keep their model.",
)
_HELP_SESSION = (
    "\n"
    + CARD_TERMS_TITLE + "\n"
    + "".join(line + "\n" for line in CARD_TERMS)
    + "\n"
    + CARD_CM_TITLE + "\n"
    + "".join(line + "\n" for line in CARD_CM_GUIDANCE)
    + "  Implementer agents work in their own Git worktree: in a directory that is not a Git "
    "repository they are unavailable (git init enables them).\n"
    "  Settings, MCP servers, memory and resumable sessions come from ~/.claude (managed sessions "
    "unset CLAUDE_CONFIG_DIR); provider API keys are always removed from the session, and any other "
    "*_API_KEY variable stays only when Settings → kept environment names it.\n"
    "\n"
    "-- workflow guarantees --------------------------------------------"
)
QUICK_HELP = (
    "New here? W — Get started: connect a provider (API key or account sign-in) and pick a profile.\n"
    "Enter — launch this profile (a fresh durable session).\n"
    "E — edit this profile (^S saves; sessions that follow it are offered the change).\n"
    "Tab / Shift-Tab — next / previous profile (connected first, then most recently used).\n"
    "P — profiles: use, create, copy, rename, delete, restore shipped versions, make one the default;\n"
    "    B there edits named bindings; F lists fallback profiles (one provider each) for an outage.\n"
    "D — direct session: one model as the lead, no agents (custom models too).\n"
    "S — sessions: resume, details (V), change a lineup (T), follow/pin (F), rename (R), forget (X).\n"
    "G — providers: Enter connects, K API key, L sign in or out, T test, X remove, P apply.\n"
    "M — models: Enter inspects a line (or admits a new one); candidates can be declared.\n"
    "O — settings: compaction, Explore, workflow default, review rounds, kept environment, token.\n"
    "V — full details: every row, the /model lead set, the kept environment, managed differences.\n"
    + _HELP_DOCTOR
    + "? — this help, then the workflow guarantees below.\n"
    + termtext.CHEATSHEET_HINT
    + "\n"
    "Esc — quit without launching (in a dialog or a text field, Esc cancels it).\n"
    + _HELP_MODEL
    + _HELP_SESSION
)


RESUME_QUICK_HELP = (
    "Enter — resume this session with the lineup shown (a resume keeps the session's lineup).\n"
    "V — full details: every row, the /model lead set, the kept environment, managed differences.\n"
    "S — sessions: change this session's lineup (T), follow/pin (F), rename (R), forget (X).\n"
    "{sign_in}\n"
    + _HELP_DOCTOR
    + "? — this help, then the workflow guarantees below.\n"
    + termtext.CHEATSHEET_HINT
    + "\n"
    "Esc — cancel: back without resuming (in a dialog or a text field, Esc cancels it).\n"
    + _HELP_MODEL
    + _HELP_SESSION
)


# ---------------------------------------------------------------- Sessions

# The sessions screen's base keys; E, M and P join for the rows they act on
# (``_SessionsScreen._keybar``). `?` sits before Esc.
SESSIONS_KEYBAR = (
    ("Enter", "resume"),
    ("V", "details"),
    ("T", "lineup"),
    ("F", "follow/pin"),
    ("R", "rename"),
    ("X", "forget"),
    ("L", "link"),
    ("C", "all dirs"),
    ("?", "help"),
    ("Esc", "back"),
)
SESSIONS_KEYBAR_ALL = tuple((key, "this dir" if key == "C" else label)
                            for key, label in SESSIONS_KEYBAR)
# The row actions that join the bar when they apply, before L.
SESSIONS_STOP_BINDING = ("E", "stop")
SESSIONS_MARK_BINDING = ("M", "mark ended")
SESSIONS_REPAIR_BINDING = ("P", "repair")
# The last line is tui.CHEATSHEET_HINT.
SESSIONS_HELP = (
    "Enter — resume the selected session (a live one opens the takeover gate; ⚠ opens fork adopt/discard;\n"
    "\"needs a choice\" opens the chooser; a moved directory offers Relink).\n"
    "V — details: identity, directory, runtime id, lineup, the pending change and why it waits, drift,\n"
    "routing and the observed usage of the last 24 hours.\n"
    "T — change its lineup: another profile, one agent, direct, keep the current lineup (discards a\n"
    "pending change, turns Follow off) or a fallback provider. Each shows its effect first: LIVE when\n"
    "compatible (then /reload-plugins in that session), otherwise recorded and applied at the next resume.\n"
    "A legacy record needs one resume first.\n"
    "F — follow its profile (profile edits apply) or pin its lineup (they do not). Turning Follow on can\n"
    "change the lineup and pinning drops a pending change: F previews that, and y applies it.\n"
    "R — rename (the title is shown here only). X — forget: deletes the record and its generated scope;\n"
    "Claude Code's transcript stays, and L links it again.\n"
    "E — stop a session running in the background (claude stop; the conversation is kept).\n"
    "M — mark ended: record the end of a session that exited without one.\n"
    "P — repair: rebuild the session's generated scope from its record.\n"
    "When liveness cannot be checked, forget and stop ask you to type the session id first.\n"
    "C — this directory / all directories. L — link a native (unmanaged) session to a profile or a\n"
    "direct model.\n"
    "Marks: ● running · ◐ unknown (no end event, no process seen) · ○ ended ·\n"
    "↻ relaunch change recorded · ⚠ fork waiting · ! needs a choice.\n"
    + termtext.CHEATSHEET_HINT
)
SESSIONS_HELP_TITLE = "sessions — help"
SESSIONS_EMPTY = "(no recorded sessions)"
SESSIONS_EMPTY_FILTERED = "(no sessions in this directory — press C to see all)"
SESSIONS_MIN_COLS = 44
# Top, title, rule, header, banner, actions row, message.
SESSIONS_CHROME = 7
SESSIONS_V3_LINEUP = "legacy record — resume it once (Enter) to change its lineup"
SESSIONS_V3_TITLE = "a legacy record has no title; resume it once to migrate it"
SESSIONS_RENAME_TITLE = "rename {m8} — title:"
SESSIONS_BANNER_ACTIONS = "Enter — choose a lineup for each session whose lead model was removed"
SESSIONS_NATIVE_TITLE = "native (not managed) — link one · newest first"
SESSIONS_NATIVE_NONE = "no native (unmanaged) session here — C shows all directories"
SESSIONS_LINK_TITLE = "link {short} to a profile · most recent first"
SESSIONS_ADOPT_TITLE = "adopt fork {short} into a profile · most recent first"
SESSIONS_DIRECT_ITEM = "direct — choose a model…"
SESSIONS_DETAILS_TITLE = "session — details"
FORGET_MODAL_TITLE = "Forget session {short}?"
FORGET_MODAL_BODY = (
    "Runtime id {runtime_id}.\n"
    "Deletes: the session record and its generated scope{scope_note}.\n"
    "Claude Code's transcript is never touched: S → L, or claude-multi sessions link {runtime_id},\n"
    "manages it again."
)
FORGET_BUTTONS = (("Cancel", False), ("Forget", True))
FORK_MODAL_TITLE = "Resolve fork on {short}?"
FORK_BUTTONS = (("Cancel", None), ("Adopt", "adopt"), ("Discard", "discard"))
STOP_MODAL_TITLE = "Stop live session {short}?"
STOP_MODAL_BODY = (
    "The session is live in the background (daemon-owned, ●).\n"
    "It will be stopped with upstream `claude stop {runtime_id}` — never a\n"
    "signal, never the transcript. The conversation is always kept and\n"
    "resumes later with Enter."
)
STOP_BUTTONS = (("Cancel", False), ("Stop", True))
FORGET_DONE = ("forgot {short}; its conversation stays as native session {runtime_id} — S → L or "
               "claude-multi sessions link {runtime_id} manages it again")
# The view a TUI forget ends with (wrapped and scrollable, so the full ids
# and both ways back stay readable at 80x24).
FORGET_DONE_TITLE = "forgot {short}"
FORGET_DONE_BODY = (
    "Session {managed_id} is forgotten: its record and generated scope are removed.",
    "Its conversation stays as native session {runtime_id}; the transcript was not touched.",
    "",
    "To manage it again:",
    "  here: L in Sessions lists the native sessions of this directory — choose {runtime_id}",
    "  in a terminal: claude-multi sessions link {runtime_id}",
)
# Forget and stop when background liveness cannot be observed.
FORCE_TITLE = "{verb} {short} without a liveness check?"
FORCE_BODY = (
    "{reason}\n"
    "Only continue if you know the session is not running. Type the session id to confirm:\n"
    "{managed_id}"
)
FORCE_BUTTONS = (("Cancel", False), ("{verb}", True))
FORCE_MISMATCH = "the typed id does not match {short} — nothing was changed"
# Mark ended and repair.
MARK_TITLE = "Mark {short} ended?"
MARK_BODY = (
    "Records an end for a session that exited without one (its last event is not an end).\n"
    "Refused when it is running or its liveness cannot be checked. Nothing else changes."
)
MARK_BUTTONS = (("Cancel", False), ("Mark ended", True))
MARK_NOT_NEEDED = "{short} already has an end event — nothing to mark"
REPAIR_TITLE = "Repair the scope of {short}?"
REPAIR_BODY = (
    "Rebuilds the session's generated files from its record and the installed catalog\n"
    "(claude-multi doctor --repair). A running session keeps what it loaded until it resumes."
)
REPAIR_BUTTONS = (("Cancel", False), ("Repair", True))
REPAIR_LEGACY = "a legacy record has no generated scope to repair — resume it once (Enter)"
REPAIR_RESULT_TITLE = "repair — {short}"
# Follow / pin from the sessions screen: the preview, then y applies it.
FOLLOW_PREVIEW_TITLE = "follow — preview for {short}"
PIN_PREVIEW_TITLE = "pin — preview for {short}"
# The moved-directory gates.
RELINK_TO = "Relink to {path}"
RELINK_CHOOSE = "Relink to…"
RELINK_INPUT_TITLE = "relink {short} — the project's new directory:"
RELINK_NOT_DIR = "{path} is not a directory — nothing was changed"
RELINK_DONE = "relinked {short} to {path}"
RELINK_MOVE_TITLE = "Move the transcript by hand"
RELINK_MOVE_BODY = (
    "The record now points at {cwd}, but Claude Code's transcript is still filed under the old "
    "directory. claude-multi never moves, reads or deletes transcripts. To move it yourself, run these "
    "two commands in a terminal, then resume again:\n"
    "\n"
    "{command}"
)
TRANSITION_MODAL_TITLE = "Confirm transition"
TRANSITION_MODAL_BODY = (
    "The target Claude process must have EXITED (not merely idle);\n"
    "exiting restarts the turn.\n"
    "\n"
    "Has the target process exited?"
)

# The lineup dialog.
LINEUP_DIALOG_KEYBAR = (
    ("Enter", "apply"),
    ("Tab", "target"),
    ("← →", "choose"),
    ("Space", "pick"),
    ("?", "help"),
    ("Esc", "cancel"),
)
LINEUP_DIALOG_HELP = (
    "Pick one change and see its effect before anything is written. The dialog opens on the session's\n"
    "own profile.\n"
    "Tab moves between the targets: a profile (← → cycles them, most recently used first; Space lists\n"
    "them), a per-agent edit (Space opens the nine agents: Enter picks a model, U unsets), direct (no\n"
    "agents; Space picks the lead), keep the current lineup, and a fallback provider (← → cycles them;\n"
    "Space lists them).\n"
    "Keep the current lineup turns Follow off and discards a pending change; the applied lineup, the\n"
    "scope and the generation stay as they are.\n"
    "A fallback switches the lead and the bound agents to a provider's fallback profile (the profile\n"
    "whose primary provider it is); every provider with one is offered, and the preview lists the moves.\n"
    "LIVE applies now: running agents keep their model, new spawns use the new lineup; run\n"
    "/reload-plugins in that session. RELAUNCH is recorded as a pending change and applies at the\n"
    "session's next resume; nothing is started from here. An ended session takes any change at its\n"
    "next resume.\n"
    "A model you added (◇) can change LIVE when its selector is already in the proven launch fence\n"
    "and the client class/process policy stays compatible. A missing selector or changed class/window\n"
    "requires RELAUNCH; admission and qualification are optional warnings, not permission to use it.\n"
    "After a LIVE change use /reload-plugins and fresh agent spawns, not continued agents.\n"
    "One change per Enter; a session that changed meanwhile shows the refreshed preview instead.\n"
    "Esc cancels without writing."
)
LINEUP_PREVIEW_STALE = "the session changed since the preview — nothing was applied; review the refreshed preview"
LINEUP_DIALOG_MIN_COLS = 60
# Top, title, rule, from, to, mode, next, blank, message (+3 diff rows).
LINEUP_DIALOG_CHROME = 9
LINEUP_REPORT_TITLE = "lineup — {m8}"
LINEUP_NOTHING = "nothing to apply"
LINEUP_PICK_PROFILE = "choose a profile · most recent first"
LINEUP_PICK_PROVIDER = "choose the provider to fall back to"
LINEUP_NO_FALLBACK = "no provider has a fallback profile yet — in Profiles, E → General → primary provider makes one"
LINEUP_KEEP_NOTHING_TO_PICK = "keep the current lineup has nothing to pick; Enter applies it"
LINEUP_PER_AGENT_TITLE = "per-agent edit — {m8} (one change per apply)"
LINEUP_PER_AGENT_KEYBAR = (("Enter", "pick model"), ("U", "unset"), ("?", "help"), ("Esc", "back"))
LINEUP_PER_AGENT_HELP = (
    "The nine agents with the bindings this session runs now. Enter opens the model\n"
    "picker for the highlighted agent (its choice becomes the dialog's change); U\n"
    "unsets it (the agent falls back to its native default). Esc returns."
)
LINEUP_PER_AGENT_MIN_COLS = 50

# The needs-a-choice chooser.
NEEDS_CHOICE_LIST_TITLE = "sessions that need a choice"
NEEDS_CHOICE_TITLE = "choose a lineup for {m8} — {notice}"
NEEDS_CHOICE_KEYBAR = (("Enter", "choose"), ("?", "help"), ("Esc", "back"))
NEEDS_CHOICE_HELP = (
    "The session's lead model was removed from the catalog. Choose a profile (most\n"
    "recently used first) or a direct model; the resume card then shows what\n"
    "changes, and Enter resumes the session on that lineup. Esc changes nothing."
)


# ---------------------------------------------------------------- Models

MODELS_HELP = (
    "Every catalog line with its generation, provider, context class, declared\n"
    "efforts and how many profiles bind it, then the models you added (op) and\n"
    "legacy custom lines. candidates counts registry and routable\n"
    "uncataloged ids: Enter lists them, and one opens with Declare…, the model form\n"
    "filled from the registry (nothing is declared or admitted before its preview).\n"
    "Enter records or revokes an optional local admission badge on a New line,\n"
    "even with an unavailable route; neither changes use availability or evidence.\n"
    "On any other line Enter inspects its provider, selectors, context and evidence.\n"
    "Q runs optional diagnostics on a model you added (chosen checks, default-No consent);\n"
    "E edits its declaration (an edit invalidates admission); V shows details.\n"
    "X removes a model you added; where profiles use it you choose a replacement first.\n"
    "Use availability, admission, qualification and family independence are separate facts.\n"
    "A missing/stale badge or failed/not-run diagnostic warns, not disables.\n"
    "A disabled provider or unusable route still blocks use: Esc → G (Providers) shows its remedy.\n"
    "CLI: models admit|revoke|edit|rm KEY; models qualify KEY --agents.\n"
    "Retired keys map to their successor; running sessions retain continuity\n"
    "aliases. An upstream retirement date within 30 days shows on the selected\n"
    "line. Providers are switched on and off in G (providers).\n"
    "Retained aliases keep older routes in the gateway configuration; Providers shows whether routes are served."
)
# Every bar ends ``? help · Esc``; Enter joins it only on a row it acts on.
MODELS_KEYBAR = (("Enter", "inspect"), ("V", "details"), ("?", "help"), ("Esc", "back"))
# The candidates list and one candidate.
CANDIDATES_TITLE = "candidates — advisory; Enter opens one (nothing is declared or admitted)"
CANDIDATES_KEYBAR = (("Enter", "open"), ("?", "help"), ("Esc", "back"))
CANDIDATES_HELP = (
    "Model ids the pinned registry lists, or the local gateway serves, that no line describes yet.\n"
    "They are observations, not offers: Enter opens one with what the registry states, and Declare… "
    "opens the model form filled with it. You review the declaration before anything is written; a "
    "valid declared model is selectable where its provider/route allows, without admission or diagnostics. "
    "Nothing here sends a request to a provider."
)
CANDIDATE_TITLE = "candidate — {wire}"
CANDIDATE_ADVISORY = "Registry figures are stated by the registry, not validated."
CANDIDATE_DECLARE = ("Enter — Declare… on {provider}: the model form, filled from the registry; "
                     "Esc goes back. Nothing is written before the form's preview.")
CANDIDATE_UNATTRIBUTED = ("This id cannot be attributed to one provider, so it is not declared from here: "
                          "G → A on its provider adds it by hand.")
MODELS_KEYBAR_NEW = (("Enter", "admit badge"), ("Q", "diagnostics"), ("E", "edit"), ("X", "remove"), ("V", "details"),
                     ("?", "help"), ("Esc", "back"))
MODELS_KEYBAR_ADMITTED = (("Enter", "revoke badge"), ("Q", "diagnostics"), ("E", "edit"), ("X", "remove"), ("V", "details"),
                          ("?", "help"), ("Esc", "back"))
MODELS_KEYBAR_UNAPPROVED = MODELS_KEYBAR_NEW
MODELS_X_CATALOG = ("Shipped models cannot be removed — G → Space turns a provider off. "
                    "Enter on a New model changes only its optional admission badge.")
REMOVE_LINE_TITLE = "Remove model {key}?"
SUCCESSOR_TITLE = "replace {key} in {n} place(s) with"
SUCCESSOR_ITEM = "{display} · {provider} · {family}"
SUCCESSOR_CONFIRM_TITLE = "remove {key} — replacements"
SUCCESSOR_CANCEL = "Cancel"
MODELS_MIN_COLS = 60
MODELS_MIN_WIDTHS = (10, 12, 8, 4, 8, 7)
# Writes a read-only Runtime refuses (``allow_state_writes``), every screen.
SETTINGS_READ_ONLY = "read-only: this command cannot write settings"
_APPLIES_NEXT = "applies at the next launch or resume"


# ---------------------------------------------------------------- Direct

DIRECT_TITLE = "direct session — lead only, no profile"
DIRECT_CHOOSE_TITLE = "choose a direct model — {purpose}"
DIRECT_CLASS_NOTE = "/model in-session: every model in the same class"
DIRECT_KEYBAR = (
    ("Enter", "launch"),
    ("Tab", "save as profile"),
    ("G", "providers"),
    ("?", "help"),
    ("Esc", "back"),
)
DIRECT_CHOOSE_KEYBAR = (("Enter", "choose"), ("?", "help"), ("Esc", "back"))
# Joining the bar when they apply: ← → on a row whose effort is chosen
# here, W while the gateway is not reachable (before ?).
DIRECT_EFFORT_BINDING = ("← →", "effort")
DIRECT_GATEWAY_BINDING = ("W", "gateway")
DIRECT_EMPTY = "(no model can lead a direct session here — G connects a provider)"
# The /model line is byte-identical to the card help's.
DIRECT_HELP = (
    "No profile is saved. The session is recorded and resumable. Every valid\n"
    "model is listed, including legacy custom models; unavailable rows name a remedy.\n"
    "Admission and diagnostics are optional. Gateway-effort models carry\n"
    "the effort in the model name: ← → picks it. In the session, /model switches\n"
    "among models of the same class (press s to keep the change to this session).\n"
    "/model lists only the lead set; another provider family asks first (Alt+P and "
    "/config block instead); a model outside the set is refused — relaunch with a "
    "profile whose lead is that model.\n"
    "Tab saves the choice as a lead-only profile (including custom models): it is then\n"
    "listed in Profiles and launched from the card like any profile, while a launch\n"
    "from here stays a direct session with no profile. A direct session can gain\n"
    "agents later with /cm profile <name> or sessions T.\n"
    "G — providers: connect one when a model is marked. W — the local gateway (start\n"
    "it or read its log) while it is not reachable."
)
DIRECT_MIN_COLS = 44
DIRECT_SAVE_TITLE = "save as profile — name:"
DIRECT_SAVED = "saved profile {name} (lead only)"
DIRECT_CUSTOM_REFUSAL = (
    "legacy custom model {key!r} can bind a profile; check its provider/route configuration if unavailable"
)
# Enter's recheck of a marked row.
DIRECT_NOT_LAUNCHED = "not launched: the provider's API key is missing (G → K)"
DIRECT_UNSERVED_TITLE = "Not served by the running gateway"
DIRECT_UNSERVED_BODY = ("The gateway configuration carries this model but the running gateway does not serve it "
                        "yet — apply your changes with G → P (claude-multi providers apply; a verified reload). "
                        "Launching now fails at request time.")
DIRECT_SIGNIN_TITLE = "Not signed in"
DIRECT_SIGNIN_BODY = ("{kind} is not signed in on this computer, so this model fails at its first "
                      "request. Sign in now runs the sign-in in this terminal (personal use).")
DIRECT_SIGNIN_BUTTONS = (("Cancel", None), ("Launch anyway", "launch"), ("Sign in now", "signin"))
DIRECT_SIGNIN_CHOOSE_BUTTONS = (("Cancel", None), ("Choose anyway", "launch"), ("Sign in now", "signin"))
DIRECT_KEY_MISSING_TITLE = "API key missing — requests may fail"
DIRECT_KEY_MISSING_BODY = ("{display} has no API key on this computer; requests may fail. G → K sets it. "
                           "Launch anyway? This does not save a key or change the provider's route.")
DIRECT_KEY_MISSING_BUTTONS = (("Cancel", False), ("Launch anyway", True))
DIRECT_SECRET_FILE_ERROR = "the key file is unavailable or invalid"
_DIRECT_MARK_NO_SECRET = "key missing"
_DIRECT_MARK_SIGNIN = "not signed in"
_DIRECT_MARK_UNSERVED = "not served"
DIRECT_REASON_KEY = "{display} API key missing — G → K sets it"
DIRECT_REASON_KEY_INVALID = "{display} API key invalid or unreadable — G → K replaces it"
DIRECT_REASON_SIGNIN = "{kind} not signed in — Enter offers the sign-in (personal use)"
DIRECT_REASON_UNSERVED = ("not served by the running gateway — G → P applies your changes; Enter asks before "
                          "launching anyway")


# ---------------------------------------------------------------- Providers

PROVIDERS_TITLE = "providers — local status"
PROVIDERS_LEGEND = ("credential: API key or sign-in · served: models the local gateway serves now "
                    "(not a test of the provider)")
PROVIDERS_APPLY_BANNER = "The gateway is not serving your latest provider changes — P applies them."
# The bar while the list is empty.
PROVIDERS_KEYBAR = (("N", "new"), ("R", "refresh"), ("?", "help"), ("Esc", "back"))
PROVIDERS_KEYBAR_KEY_MISSING = (("Enter", "set key"), ("A", "add models"), ("Space", "on/off"), ("Q", "details"),
                                ("N", "new"), ("R", "refresh"), ("?", "help"), ("Esc", "back"))
PROVIDERS_KEYBAR_KEY_SET = (("Enter", "details"), ("K", "replace key"), ("X", "remove key"), ("T", "test"),
                            ("A", "add models"), ("Space", "on/off"), ("N", "new"), ("?", "help"), ("Esc", "back"))
PROVIDERS_KEYBAR_ACCOUNT = (("Enter", "{primary}"), ("L", "sign in/out"), ("T", "test"), ("Space", "on/off"),
                            ("Q", "details"), ("N", "new"), ("?", "help"), ("Esc", "back"))
# An account provider whose active connection is its API key: key actions
# first; L still lists and manages the saved accounts.
PROVIDERS_KEYBAR_ACCOUNT_KEY = (("Enter", "{primary}"), ("K", "{key_label}"), ("X", "remove key"),
                                ("L", "accounts"), ("T", "test"), ("Space", "on/off"), ("Q", "details"),
                                ("N", "new"), ("?", "help"), ("Esc", "back"))
PROVIDERS_KEYBAR_OWN = (("Enter", "{primary}"), ("E", "edit"), ("K", "{key_label}"), ("X", "remove"),
                        ("A", "add models"), ("T", "test"), ("Space", "on/off"), ("N", "new"), ("?", "help"),
                        ("Esc", "back"))
PROVIDERS_KEYBAR_OWN_KEYLESS = (("Enter", "details"), ("E", "edit"), ("X", "remove"), ("A", "add models"),
                                ("T", "test"), ("Space", "on/off"), ("N", "new"), ("?", "help"), ("Esc", "back"))
PROVIDERS_KEYBAR_KEYLESS = (("Enter", "details"), ("T", "test"), ("A", "add models"), ("Space", "on/off"),
                            ("N", "new"), ("R", "refresh"), ("?", "help"), ("Esc", "back"))
PROVIDERS_APPLY_BINDING = ("P", "apply")
# Joins the bar while the local gateway is not reachable (before ?).
PROVIDERS_GATEWAY_BINDING = ("W", "gateway")
PROVIDERS_PRIMARY = {"approve": "approve", "set-key": "set key", "connect": "connect", "sign-in": "sign in",
                     "account": "sign in/out", "details": "details"}
PROVIDERS_KEY_LABELS = {"set": "set key", "replace": "replace key"}
L_NOT_ACCOUNT = "{display} uses an API key — K sets it."
E_CATALOG = "{display} ships with claude-multi — E edits providers you added (N adds one)."
DETAILS_MULTI_ACCOUNT = ("{n} accounts are signed in; requests may use any of them — L signs out all of "
                         "them.")
DETAILS_SIGNED_IN = "Signed in: {accounts}"
DETAILS_SAVED_ACCOUNTS = "Saved accounts: {accounts} (not used while the API key is in use; L manages them)"
DETAILS_TRANSPORT = "Transport: {transport}"
DETAILS_TRANSPORT_KEY = "{display} API key"
DETAILS_TITLE = "provider — full details"
PROVIDERS_HELP = (
    "Enter does the main thing for the selected provider: set its API key, sign in, approve its route,\n"
    "or show details.\n"
    "K sets or replaces an API key (typed masked; saving reloads the gateway; nothing is sent to the\n"
    "provider). X removes a key, signs out, or removes a provider you added; profiles that use it stop\n"
    "being connected.\n"
    "L signs in to or out of a Claude or ChatGPT account (personal use; it runs in this terminal).\n"
    "Anthropic and OpenAI: Enter chooses between your account and an API key (one at a time; an\n"
    "OpenAI API key serves only the models reviewed for it).\n"
    "T tests a provider with one request (you confirm first; it may be billed).\n"
    "A adds models (a listing you confirm, or by hand). N adds your own endpoint (from a reviewed\n"
    "preset or by hand) or a server on your network. E edits a provider you added; Enter approves its\n"
    "route.\n"
    "P applies your changes when the gateway is not serving them yet.\n"
    "Space turns a provider on or off for new sessions; running sessions keep theirs.\n"
    "Q shows details: models, served state, signed-in accounts, quota. R refreshes.\n"
    "W opens the local gateway: start or restart it now, or read its log (stopping it is a terminal\n"
    "command: claude-multi gateway stop).\n"
    "Changing credentials needs a terminal outside Claude Code."
)
# The key modal (set or replace an API key).
KEY_MODAL_TITLE = "{display} API key"
KEY_MODAL_BODY = ("Typed masked and saved in a private file on this computer:\n  {path}\n"
                  "The value is never shown or logged. Saving reloads the local gateway; nothing is "
                  "sent to {display}.")
KEY_MODAL_REPLACE = "A key is set ({n} chars). Replace replaces it; Cancel keeps it."
KEY_MODAL_BUTTONS_NEW = (("Save", True), ("Cancel", False))
KEY_MODAL_BUTTONS_REPLACE = (("Cancel", False), ("Replace", True))
KEY_CANCELLED = "Nothing saved."
# One more provider from a preset whose API key another provider uses: which
# key it takes, and the confirmation before a shared key is replaced.
PRESET_KEY_TITLE = "Which API key does {id} use?"
# The chooser's short items (own, share, replace); the text above them says
# what each does, the full generated key name included, wrapped to the screen.
PRESET_KEY_ITEMS = ("A new key of its own", "Share the saved key", "Replace the saved key")
PRESET_KEY_DETAILS = ("A new key of its own: saved as {own}.",
                      "Share the saved key: {id} uses {name} too; no key is typed.",
                      "Replace the saved key: the key you type replaces {name} for {providers} too.")
PRESET_KEY_HELP = (
    "Another provider already uses this preset's API key. A new key of its own leaves the saved key as it\n"
    "is; sharing the saved key types nothing; replacing it changes the key of every provider that uses it\n"
    "(you confirm that first, and the providers are named)."
)
SHARED_KEY_REPLACE_TITLE = "Replace the API key of {providers}?"
SHARED_KEY_REPLACE_BODY = ("{name} is the API key of {providers}. The key you type next replaces it for each of "
                           "them. Cancel keeps it.")
SHARED_KEY_REPLACE_BUTTONS = (("Cancel", False), ("Replace", True))
# Removing an API key.
REMOVE_KEY_TITLE = "Remove the {display} API key?"
REMOVE_BUTTONS = (("Cancel", False), ("Remove", True))
# An account provider (Anthropic, OpenAI): its account or its API key. The
# items are the picker's labels, padded to one column, then the state.
TRANSPORT_CHOOSER_TITLE = "Connect {display} — choose one"
TRANSPORT_CHOOSER_BODY = ("Both reach the same {models}; claude-multi uses one of them at a time, never "
                          "both.")
# A key route that serves only the models reviewed for it.
TRANSPORT_CHOOSER_BODY_REVIEWED = ("claude-multi uses one of them at a time, never both. The API key serves "
                                   "only the models reviewed for it: {names}.")
TRANSPORT_CHOOSER_GAP = 3
TRANSPORT_IN_USE = "  · in use"
# Anthropic's title stays the one its chooser has always shown; another
# account provider's API key (OpenAI) has its own wording.
TRANSPORT_TO_KEY_TITLES = {"anthropic": "Use an Anthropic API key for Claude models?"}
TRANSPORT_TO_KEY_TITLE = "Use your {display} API key for {models}?"
TRANSPORT_TO_ACCOUNT_TITLE = "Use your {account} for {models} again?"
TRANSPORT_BUTTONS = (("Cancel", False), ("Switch", True))
KEEP_KEY_TITLE = "Remove the saved {display} API key too?"
KEEP_KEY_BODY = "It is no longer used. Removing it deletes it from {path}."
KEEP_KEY_BUTTONS = (("Keep it", False), ("Remove it", True))
SIGN_IN_NOW_TITLE = "Sign in to your {account} now?"
SIGN_IN_NOW_BUTTONS = (("Later", False), ("Sign in", True))
# Approving a route.
APPROVE_TITLE = "Approve the route of {id}?"
APPROVE_TITLE_CHANGED = "{id} changed — approve its new route?"
APPROVE_BUTTONS = (("Cancel", False), ("Approve", True))
# Applying provider changes.
APPLY_TITLE = "Apply your provider changes?"
APPLY_BUTTONS = (("Cancel", False), ("Apply", True))
# Removing a provider you added.
REMOVE_CHOICE_TITLE = "remove — {id}"
REMOVE_CHOICE_KEY = "Remove its API key (keep the provider)"
REMOVE_CHOICE_PROVIDER = "Remove the provider"
REMOVE_CHOICE_CANCEL = "Cancel"
REMOVE_BLOCKED_TITLE = "{id} cannot be removed yet"
REMOVE_BLOCKED_HEAD = "Clear these first:"
REMOVE_BLOCKED_ITEM = "· {fix}"
REMOVE_PROVIDER_TITLE = "Remove provider {id}?"
REMOVE_KEY_TOO_BUTTONS = (("Keep it", False), ("Remove it", True))
CLOSE_BUTTON = (("Close", None),)
# The connection test.
TEST_PICK_TITLE = "test which providers? — Space selects"
TEST_PICK_KEYBAR = (("Space", "select"), ("Enter", "continue"), ("?", "help"), ("Esc", "back"))
TEST_PICK_HELP = ("Each selected provider gets one small request through the local gateway after you "
                  "confirm the list. Providers may bill it.")
TEST_CONSENT_TITLE = "connection test — confirm"
TEST_RESULTS_TITLE = "connection test — results"
TEST_DECLINED = "Nothing was sent."
# Your own endpoint and a server on your network.
EDIT_FORM_TITLE = "edit {id}"
EDIT_FORM_FOOTER = "Nothing is saved before the preview and your confirmation."
EDIT_FORM_HELP = (
    "Each field starts with what the provider has now; Enter keeps it.\n"
    "A changed base URL or key header is a new route: it is approved again before it is used.\n"
    "The id, the kind and the key name stay; claude-multi providers edit ID opens every field in your editor."
)
EDIT_UNREADABLE = "{id}: its declaration cannot be read here ({problem}) — claude-multi providers edit {id}"
EDIT_DONE = "{id} saved (the result lists what changed)"
EDIT_NOT_SAVED = "{id}: nothing changed"
ENDPOINT_FORM_TITLE = {"anthropic-compatible": "your Anthropic-compatible endpoint",
                       "openai-compatible": "your OpenAI-compatible endpoint"}
ENDPOINT_FORM_FOOTER = "Nothing is saved before you approve the route and type the key."
ENDPOINT_FORM_HELP = (
    "Name: a–z, 0–9 and -, starting with a letter.\n"
    "Base URL: the endpoint your vendor documents (https://…).\n"
    "How the key is sent: Anthropic-compatible endpoints usually take the x-api-key header.\n"
    "Family: who makes the models; unknown is never treated as independent in reviews.\n"
    "Model list URL: optional; listing it later sends one request you confirm first."
)
ENDPOINT_PREVIEW_TITLE = "Declare this provider?"
LAN_FORM_TITLE = "a server on your network"
LAN_FORM_FOOTER = "Nothing is saved before you confirm the preview."
LAN_FORM_HELP = (
    "An OpenAI-compatible server on your network needs no key, for example a local model server.\n"
    "Its models are lead-only in this release: agents need a reviewed route."
)
LAN_PREVIEW_TITLE = "Declare this server?"
PRESET_FORM_TITLE = "{display} — a preset"
PRESET_FORM_HELP = (
    "Name: a–z, 0–9 and -, starting with a letter; the preset's name is the default.\n"
    "Address: the preset's by default. Change it only to another address the vendor documents (a\n"
    "workspace or regional endpoint); it must be https:// and is checked like the command line's\n"
    "providers add --preset NAME --base-url URL.\n"
    "The key's name and the model list come from the vendor's documentation; claude-multi has not\n"
    "tested them. You approve where the key is sent before anything is saved."
)
ADD_MODELS_TITLE = "add models for {id}?"
NO_MODELS_HEAD = "{id} has no models yet; declare one to select in a profile. Admission is optional."
NO_MODELS_LATER = "{id} has no models yet — A on Providers adds them (claude-multi discover {id} --add WIRE)"
NO_MODELS_NOT_ADMITTED = ("P (Profiles) selects normally. Optional diagnostics: M → Q. "
                          "{id}: added {keys}, not admitted; provider/route restrictions still apply. "
                          "CLI diagnostics: claude-multi models qualify KEY --smoke.")
NO_MODELS_ADMITTED = ("{id}: admitted {keys} (badge only) — P (Profiles) selects it normally; "
                     "provider/route restrictions still apply. Optional diagnostics: M → Q.")
ADMIT_NOW_TITLE = "Admit {key} now?"
ADMIT_NOW_BODY = ("Record an optional local admission badge for {key}? No test request is sent. "
                  "Skip to select it normally in Profiles; provider/route restrictions still apply. "
                  "Optional diagnostics are on Models (Q).")
ADMIT_NOW_BUTTONS = (("Skip", False), ("Admit badge", True))
ADD_MODELS_ITEMS = ("List its models (sends one request; you confirm first)", "Enter a model by hand", "Later")
# The new-provider form's key-name check, before any command or preview.
PROVIDER_SECRET_NAME_TITLE = "new provider — not declared"
PROVIDER_SECRET_NAME_HINT = ("The secret is the NAME of the environment variable that holds the key "
                             "(for example ACME_API_KEY); the key itself is asked for once the provider is "
                             "declared. Nothing was declared.")
ADVANCED_MANUAL = "Advanced: every field (manual)"
OPENAI_COMPAT_CLOSED_FORM_NOTE = ("OpenAI-compatible with an API key is not available in this release — use "
                                  "Anthropic-compatible if your vendor offers it.")
PROVIDERS_MIN_WIDTHS = (8, 3, 9, 6, 6, 6, 3, 5)
PROVIDERS_GAP = 1
# Cursor/margin, six cells (including the full served header), gaps, right edge.
PROVIDERS_MIN_COLS = 4 + sum(PROVIDERS_MIN_WIDTHS) + PROVIDERS_GAP * (len(PROVIDERS_MIN_WIDTHS) - 1) + 1
PROVIDERS_DETAIL_RESERVE = 3


# ---------------------------------------------------------------- Settings

SETTINGS_TITLE = "settings — global (profile overrides marked ◆)"
SETTINGS_KEYBAR = (("Enter", "edit"), ("R", "reset to default"), ("?", "help"), ("Esc", "back"))
# The footer follows the selected row's supported actions (the same
# predicates the key handlers use; the handlers still refuse defensively).
SETTINGS_KEYBAR_OPEN = (("Enter", "open"), ("?", "help"), ("Esc", "back"))
SETTINGS_KEYBAR_ROTATE = (("Enter", "rotate"), ("?", "help"), ("Esc", "back"))
SETTINGS_READ_ONLY_KEY = "read-only:"
SETTINGS_READ_ONLY_EVIDENCE = "shown, not edited"
SETTINGS_READ_ONLY_INVALID = "fix settings.json to edit"
SETTINGS_READ_ONLY_COMMAND = "this command cannot write"
SETTINGS_READ_ONLY_CHOICES = "fix choices.json to edit"
# The context window ceiling row (choices.json) and its command.
SETTINGS_CEILING_PROMPT = "current {current} · default {default} · range {range} (tokens, or e.g. 400K)"
SETTINGS_CEILING_SAVED = "saved {label} = {value} — {applies}; running sessions keep their window"
CEILING_SHOW = "window ceiling: {value} ({tokens} tokens, {source}) · range {range}"
CEILING_RULE = (
    "the lead and every agent run at the smaller of the ceiling and the lead set's smallest provider "
    "bound; agent lines whose bound is below it keep the 200K class"
)
CEILING_HOW = "set: claude-multi window-ceiling 400K · default: claude-multi window-ceiling --reset"
CEILING_SET = "window ceiling set to {value} ({tokens} tokens) — {applies}; running sessions keep their window"
CEILING_RESET = "window ceiling reset to the default {value} ({tokens} tokens) — {applies}"
# A reset is reversible: its result names the ceiling it replaced.
CEILING_RESET_WAS = "  it was {value}: claude-multi window-ceiling {value} sets it again"
CEILING_UNCHANGED = "window ceiling is already {value} ({source}) — nothing changed"
SETTINGS_HELP = (
    "Launcher settings (settings.json and choices.json in ~/.config/claude-multi).\n"
    "Every value is snapshotted into a session at launch: a change applies at the\n"
    "next launch or resume (\"next\"); running sessions show \"settings changed\" and\n"
    "are never blocked. \"live\" applies at once. ◆ marks a value a profile\n"
    "overrides (edit it in the profile's General row). Enter edits, R resets to the\n"
    "default.\n"
    "The context window ceiling caps the window of the lead and every agent. The\n"
    "effective window and the provider bounds are evidence: shown, not edited.\n"
    "The kept environment row lists the API-key variables (names only) a managed\n"
    "session keeps: every other *_API_KEY is removed, so an MCP server that needs its\n"
    "own key gets it only when its name is listed there. Provider keys are never kept."
)
SETTINGS_ENV_KEEP_PROMPT = ("Names of the API-key variables to keep, separated by spaces or commas "
                            "(for example an MCP server's key). Names only; a value is never stored.")
SETTINGS_ENV_KEEP_SAVED = "saved kept environment = {names} — {applies}"
SETTINGS_ENV_KEEP_REFUSED = "not saved, nothing changed: {reasons}"
SETTINGS_ENV_KEEP_RESET_BODY = "Managed sessions then remove every *_API_KEY again."
SETTINGS_ENV_KEEP_INVALID = "fix choices.json to edit"
SETTINGS_MIN_COLS = 60
SETTINGS_DETAIL_LINES = 2
SETTINGS_TOKEN_TITLE = "Rotate the gateway token now?"
SETTINGS_TOKEN_BODY = (
    "The gateway reloads with both keys, the helper switches, then the old key is "
    "retired. Running sessions keep working. Progress is printed."
)


# ---------------------------------------------------------------------------
# The propagation prompt and the profile editor glue (save, named
# bindings).  The tui editor stack writes nothing; these helpers are its
# store access.

PROPAGATION_KEYBAR = (
    ("A", "apply to running"),
    ("L", "later (next resume)"),
    ("?", "help"),
    ("Esc", "keep sessions as they are"),
)
PROPAGATION_KEYBAR_LATER = (
    ("L", "later (next resume)"),
    ("?", "help"),
    ("Esc", "keep sessions as they are"),
)
PROPAGATION_REPORT_KEYBAR = (("?", "help"), ("Esc", "back"))
PROPAGATION_HELP = (
    "The saved profile is followed by the sessions listed. A applies it now to every "
    "running follower: each change is applied live (then run /reload-plugins in that "
    "session) or, when it needs a relaunch, recorded for the session's next resume. "
    "L lists what happens without applying anything live. Esc keeps the sessions as "
    "they are.\n\n"
    "Ended followers take the profile at their next resume in every case. A session "
    "whose state is unknown (◐) is offered like a running one."
)
PROPAGATION_REPORT_HELP = (
    "What the save did to each following session, as the propagation reported it. "
    "Esc goes back."
)
PROPAGATION_MIN_COLS = 60
PROPAGATION_NO_FOLLOWERS = "saved {name} · no session follows it"


# ---------------------------------------------------------------------------
# The launch card and its entry.  The card never performs or execs: every
# launch intent is returned and performed by the caller after curses
# teardown.

CARD_MIN_COLS = 48
# §3.4: top, title, rule, lead, lineup, table header, 1 agent row, blank,
# Status, 1 error line.
CARD_CHROME = 10
CARD_HELP_TITLE = "launch card — help"
# The card's V: the session sections after the card's rows.
CARD_DETAILS_MODEL = "/model in the session (the lead set; s keeps a switch to this session):"
CARD_DETAILS_ENV_TITLE = "Kept environment (Settings → kept environment; names only, never a value)"
CARD_DETAILS_ENV_NONE = "  no API-key variable in this shell: nothing to keep or remove"
CARD_DETAILS_ENV_KEPT = "  kept: {names}"
CARD_DETAILS_ENV_STRIPPED = "  removed: {names}"
CARD_DETAILS_ENV_REFUSED = "  removed although listed: {name} — {reason}"
CARD_DETAILS_ENV_WHY = (
    "  A managed session removes every *_API_KEY from its environment, so tools it starts (MCP servers "
    "too) see only the kept names; provider keys are never kept."
)
CARD_DETAILS_MANAGED_TITLE = "How a managed session differs from plain claude"
CARD_DETAILS_MANAGED = (
    "  Settings, MCP servers, memory and resumable sessions come from ~/.claude (CLAUDE_CONFIG_DIR is unset).",
    "  /model offers the lead set only; the cm-* agents come from the lineup, and /cm changes it.",
    "  Models are served through the local gateway; the session is recorded and resumable "
    "(claude-multi -r).",
)
# The card's H (doctor's report in the terminal).
CARD_DOCTOR_HIDDEN = "({n} more detail line{s}: claude-multi doctor -v)"
CARD_DOCTOR_REPAIR = "\n{n} session finding(s) above name a repair. Repair every stopped session now? [y/N] "
CARD_ONE_PROFILE = "only one profile"
CARD_SECRET_TITLE = "Provider credential missing"
CARD_SECRET_HINT = "Set it in G (providers) → K, or sign in with L."
CARD_SECRET_HINT_RESUME = "Set it with {way}, G (providers) → K, or sign in there with L; then S resumes this session."
CARD_RESUME_INERT = "a resume card keeps the session's lineup: change it in sessions (S, then T)"
# A sign-in remedy as the card states it: the key route, never the command.
# A resume card has no G: its way back (``{way}``: Esc once for each screen
# between it and the launch card, or out of claude-multi and into it again
# when no launch card is below it) leads to a card where G → L signs in and
# S resumes the session again.
CARD_SIGN_IN_FRESH = "G → L"
CARD_SIGN_IN_RESUME = "{way}, G → L, then S to resume"
# A key remedy (``claude-multi providers set-key <id>`` on the command line)
# as the card's H report states it: K on the provider in Providers.
CARD_SET_KEY = "set the key: {route}"
CARD_SET_KEY_FRESH = "G (providers) → K on {display}"
CARD_SET_KEY_RESUME = "{way}, G (providers) → K on {display}, then S to resume"
CARD_WAY_REOPEN = "run claude-multi"
RESUME_SIGN_IN_HELP_BACK = (
    "A sign-in remedy here reads \"{route}\": Esc leads back, screen by screen, to the launch card,\n"
    "    G → L signs in there, and S resumes this session again."
)
RESUME_SIGN_IN_HELP_REOPEN = (
    "A sign-in remedy here reads \"{route}\": Esc leaves claude-multi; run claude-multi again, sign\n"
    "    in there with G → L, and S resumes this session again."
)


# A relaunch of a session still running in the background.
TRANSITION_LIVE_NOTE = ("the target session is LIVE in the background (●, daemon-owned) — a relaunch on a live "
                        "session forks it; stop it first with `claude-multi sessions stop {mid}`")
# The transition screen's help ("lineup" is the word for what a session runs).
TRANSITION_HELP = (
    "A transition changes a session's lineup while keeping its transcript:\n"
    "agents, models, effort, workflow mode, and policy are recomputed and the\n"
    "session relaunches with `claude --resume <uuid>` — same conversation, new\n"
    "lineup.\n"
    "\n"
    "Rules: review the semantic diff above first. The session's process must\n"
    "have EXITED (not merely idle) before anything is mutated — exit the TUI,\n"
    "then confirm. If the relaunch fails, the previous lineup and record\n"
    "are restored and an exact recovery command is shown."
)
COMPOSE_RESTORE_DEFAULT_NOTE = (
    "claude-multi: 'compose restore-default' is the earlier spelling of 'profile reseed "
    "balanced'; it overwrites your edits to the balanced seed, as the earlier spelling did for default"
)
PROFILE_EDITOR_REFUSAL = (
    "profile {verb} needs an editor: set $VISUAL or $EDITOR, or run it on a terminal "
    "that can show the full-screen editor"
)


# A live scope with the other release's managed skill
# policy (the ``scope.SKILL_POLICY`` denies, the /cm skill bytes) is lazy.
SKILL_POLICY_DRIFT = (
    "session {m8}: the managed skill policy or /cm skill differs from this release (an "
    "earlier skill deny or /cm skill; lazy state); applies at the next resume (claude-multi -r "
    "{mid}) — or claude-multi doctor --repair {mid} now (a repaired live scope is written, "
    "not yet proven active in the running client)"
)
# The permission default mode is decided once per launch/resume;
# a live scope whose only difference is ``permissions.defaultMode`` is lazy.
DEFAULT_MODE_DRIFT = (
    "session {m8}: the compiled permission default mode differs from this launch's decision "
    "(lazy state); applies at the next resume (claude-multi -r {mid})"
)
# A passthrough surface S14 proved to defeat the deny gets exactly
# this one stderr line at launch (``compiler.SKILL_POLICY_BYPASS_FLAGS``).
SKILL_POLICY_BYPASS_NOTICE = (
    "claude-multi: notice: {flag} defeats the managed skill deny at this client; "
    "agent-spawning bundled skills are then reachable by the model in this session"
)


# ---------------------------------------------------------------- Get started

GS_TITLE = "Get started"
GS_HEADER = "claude-multi — Get started"
GS_INTRO = ("Connect a provider and claude-multi picks a profile that uses it. Nothing is saved "
            "until you confirm. claude-multi is an independent tool, not affiliated with "
            "Anthropic or OpenAI.")
GS_STEP_TITLES = {"preflight": "Check this computer", "claude": "Claude Code {version}",
                  "gateway": "Local gateway", "providers": "Connect providers",
                  "test": "Test a connection", "profile": "Choose your profile", "check": "Review"}
GS_SUMMARY = {
    ("preflight", "done"): "ready",
    ("preflight", "blocked"): "{n} problem(s) — Enter shows them",
    ("claude", "done"): "verified copy",
    ("claude", "todo"): "not installed — Enter installs it",
    ("claude", "blocked"): "{problem}",
    ("gateway", "done"): "running",
    ("gateway", "todo"): "starts when needed — Enter starts it now",
    ("gateway", "setup"): "not set up yet — Enter sets it up",
    ("gateway", "blocked"): "{problem}",
    ("providers", "done"): "{connected}",
    ("providers", "todo"): "nothing connected yet",
    ("test", "optional"): "optional; one request per provider",
    ("test", "done"): "{results}",
    ("profile", "waiting"): "after a provider is connected",
    ("profile", "done"): "{name} — {why}",
    ("profile", "todo"): "no profile fits yet — Enter builds one",
    ("check", "done"): "ready",
    ("check", "todo"): "{n} item(s) need a fix — Enter shows them",
}
GS_MARKS = {"done": "✓", "todo": "·", "setup": "·", "waiting": "·", "attention": "!", "blocked": "✗",
            "optional": "—"}
GS_CONNECTED = "Connected: {list}"
GS_CONNECTED_NONE = "none"
GS_PROFILE_WHY = {"default": "your default"}
GS_PROFILE_WHY_AUTOMATIC = "chosen automatically"
GS_DETAIL = {
    "preflight": "Supported system, private folders for your settings, and no administrator "
                 "policy that blocks claude-multi. Read-only.",
    "claude": "claude-multi runs its own verified copy of Claude Code {version}. Your own "
              "Claude Code install is never changed.",
    "gateway": "A local gateway on this computer sends each model's requests to its provider. "
               "It starts when you launch a session.",
    "providers": "API keys (OpenRouter, DeepSeek, …), Claude and ChatGPT account sign-ins, your "
                 "own endpoints and servers on your network. Several can be connected.",
    "test": "Sends one small request to each connected provider you choose, after you confirm. "
            "Providers may bill it.",
    "profile": "A profile says which model leads and which models the helper agents use.",
    "check": "The first-run checks: what is ready and the one fix for anything that is not.",
}
GS_KEYBAR = (("Enter", "open"), ("A", "add a provider"), ("P", "profiles"), ("?", "help"), ("Esc", "launch card"))
GS_HELP = (
    "Get started lists what claude-multi needs, in order. ✓ is done; Enter opens any step to run or review it.\n"
    "Progress is read from your setup each time: stop at any point and come back with W on the launch card.\n"
    "A connects a provider: an API key, a Claude or ChatGPT account sign-in, your own Anthropic- or\n"
    "OpenAI-compatible endpoint, or a server on your network.\n"
    "Nothing is written until you confirm, and nothing is sent to a provider without asking first.\n"
    "Keys are typed masked and never shown. P opens Profiles. Esc goes to the launch card."
)
GS_HELP_TITLE = "Get started — help"
GS_RESUME_READONLY = "Get started is read-only on a resume card — Esc, then W on the launch card."
GS_IN_SESSION = ("Get started needs a terminal outside Claude Code sessions — run claude-multi in a separate "
                 "terminal.")
GS_READONLY = "This claude-multi cannot change your setup: it was written by a newer claude-multi."
GS_PROFILES_FROM_CARD = "Profiles open from the launch card: Esc, then P."
GS_MIN_ROWS = 18
GS_MIN_COLS = 60
# Step views.
PREFLIGHT_TITLE = "check this computer"
CLAUDE_STEP_TITLE = "install Claude Code {version}"
CLAUDE_STEP_STATUS_TITLE = "Claude Code {version}"
CLAUDE_STEP_UNTOUCHED = "Your own Claude Code install is not changed."
CLAUDE_STEP_DONE = "Claude Code {version} is installed for claude-multi (verified)."
CLAUDE_STEP_NOT_DONE = "Claude Code {version} is not set up for claude-multi."
CLAUDE_STEP_PROGRESS = "… {line}"
GATEWAY_STEP_TITLE = "local gateway"
GATEWAY_STEP_STARTED = "The local gateway is running ({port})."
GATEWAY_STEP_RUNNING = "The local gateway is already running."
GATEWAY_STEP_BUTTONS = (("Close", None), ("Outbound proxy…", "proxy"), ("Log", "log"))
GATEWAY_LOG_TITLE = "local gateway — log"
GATEWAY_LOG_EMPTY = "(no log lines)"
PROXY_TITLE = "the gateway's outbound proxy"
PROXY_BODY = ("Requests to providers go through this proxy (http://, https:// or socks5://, a host and an "
              "optional port; no user name or password). Leave it empty to connect directly.")
PROXY_CURRENT = "Now: {proxy}"
PROXY_NONE = "none (direct)"
PROXY_BUTTONS = (("Save", "save"), ("No proxy", "clear"), ("Cancel", None))
PROXY_SET = "The gateway's outbound proxy is {proxy}."
PROXY_CLEARED = "The gateway connects directly (no outbound proxy)."
PROFILE_STEP_TITLE = "choose your profile"
PROFILE_STEP_YOURS = "Your profile: {name} ({why})"
PROFILE_STEP_WHY = {"seed": "chosen automatically: the first shipped profile that is connected here",
                    "default": "your default",
                    "here": "the most recent one in this directory",
                    "recent": "chosen automatically: the most recently used connected profile",
                    "yours": "chosen automatically: the first connected profile of yours"}
PROFILE_STEP_KEEP_BUTTONS = (("Keep it", "keep"), ("Other profiles (P)", "profiles"))
PROFILE_STEP_STARTER_INTRO = ("Nothing shipped fits {connected}. claude-multi can build a profile from what is "
                              "connected (no request is sent; nothing is saved yet):")
PROFILE_STEP_STARTER_BUTTONS = (("Save as starter and use it", "save"), ("Profiles (P)", "profiles"),
                                ("Not now", None))
PROFILE_STEP_SAVED = "Saved {name} and made it your default profile."
PROFILE_STEP_WAITING = "Connect a provider first: A adds one."
STARTER_NO_LEAD_TUI = ("No connected provider has a model that can lead yet — G → A adds models, or W connects "
                       "another provider.")
CHECK_STEP_TITLE = "first-run checks"
FIRSTRUN_FOOTER_READY = "Ready. Enter on the launch card starts a session."
FIRSTRUN_FOOTER_NOT_READY = "Not ready yet: {n} item(s) need a fix (first: {title})."
FIRSTRUN_CARD_FOOTER = "W opens Get started."
CHECK_STEP_KEYBAR = (("Enter", "open the launch card"), ("?", "help"), ("Esc", "back"))
STEP_VIEW_KEYBAR = (("↑↓", "scroll"), ("?", "help"), ("Esc", "back"))
CHOICE_KEYBAR = (("Enter", "choose"), ("?", "help"), ("Esc", "back"))

# ---------------------------------------------------------------- the provider picker

PICKER_TITLE = "connect a provider — more can follow"
PICKER_TITLE_OTHER = "add your own provider"
PICKER_KEYBAR = (("Enter", "connect"), ("?", "help"), ("Esc", "back"))
PICKER_HELP = (
    "Pick one way to connect; come back for more. A provider can have two ways (Anthropic: a Claude\n"
    "account sign-in or an Anthropic API key) — claude-multi uses one at a time.\n"
    "API keys are typed masked and stored in a private file on this computer; saving reloads the\n"
    "local gateway and sends nothing to the provider. Account sign-ins are for your own, personal\n"
    "use and run in this terminal. Your own endpoint: Anthropic-compatible is recommended when the\n"
    "vendor documents it. A server on your network needs no key. The family is who makes the\n"
    "models; reviews prefer recognized different families. An unrecognized label does not prove independence."
)
PICKER_HELP_TITLE = "connect a provider — help"
PICKER_FAMILY = "family {family}"
PICKER_YOURS = "Your providers"
PICKER_DETAIL = {
    "account": "Opens a sign-in in your browser, or prints an address to open on any device. "
               "Personal use of your own account only; you confirm that first.",
    "api-key": "Paste the key from your {display} account. It is stored in a private file on this "
               "computer; saving reloads the local gateway and sends nothing to {display}.",
    "api-key-paid": " Billed per token by {display}: set a spend limit in your {display} account.",
    "no-models": "{display} has no models in claude-multi yet: after the key, A on the providers "
                 "screen adds models.",
    "own": "A provider you added. Enter approves its route or sets its key; G → E edits it.",
    "endpoint": "Your vendor's documented endpoint and an API key. You approve where the key is "
                "sent before anything is saved.",
    "lan": "An OpenAI-compatible server on your network (no key), for example a local model server.",
    "preset": "{display}'s documented endpoint: the address, the key's name and the model list are filled "
              "in from its documentation (not tested by claude-multi). You name it, approve where the key "
              "is sent and type the key.",
    "unavailable": "{note}",
    "manual": "Every field of a provider declaration, for endpoints the forms above do not cover.",
}
PICKER_MIN_ROWS = 16
PICKER_MIN_COLS = 56

# ---------------------------------------------------------------- account sign-in

ACK_TITLE = "{kind} — personal use"
ACK_BUTTONS = (("Continue", True), ("Cancel", False))
# On a terminal too small for the text and the field together: read it first (Esc cancels).
ACK_READ_BUTTON = ("Continue", True)
SIGNIN_METHOD_TITLE = "{kind} — how to open the sign-in"
SIGNIN_METHODS = (("browser", "Open my browser on this computer"),
                  ("address", "Show an address to open on any device (SSH, no browser)"))
SIGNIN_RETURN = "Press Enter to return to claude-multi."
SIGNIN_RESULT_TITLE = "{kind}"
L_TITLE = "{kind}"
L_BODY_SIGNED_IN = "Signed in: {accounts}"
L_BODY_SEPARATE = "This sign-in is separate from your Claude Code login, which stays untouched."
L_BODY_NOT_SIGNED_IN = "Not signed in on this computer."
L_BUTTONS_SIGNED_IN = (("Close", None), ("Sign in again", "signin"), ("Sign out", "signout"))
L_BUTTONS_NOT = (("Close", None), ("Sign in", "signin"))
SIGNOUT_TITLE = "Sign out of your {kind}?"
SIGNOUT_BUTTONS = (("Cancel", False), ("Sign out", True))

# ---------------------------------------------------------------- profiles (the Profiles screen and the card)

PROFILES_TITLE = "profiles"
PROFILES_TITLE_FALLBACK = "profiles — fallback profiles (one provider each)"
PROFILES_KEYBAR_YOURS = (("Enter", "use"), ("N", "new"), ("E", "edit"), ("C", "copy"), ("R", "rename"),
                         ("X", "delete"), ("D", "default"), ("F", "fallback only"), ("B", "bindings"),
                         ("W", "get started"), ("?", "help"), ("Esc", "back"))
PROFILES_KEYBAR_SEED = (("Enter", "use"), ("N", "new"), ("E", "edit"), ("C", "copy"), ("U", "reseed"),
                        ("D", "default"), ("F", "fallback only"), ("B", "bindings"), ("W", "get started"),
                        ("?", "help"), ("Esc", "back"))
PROFILES_KEYBAR_UNLOADABLE = (("E", "edit file"), ("X", "delete"), ("N", "new"), ("?", "help"), ("Esc", "back"))
PROFILES_RESTORE_BINDING = ("U", "restore")
PROFILES_FILTER_OFF_LABEL = "all profiles"
PROFILES_HELP_TITLE = "profiles — help"
PROFILES_NONE = "no profiles"
PROFILES_NO_FALLBACK = "no profile is a fallback profile yet — E → General → primary provider makes one"
PROFILES_HELP = (
    "Profiles decide which model leads and which models the helper agents use.\n"
    "Enter uses the selected profile for this launch. D makes it your default: it is used in any\n"
    "directory with no recent profile, whenever it is connected (D on the default clears it).\n"
    "N creates one — from a shipped profile, from the profile on the card, or built from your\n"
    "connected providers (you see it before anything is saved). C copies, R renames, X deletes\n"
    "(a copy is kept). E edits.\n"
    "Shipped profiles (seed) can be edited; U shows what a newer shipped version changes and keeps\n"
    "your version as a backup.\n"
    "F lists fallback profiles: each uses one provider, so you can switch to one when another\n"
    "provider is down or out of quota (in a session: /cm fallback). A profile becomes a fallback\n"
    "in its editor: E → General → primary provider.\n"
    "B edits named bindings: a model and effort saved under a name that profiles use for a role;\n"
    "changing one offers the change to every profile that uses it and to their sessions.\n"
    "connected means every model it uses is served here; needs … names the one fix.\n"
    "A profile that cannot be loaded is listed with its file and line: E edits it, X deletes it."
)
PROFILES_USED = "card: {name} — for this launch; D makes it the default"
NEW_TITLE = "new profile — start from"
NEW_ITEMS = ("A shipped profile…", "The profile on the card ({card})",
             "My connected providers (built for you; preview first)")
NEW_SEED_TITLE = "new profile — from a shipped profile"
NAME_TITLE = "name the new profile"
NAME_BUTTONS = (("OK", True), ("Cancel", False))
KEEP_FALLBACK_TITLE = "Keep it as the fallback for {provider}?"
KEEP_FALLBACK_BODY = ("Fallback profiles are what F lists and what /cm fallback switches to when "
                      "{provider} is down or out of quota. Keep one fallback per provider.")
KEEP_FALLBACK_BUTTONS = (("No", False), ("Keep", True))
STARTER_PREVIEW_TITLE = "new profile from your connected providers"
MAKE_DEFAULT_TITLE = "Make {name} your default profile?"
MAKE_DEFAULT_BUTTONS = (("Not now", False), ("Make default", True))
RENAME_TITLE = "Rename {name}?"
RENAME_BUTTONS = (("Cancel", False), ("Rename…", True))
# The editor's Rename: the new name is already chosen.
EDITOR_RENAME_TITLE = "Rename {old} to {new}?"
EDITOR_RENAME_BUTTONS = (("Cancel", False), ("Rename", True))
DELETE_TITLE = "Delete {name}?"
DELETE_BUTTONS = (("Cancel", False), ("Delete", True))
REFRESH_TITLE = "Update {names} to the shipped version?"
REFRESH_BODY = ("You have not changed {these}; shipped updates: {versions}. Following sessions get "
                "the change at their next resume, or now if you choose next. A copy of each is kept.")
REFRESH_BUTTONS = (("Cancel", False), ("Update", True))
RESEED_VIEW_TITLE = "reseed {name} — your version → shipped version {v}"
PROFILES_REFRESHED = "Updated {names} to the shipped version; a copy of each is kept."
DEFAULT_SET = "{name} is your default profile — used in directories with no recent profile."
DEFAULT_NOT_READY_TITLE = "{name} is not connected here"
DEFAULT_NOT_READY_BODY = "{why}. Make it the default anyway? It is used wherever it is connected."
DEFAULT_NOT_READY_BUTTONS = (("Cancel", False), ("Make default", True))
# The editor's texts (the changed-on-disk dialog, the save choices of a
# shipped profile) live with the editor stack in tui.py.
RAW_EDIT_NO_EDITOR = "Set VISUAL or EDITOR to edit the file, or X deletes it (a copy is kept)."
RAW_EDIT_KEPT = "your edit is kept at {tmp}"
RAW_EDIT_TITLE = "profile {name} — not saved"
SETTINGS_DEFAULT_TITLE = "default profile"
SETTINGS_DEFAULT_CONNECTED = "connected"
