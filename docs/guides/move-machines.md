# Move to another computer

`claude-multi export` writes your configuration as one portable file;
`claude-multi import <file>` previews it on the new computer and applies what is
ready. Credentials, sign-ins and approvals do not travel: you reconnect
them on the new computer, deliberately.

## What travels, and what does not

| In the export | Not in the export |
| --- | --- |
| your profiles (and references to shipped ones) | API keys, the key file, account sign-ins |
| named bindings | session records, transcripts, logs |
| the portable Settings | qualification evidence |
| the providers you declared | the gateway's state and local key |
| re-approval requests: the route approvals, admissions and transport choices you held | your host preferences and the choices on this computer |

Imported trust is never active: a credential-route approval must be obtained
locally on the new computer. Any carried admission request is only an optional
badge reminder, not a prerequisite for importing or binding the model.
Qualification evidence is never imported as trusted evidence, and import
never sends a diagnostic request or implicitly approves a key's destination.

A provider you added from a preset travels as its declaration, with the
key name it uses (its own, such as `DASHSCOPE_API_KEY_STUDIO_2`, when the
preset was added twice). An API key in use for Anthropic or OpenAI
travels as a transport request: on the new computer
`claude-multi providers transport openai api-key` (or `anthropic`)
selects it again and takes the key.

Sessions do not move. A session's transcript is Claude Code's, kept under
Claude Code's own directory; claude-multi neither exports nor imports it.
Finish or end your sessions before you move, and start new ones on the new
computer.

## 1. Export

```bash
claude-multi export --out <file>
```

The file is written atomically with mode 0600, and an export receipt is
recorded. Without `--out` the export goes to standard output. Export
refuses to write into a credential location or into claude-multi's own
state and configuration folders, and refuses a document that would carry
anything that looks like a secret. Inspect the file before you copy it:
it is plain JSON.

## 2. Install on the new computer

Install claude-multi there ([install pages](../USAGE.md#start-here)) and
run setup's first steps (Claude Code and the gateway).

## 3. Preview the import

```bash
claude-multi import <file>
```

The preview checks every item against the new computer (its release,
its stores and its providers) and writes nothing. It ends with a
**reconnect checklist**: the API keys to set, the accounts to sign in and
the routes to approve, each with its command. Valid local lines do not wait
for admission or qualification; any suggested diagnostics are optional.

## 4. Apply

```bash
claude-multi import <file> --apply
```

It asks once (in a terminal outside Claude Code) and writes the ready
items through the ordinary stores; items that are not ready are listed
with the reason.

## 5. Reconnect and verify

Work through the checklist: set keys (`claude-multi providers set-key
<provider>`), sign in (`claude-multi providers sign-in anthropic`, or
`openai`), select an API-key transport again and approve credential routes.
Admission is optional; qualification can be run separately with human consent
to its request plan, default **No**. Neither is required for leads or agents.
Then:

```bash
claude-multi doctor
```

Profiles whose providers are not connected yet show as not connected on
the card until you reconnect them.

Do not copy sign-in records between computers: sign in again on the new
one. Your API keys are yours to carry: type them again, or copy your key
file privately into a folder sessions cannot read (mode 0600, under
`~/.config/claude-multi/`) and select it with
`claude-multi setup --keys-file <file>`
([the key file](../reference/settings.md#the-api-key-file)).

Moving stops nothing on the old computer: its gateway and sessions keep
running there until you stop them or uninstall
([uninstall.md](../uninstall.md)).

## Moving to an older release or rolling back

The record and configuration formats have not been migrated for permissive
bindings. That does not guarantee older behavior: an older binary may refuse
broader family labels, unadmitted or unqualified bindings, legacy custom agents
or other newly allowed profiles. Review those bindings before rollback; do not
edit session records to bypass a refusal. Running sessions keep their recorded
selectors, classes and launch fence until normal resume.

`models revoke` now removes only the admission badge. It does not disable a
model or erase evidence, and cannot be used to preserve an older release's
per-model off behavior. Disable a provider or remove the binding/declaration
if you want to stop using it.
