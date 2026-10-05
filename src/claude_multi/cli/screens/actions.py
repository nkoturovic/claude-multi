"""The TUI's actions by stable id: one row per key a screen's bar offers.

The screens dispatch on keys and draw their bars from the text constants;
this table names what each key does with an id that stays the same when a
label changes, and the command that does the same from a shell when there
is one. ``tests/test_tui_actions.py`` checks it against the bars in both
directions: every key a bar shows has a row here, and every row's key is on
one of that screen's bars. Pure data: nothing here is imported by a screen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    """One key of one screen: ``id`` is ``<screen>.<verb>``; ``command`` is
    the shell equivalent (None when the action exists only in the TUI)."""

    id: str
    key: str
    does: str
    command: str | None = None


def _rows(screen: str, *rows: tuple[str, str, str] | tuple[str, str, str, str | None]) -> tuple[Action, ...]:
    return tuple(Action(f"{screen}.{row[0]}", row[1], row[2], row[3] if len(row) > 3 else None) for row in rows)


_HELP = ("help", "?", "this screen's help")

ACTIONS: dict[str, tuple[Action, ...]] = {
    "card": _rows(
        "card",
        ("launch", "Enter", "launch the profile on the card", "claude-multi --profile <name>"),
        ("update", "U", "update claude-multi (shown when an update applies)", "claude-multi update"),
        ("get-started", "W", "open Get started", "claude-multi setup"),
        ("edit", "E", "edit the profile on the card", "claude-multi profile edit <name>"),
        ("next-profile", "Tab", "show the next profile (Shift-Tab: the previous one)"),
        ("profiles", "P", "open Profiles", "claude-multi profile list"),
        ("direct", "D", "open Direct", "claude-multi direct <model>"),
        ("details", "V", "every card row in full, the /model lead set and the kept environment"),
        ("sessions", "S", "open Sessions", "claude-multi sessions list"),
        ("providers", "G", "open Providers", "claude-multi providers list"),
        ("models", "M", "open Models", "claude-multi models list"),
        ("settings", "O", "open Settings"),
        ("doctor", "H", "run doctor here, then the gateway's actions", "claude-multi doctor"),
        _HELP,
        ("quit", "Esc", "leave without launching"),
    ),
    "resume-card": _rows(
        "resume-card",
        ("resume", "Enter", "resume the session with the lineup shown", "claude-multi -r <session>"),
        ("update", "U", "update claude-multi (shown when an update applies)", "claude-multi update"),
        ("details", "V", "every card row in full, the /model lead set and the kept environment"),
        ("sessions", "S", "open Sessions", "claude-multi sessions list"),
        ("doctor", "H", "run doctor here, then the gateway's actions", "claude-multi doctor"),
        _HELP,
        ("cancel", "Esc", "go back without resuming"),
    ),
    "sessions": _rows(
        "sessions",
        ("resume", "Enter", "resume the selected session", "claude-multi -r <session>"),
        ("details", "V", "identity, directory, lineup, pending change, drift and usage", "claude-multi sessions show <session>"),
        ("lineup", "T", "change the session's lineup, with its effect first", "claude-multi lineup --session <session>"),
        ("follow", "F", "follow the session's profile or pin its lineup", "claude-multi lineup --session <session> follow"),
        ("rename", "R", "rename the session (shown here only)"),
        ("forget", "X", "forget the record and its generated scope", "claude-multi sessions forget <session>"),
        ("stop", "E", "stop a session running in the background", "claude-multi sessions stop <session>"),
        ("mark-ended", "M", "record the end of a session that exited without one", "claude-multi sessions mark-ended <session>"),
        ("repair", "P", "rebuild the session's generated scope", "claude-multi doctor --repair <session>"),
        ("link", "L", "manage a native session", "claude-multi sessions link <runtime-id>"),
        ("directories", "C", "this directory or all directories", "claude-multi sessions list"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "lineup-dialog": _rows(
        "lineup-dialog",
        ("apply", "Enter", "apply the previewed change", "claude-multi lineup --session <session>"),
        ("target", "Tab", "move to the next kind of change"),
        ("choose", "← →", "cycle the profiles or fallback providers"),
        ("pick", "Space", "list the choices of the current target"),
        _HELP,
        ("cancel", "Esc", "close without writing"),
    ),
    "direct": _rows(
        "direct",
        ("launch", "Enter", "launch the model as a direct session", "claude-multi direct <model>"),
        ("save", "Tab", "save the choice as a lead-only profile"),
        ("providers", "G", "open Providers", "claude-multi providers list"),
        ("effort", "← →", "choose the effort of a gateway-effort model"),
        ("gateway", "W", "start the local gateway or read its log", "claude-multi gateway start"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "providers": _rows(
        "providers",
        ("primary", "Enter", "the row's main action: set a key, sign in, connect, approve or details"),
        ("apply", "P", "make the gateway serve your changes", "claude-multi providers apply"),
        ("set-key", "K", "set or replace an API key", "claude-multi providers set-key <provider>"),
        ("remove", "X", "remove the key, the sign-in or a provider you added", "claude-multi providers remove-key <provider>"),
        ("sign-in", "L", "sign in or out of an account provider", "claude-multi providers sign-in <provider>"),
        ("test", "T", "test the provider", "claude-multi providers test <provider>"),
        ("add-models", "A", "add models to the provider", "claude-multi models add"),
        ("toggle", "Space", "turn the provider on or off", "claude-multi providers enable <provider>"),
        ("details", "Q", "the provider's details", "claude-multi providers show <provider>"),
        ("edit", "E", "edit a provider you added", "claude-multi providers edit <provider>"),
        ("new", "N", "add a provider", "claude-multi providers add"),
        ("refresh", "R", "read the gateway again"),
        ("gateway", "W", "start the local gateway or read its log", "claude-multi gateway start"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "models": _rows(
        "models",
        ("primary", "Enter", "inspect a line, admit a new one, revoke it or approve its route", "claude-multi models show <key>"),
        ("qualify", "Q", "check a line against the gateway", "claude-multi models qualify <key>"),
        ("edit", "E", "edit a model you declared", "claude-multi models edit <key>"),
        ("remove", "X", "remove a model you declared", "claude-multi models rm <key>"),
        ("details", "V", "the line's details", "claude-multi models show <key>"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "settings": _rows(
        "settings",
        ("edit", "Enter", "edit the value, open the screen it summarizes or rotate the token"),
        ("reset", "R", "reset the value to its default (Cancel is focused first)"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "profiles": _rows(
        "profiles",
        ("use", "Enter", "use the profile for this launch"),
        ("new", "N", "create a profile", "claude-multi profile new <name>"),
        ("edit", "E", "edit the profile (or its file when it cannot be loaded)", "claude-multi profile edit <name>"),
        ("copy", "C", "copy the profile", "claude-multi profile duplicate <name> <new>"),
        ("rename", "R", "rename the profile", "claude-multi profile rename <name> <new>"),
        ("delete", "X", "delete the profile (a copy is kept)", "claude-multi profile rm <name>"),
        ("reseed", "U", "update a shipped profile, or restore one that cannot be loaded", "claude-multi profile reseed <name>"),
        ("default", "D", "make the profile the default", "claude-multi profile default <name>"),
        ("fallback", "F", "list only the fallback profiles"),
        ("bindings", "B", "edit the named bindings"),
        ("get-started", "W", "open Get started", "claude-multi setup"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "get-started": _rows(
        "get-started",
        ("open", "Enter", "open the selected step"),
        ("add-provider", "A", "add a provider", "claude-multi providers add"),
        ("profiles", "P", "open Profiles"),
        _HELP,
        ("card", "Esc", "go to the launch card"),
    ),
    "editor": _rows(
        "editor",
        ("change", "Enter", "change the selected row"),
        ("unbind", "U", "unbind the selected agent"),
        ("named-bindings", "N", "edit the named bindings"),
        ("routing", "R", "show the review routing"),
        ("save", "^S", "save the profile"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "binding-picker": _rows(
        "binding-picker",
        ("effort", "← →", "choose the effort"),
        ("choose", "Enter", "bind the model, use a named binding or turn the slot off"),
        ("details", "V", "the model's details"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "named-bindings": _rows(
        "named-bindings",
        ("edit", "Enter", "change the binding's model or effort"),
        ("add", "A", "add a named binding"),
        ("delete", "X", "delete the named binding"),
        _HELP,
        ("back", "Esc", "back"),
    ),
}


def action(action_id: str) -> Action:
    """The action with ``action_id`` (KeyError when there is none)."""

    found = next((row for row in ACTIONS.get(action_id.split(".", 1)[0], ()) if row.id == action_id), None)
    if found is None:
        raise KeyError(action_id)
    return found
