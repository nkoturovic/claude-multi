# Plain Claude Code alongside claude-multi

Most people who use claude-multi also keep using plain `claude`, the
Claude Code they installed from Anthropic. This page explains where the
two stay apart, where they meet, and how to switch between them safely.

## Apart by design

- claude-multi does not replace, wrap or reconfigure your Claude Code. It
  runs its **own copy** of the Claude Code version its release pins
  (`~/.local/share/claude-multi/claude/<version>/claude`), checked by size
  and sha256 on every launch. Your installation is only read (to copy a
  matching build when there is one), never moved, linked or changed.
- claude-multi never writes `~/.claude/settings.json`, `~/.claude/agents/`
  or other global Claude Code configuration, never sets gateway variables
  for plain sessions, never reads transcripts and never deletes anything
  under `~/.claude`.
- Plain `claude` uses your own Anthropic login and never goes through
  claude-multi's gateway. Provider keys and account sign-ins you connect
  in claude-multi are separate from your Claude Code login: signing in to
  claude-multi with your Claude account does not log you in or out of
  Claude Code, and the reverse.
- Managed sessions get claude-multi's settings, agents and hooks from a
  per-session scope passed on the command line, not from your global
  files.

## Where they meet

Both use Claude Code, so some of Claude Code's own behaviour crosses the
line. These are not claude-multi writing your configuration, but they
affect it:

- **`/model` with Enter saves globally.** Inside a managed session:

  - /model: press s to switch for this session only — Enter saves the choice into ~/.claude/settings.json, which plain claude then inherits

  A claude-multi model saved that way means nothing to plain `claude`.
  claude-multi warns you in the session, and `claude-multi doctor` names
  the key and offers the revert; it never writes your settings itself.
- **Shared settings and data.** Managed sessions read your
  `~/.claude/settings.json`, project `.claude/` settings, MCP servers and
  memory like plain sessions do, and store transcripts in the same place.
  Settings that would defeat a managed session's policy (hooks turned
  off, a model or credential override) are reported by doctor, never
  edited.
- **`CLAUDE_CONFIG_DIR`.** Managed sessions unset it: they always use
  `~/.claude`. If you run plain `claude` with another configuration
  directory, managed sessions do not see that directory's settings, MCP
  servers or sessions.
- **The background supervisor.** Claude Code's supervisor ("daemon") can
  adopt a session you background, and it relaunches adopted sessions on
  your own `claude` after your Claude Code updates. A managed session
  keeps its gateway routing when that happens (its settings carry the
  gateway address and token helper), but it then runs your client
  instead of the pinned one: its hooks tell you on every prompt to `/exit`
  and resume it with `claude-multi -r <id>`. See
  [the client skew notes](reference/compatibility.md#client-skew).

## Updates

Your Claude Code updates itself on its own schedule, as you configured it;
claude-multi does not turn that updater on or off and does not promise
that your installation is current. Managed sessions run the pinned copy
with Claude Code's updater disabled, so the managed copy never changes by
itself. A newer Claude Code reaches managed sessions with a claude-multi
release that verifies it ([reference/compatibility.md](reference/compatibility.md)).
Until then doctor shows an Attention line when the pin is more than 30
days old or your own `claude` is newer: a fact, with nothing to run.

## API keys in the two worlds

claude-multi reads provider keys only from its own key file:
`CLAUDE_MULTI_SECRET_ENV` when set (the supervised gateway service ignores
it), else the file `claude-multi setup --keys-file <file>` selected
(recorded in `~/.config/claude-multi/secret-file.json`), else
`~/.config/claude-multi/secrets/provider-keys.env`. It never takes keys
from your shell environment, and managed sessions remove API-key
variables from their own environment (except the names you keep in
Settings → kept environment). Plain `claude` keeps reading whatever your
shell and Claude Code configuration give it.

## Forks

When a second process opens a session another process owns (typically a
backgrounded session you re-enter from a menu), Claude Code **forks** it:
a new session id with a copy of the transcript, after which the two
branches are independent. Both stay on disk. For a managed session,
claude-multi notices the fork and asks you to adopt or discard it before
the parent resumes ([guides/sessions.md](guides/sessions.md#native-forks)).
To avoid forks, exit a backgrounded session before resuming it.

## Which tool when

| Want | Use |
| --- | --- |
| plain Claude Code, everything native, your own login | `claude` |
| one lead on any connected model, recorded and resumable | `claude-multi direct` |
| a lead plus `cm-*` agents from a profile, changed live with `/cm` | `claude-multi` |
| Remote Control, `/schedule`, claude.ai connectors | plain `claude` (managed sessions authenticate through their token helper, which disables these) |

A plain session can be adopted later
(`claude-multi sessions link <id> --profile <name>`, or **L** on the
sessions screen), and any session can be resumed natively with
`claude --resume <runtime-id>`. Resuming a managed session that way
loads its settings but skips claude-multi's client verification and
record checks: prefer `claude-multi -r <id>`.

## Safe switching

- Exit a managed session before you resume it in plain `claude`, and the
  reverse; never reattach to a running (●) session from another tool.
- After a Claude Code update of your own, resume managed sessions with
  `claude-multi -r <id>` so they run the verified copy again.
- If plain `claude` misbehaves after a managed session, check
  `claude-multi doctor` for a claude-multi model saved in your settings;
  Claude Code's own `claude doctor` and `/status` diagnose plain sessions.
