"""Terminal-safe text shared by hooks and interactive presentation."""


# The card and sessions help name the symptom index; its
# path is printed by `claude-multi --help` (the asset root differs per install).
CHEATSHEET_HINT = "Symptom → command: CHEATSHEET.md (claude-multi --help prints its path)."


# ---------------------------------------------------------------------------
# Drawing helpers and widgets.


def visible_text(text: str) -> str:
    """Render external text inert for terminal display.

    External strings — session cwd, project paths, agent names/files,
    composition names from user files — may contain terminal control bytes
    (ESC/OSC payloads, newlines). Every such string passes through this ONE
    sanitizer at the render points before it reaches a terminal: C0 controls
    (including ESC -> ``^[`` and newline -> ``^J``) become caret notation,
    DEL becomes ``^?``, and C1 controls become ``\\xNN`` hex. Launcher-owned
    ANSI styling is never routed through this function.
    """

    out: list[str] = []
    for ch in str(text):
        code = ord(ch)
        if code < 0x20:
            out.append("^" + chr(code + 0x40))
        elif code == 0x7F:
            out.append("^?")
        elif 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def visible_message(text: object) -> str:
    """Newline-preserving variant for launcher-owned multi-line messages.

    Apply at message write points (e.g. the main error writer): each line is
    neutralized independently (external payloads on any line stay inert),
    while launcher-owned line structure survives — a multi-line error never
    collapses into a single ``^J``-mangled line.
    """

    return "\n".join(visible_text(line) for line in str(text).split("\n"))
