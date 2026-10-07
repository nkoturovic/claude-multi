"""Shared interactive presentation helpers."""

from __future__ import annotations

from claude_multi import errors as cli_errors
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import tui
from claude_multi import views
from typing import Any
from typing import Callable
from typing import Sequence
from typing import TextIO
import textwrap
import io
import re
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _output_palette(
    input_stream: TextIO, output_stream: TextIO, no_color: bool
) -> tui.Palette:
    """Palette for line-mode badge styling; mono unless the output is a tty."""

    try:
        is_tty = bool(output_stream.isatty())
    except (AttributeError, ValueError, OSError):
        is_tty = False
    if not is_tty:
        return tui.MONO_PALETTE
    # Line output never asks the terminal anything (a late reply would land
    # in the next prompt's input).
    return tui.detect_palette(
        no_color=no_color, tty_in=input_stream, tty_out=output_stream, query=False
    )


def _run_choice_list(
    win: Any,
    palette: tui.Palette,
    title: str,
    labels: Sequence[str],
    *,
    help_title: str,
    help_text: str,
    footer: Sequence[tuple[str, str]] = cli_text.NEEDS_CHOICE_KEYBAR,
    selected: int = 0,
    message: str = "",
) -> int | None:
    """One SelectList pass (Esc → None); ``?`` opens ``help_text``."""

    picker = tui.SelectList(title, [tui.SelectItem(label) for label in labels], footer=footer, selected=selected)
    picker.message = message
    index = picker.run(
        win,
        palette,
        on_help=lambda: _help_modal(win, palette, help_title, help_text, lambda w: picker.draw(w, palette)),
    )
    return index if isinstance(index, int) else None


def _run_screen_curses(
    run: Callable[[Any], Any],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    palette: tui.Palette,
    what: str,
) -> Any:
    """Run one screen in its own curses session; the result after teardown.

    ``tui.CursesError``/``OSError`` raised before curses started propagate
    (the caller falls back to its line flow); raised after, they become a
    ``CLIError`` (the screen may already have written through a store API).
    """

    started = False

    def app(win: Any) -> Any:
        nonlocal started
        started = True
        return run(win)

    try:
        return tui.run_curses_on_streams(app, input_stream, output_stream, palette=palette)
    except (tui.CursesError, OSError) as exc:
        if not started:
            raise
        raise cli_errors.CLIError(f"{what} stopped: {exc}") from exc


def _screen_too_small(win: Any, palette: tui.Palette, what: str, rows: int, cols: int) -> None:
    """The too-small text: rows 1–2 at column 2; only Esc (and resize) act."""

    win.erase()
    tui.safe_add(win, 1, 2, tui.TOO_SMALL.format(what=what), palette.attr("warn"))
    tui.safe_add(win, 2, 2, tui.TOO_SMALL_HINT.format(cols=cols, rows=rows), palette.attr("dim"))
    win.refresh()


def _wrapped(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(20, width), break_on_hyphens=False) or [""]


MODAL_MAX_WIDTH = 72


def modal_lines(text: str, win: Any) -> list[str]:
    """A modal body: each paragraph (``\\n``-separated; an empty line stays
    empty) wrapped to the box width at this terminal size."""

    width = max(20, min(MODAL_MAX_WIDTH, win.getmaxyx()[1] - 10))
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        indent = paragraph[: len(paragraph) - len(paragraph.lstrip())]
        lines.extend(textwrap.wrap(paragraph, width, subsequent_indent=indent, break_on_hyphens=False)
                     or [""])
    return lines


def _help_modal(win: Any, palette: tui.Palette, title: str, text: str, background: Any) -> None:
    """A help dialog that scrolls at any size: each line of the help text is
    a paragraph or an item, wrapped to the terminal (never cut at a hyphen),
    blank lines kept."""

    wrap = max(20, min(76, win.getmaxyx()[1] - 8))
    _ScrollModal(title, _wrap_help_lines(_unwrap_prose(text), wrap)).run(win, palette, background=background)


def _unwrap_prose(text: str) -> str:
    """Join a help text's hard-wrapped continuation lines (a line that does
    not start a new item: no key, bullet or mark at its start) to the line
    before, so the help re-flows at every width while its items stay
    separate lines."""

    out: list[str] = []
    for line in text.split("\n"):
        if out and out[-1].strip() and line.strip() and not _starts_item(line):
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return "\n".join(out)


# A help line that starts an item rather than continuing the sentence above:
# a key and a dash ("T — …"), a one-letter key and its verb ("Q qualifies"),
# a key word, a slash command the line is about, a bullet or a rule.
_ITEM_START = re.compile(
    r"(?:\S+ — |\S+ → |[A-Z?] [a-z]|(?:Esc|Enter|Tab|Space|Shift-Tab|Marks:|CLI:)(?:\s|$)|/model\b|/cm\b|[·•]|- |-- )")


def _starts_item(line: str) -> bool:
    stripped = line.lstrip()
    return line != stripped or bool(_ITEM_START.match(stripped))


def _default_effective(runtime: runtime_mod.Runtime) -> settings_mod.Effective:
    """Settings defaults over the merged providers/lines (a screen whose settings.json is bad)."""

    docs = runtime.ordinary_docs
    return settings_mod.effective(
        {"version": settings_mod.SETTINGS_DATA_VERSION},
        provider_ids=docs["providers"]["providers"],
        line_keys=(docs["models-v2"] if "models-v2" in docs else docs["models"])["models"],
    )


class _TextReportScreen:
    """A read-only list of report lines (the propagation report, §4.6)."""

    what = "the report"

    def __init__(self, title: str, lines: list[str], palette: tui.Palette):
        self.title = title
        self.lines = lines
        self.palette = palette
        self.offset = 0

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=3, list_rows=len(self.lines), detail_reserve=0,
            bar_rows=tui.KeyBar(cli_text.PROPAGATION_REPORT_KEYBAR).rows(width), cols=cli_text.PROPAGATION_MIN_COLS,
        ).as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            _screen_too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = tui.KeyBar(cli_text.PROPAGATION_REPORT_KEYBAR)
        tui.safe_add(win, 1, 2, views.clip(self.title, width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        visible = max(1, height - bar.rows(width) - 3)
        wrapped = [
            piece
            for line in self.lines
            for piece in (textwrap.wrap(line, max(20, width - 3), subsequent_indent="    ",
                                        break_on_hyphens=False) or [""])
        ]
        self.offset = max(0, min(self.offset, max(0, len(wrapped) - visible)))
        for y, line in enumerate(wrapped[self.offset:self.offset + visible], 3):
            tui.safe_add(win, y, 2, views.clip(line, width - 3))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def run(self, win: Any) -> None:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.offset = max(0, self.offset - 1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.offset += 1
            elif key.kind == "char" and key.ch == "?":
                _help_modal(win, self.palette, f"{self.title} — help", cli_text.PROPAGATION_REPORT_HELP, self._draw)


# A screen entry whose curses could not start falls back to its line flow.
_LINE_FALLBACK = object()
# The terminal-type notice is printed once per process.
_TERM_NOTICE_SHOWN: list[bool] = []


def _curses_ok(line: bool, input_stream: Any, output_stream: Any) -> bool:
    """The full screen only without ``--line`` and on capable streams; a
    terminal whose type is unset or unknown gets the line interface and one
    notice on stderr saying why (and which type to try)."""

    if line:
        return False
    if tui.streams_curses_capable(input_stream, output_stream):
        return True
    import os
    import sys

    notice = tui.term_notice(os.environ, input_stream, output_stream)
    if notice is not None and not _TERM_NOTICE_SHOWN:
        _TERM_NOTICE_SHOWN.append(True)
        try:
            sys.stderr.write(notice + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    return False


def _state_error_text(exc: BaseException) -> str:
    if isinstance(exc, state.StateError) and exc.strerror:
        return str(exc.strerror)
    return str(exc)


def _wrap_help_lines(text: str, width: int) -> list[str]:
    """Wrap each help line to ``width``; continuations indent two past the line's own indent.

    A rule line (``-- …``) is clipped instead: its dashes are decoration.
    """

    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("--"):
            lines.append(views.clip(line, width))
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines.extend(
            textwrap.wrap(line, width, subsequent_indent=indent + "  ", break_on_hyphens=False) or [""]
        )
    return lines


class _ScrollModal(tui.Modal):
    """A Close-only Modal whose body scrolls (↑/↓, j/k, PgUp/PgDn, Home/End).

    ``tui.Modal`` shows only the lines that fit; the card's help is longer
    than an 80x24 Modal, so this one keeps an offset and marks the hidden
    part on the button row.  Enter closes (True), Esc closes (None).
    """

    SCROLL_HINT = "↑↓ more"

    def __init__(self, title: str, lines: Sequence[str], *, button: tuple[str, Any] = ("Close", True)):
        super().__init__(title, lines, buttons=(button,))
        self.offset = 0

    def _room(self, box_h: int) -> int:
        return max(0, box_h - 5)

    def draw(self, win: Any, palette: tui.Palette) -> None:
        top, left, box_h, box_w = self._geometry(win)
        room = self._room(box_h)
        self.offset = max(0, min(self.offset, len(self.lines) - room))
        blank = " " * (box_w - 2)
        for row in range(top + 1, top + box_h - 1):
            tui.safe_add(win, row, left + 1, blank)
        horizontal = "+" + "-" * (box_w - 2) + "+"
        tui.safe_add(win, top, left, horizontal, palette.attr("accent"))
        for row in range(top + 1, top + box_h - 1):
            tui.safe_add(win, row, left, "|", palette.attr("accent"))
            tui.safe_add(win, row, left + box_w - 1, "|", palette.attr("accent"))
        tui.safe_add(win, top + box_h - 1, left, horizontal, palette.attr("accent"))
        tui.safe_add(win, top + 1, left + 2, views.clip(self.title, box_w - 4),
                     palette.attr("accent") | tui.curses.A_BOLD)
        for y, line in enumerate(self.lines[self.offset:self.offset + room], top + 2):
            tui.safe_add(win, y, left + 2, views.clip(line, box_w - 4))
        label = self.buttons[0][0]
        tui.safe_add(win, top + box_h - 2, left + 2, f"[ {label} ]",
                     palette.attr("accent") | tui.curses.A_REVERSE)
        if len(self.lines) > room:
            hint = self.SCROLL_HINT
            tui.safe_add(win, top + box_h - 2, left + box_w - 2 - len(hint), hint, palette.attr("dim"))
        win.refresh()

    def run(
        self,
        win: Any,
        palette: tui.Palette,
        background: Callable[[Any], None] | None = None,
    ) -> Any:
        while True:
            self.draw(win, palette)
            key = tui.read_key(win)
            room = max(1, self._room(self._geometry(win)[2]))
            if key.kind == "resize":
                if background is not None:
                    background(win)
                else:
                    win.erase()
                continue
            if key.kind == "esc":
                return None
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "enter":
                return self.buttons[0][1]
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.offset -= 1
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.offset += 1
            elif key.kind == "pageup":
                self.offset -= room
            elif key.kind == "pagedown" or (key.kind == "char" and key.ch == " "):
                self.offset += room
            elif key.kind == "home":
                self.offset = 0
            elif key.kind == "end":
                self.offset = len(self.lines)
            self.offset = max(0, min(self.offset, max(0, len(self.lines) - room)))


class _ProviderForm(tui.OnboardingForm):
    """The mixed-kind draft keeps authentication valid after backtracking."""

    def _draw(self, win):
        choices = views.endpoint_auth_choices(self.inputs["kind"].value)
        self.fields = tuple((name, label, default, choices if name == "auth" else options)
                            for name, label, default, options in self.fields)
        auth = self.inputs["auth"]
        if auth.value not in {value for value, _label in choices}:
            auth.value = choices[0][0]
        super()._draw(win)


class OnboardingActions:
    """Curses form/action adapter. Draft navigation never writes or calls a provider."""

    def __init__(self, runtime, win, palette):
        self.runtime, self.win, self.palette = runtime, win, palette

    def confirm(self, text):
        return tui.TextView("Confirm explicit action", text.splitlines(),
                            palette=self.palette, confirm=True).run(self.win)

    def preview(self, text):
        return tui.TextView("Declaration preview", text.splitlines(), palette=self.palette, confirm="declare").run(self.win)

    def show(self, title, lines):
        tui.TextView(title, lines, palette=self.palette).run(self.win)

    def invoke(self, argv):
        import claude_multi.cli.onboarding as onboarding
        output = io.StringIO()
        try:
            code = onboarding.invoke(self.runtime, argv,
                                     confirm=lambda text: self.confirm(output.getvalue() + "\n" + text), output=output)
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            code = 1
            output.write(str(exc))
        self.show("Action result", output.getvalue().splitlines())
        return code

    def offer_key(self, provider_id):
        """After a keyed N: when the key is not set, the key modal (masked;
        saving reloads the gateway; Cancel writes nothing)."""
        import claude_multi.cli.onboarding as onboarding
        import claude_multi.cli.screens.providers as screens_providers
        try:
            status = onboarding.key_status(self.runtime, provider_id)
        except (cli_errors.ClaudeMultiError, ValueError, OSError):
            return
        if status is None or status[1]:
            return
        outcome = screens_providers.ConnectActions(self.runtime, self.win, self.palette).set_key(provider_id)
        if outcome is not None:
            self.show(cli_text.KEY_MODAL_TITLE.format(display=provider_id), [outcome.text])

    def new_provider(self, *, manual_only=False):
        from claude_multi import catalog, operator
        choices = ["manual", *sorted(operator.preset_paths(self.runtime.asset_root))]
        choice = 0
        if not manual_only:
            choice = tui.SelectList("new provider — preset or manual", [tui.SelectItem(x) for x in choices],
                                    footer=(("Enter", "choose"), ("?", "help"), ("Esc", "back"))).run(
                self.win, self.palette, help_text=cli_text.PROVIDERS_HELP)
        if choice is None:
            return
        audited = catalog.keyed_compat_audited(self.runtime.catalog.docs.get("gateway"))
        if choice:
            name = choices[choice]
            fields = (("id", "Provider id", name, ()), ("url", "Base URL override (blank keeps preset)", "", ()))
            values = tui.OnboardingForm("new provider — preset", fields, palette=self.palette,
                                       help_text="Preset kinds stay authoritative. providers add --preset NAME").run(self.win)
            if values is None:
                return
            argv = ["providers", "add", "--preset", name, "--as", values["id"]]
            if values["url"]:
                argv += ["--base-url", values["url"]]
        else:
            from claude_multi import secret_store

            help_text = cli_text.PROVIDERS_HELP if audited else (
                cli_text.OPENAI_COMPAT_CLOSED_FORM_NOTE + "\n" + cli_text.PROVIDERS_HELP)
            # The key's name is checked at its field and never drawn unless it
            # reads as a name: a pasted key shows nowhere, not even in part.
            values = _ProviderForm(
                "new provider", views.provider_form_fields(keyed_audited=audited), palette=self.palette,
                help_text=help_text,
                checks={"secret": lambda text: secret_store.secret_name_problem(text.removeprefix("env:"))
                        if text else None},
                shown={"secret": secret_store.name_field_shown}).run(self.win)
            if values is None:
                return
            if values["auth"] != "none":
                # Checked again before any command or preview is built.
                problem = secret_store.secret_name_problem(values["secret"].removeprefix("env:"))
                if problem is not None:
                    self.show(cli_text.PROVIDER_SECRET_NAME_TITLE, [problem, "", cli_text.PROVIDER_SECRET_NAME_HINT])
                    return
            argv = ["providers", "add", values["id"], "--kind", values["kind"], "--base-url", values["url"],
                    "--auth", values["auth"], "--family", values["family"]]
            if values["auth"] != "none":
                argv += ["--secret-ref", "env:" + values["secret"].removeprefix("env:")]
            if values["auth"] == "header":
                argv += ["--header", "x-api-key"]
            if values["contracts"]:
                argv += ["--contracts", values["contracts"]]
            if values["listing"]:
                argv += ["--listing-url", values["listing"], "--listing-shape", values["shape"],
                         "--listing-auth", values["listing_auth"]]
        if self.preview("Declare this provider? No credentials are included.\n" + "\n".join(argv)):
            if self.invoke(argv) == 0:
                self.offer_key(values["id"])

    def model_form(self, provider_id, line=None, key="", *, edit=False):
        from claude_multi import discovery, operator
        import claude_multi.cli.onboarding as onboarding
        line = dict(line or {})
        provider = self.runtime.lineup_catalog().providers[provider_id]
        # The model declaration owner chooses the same reviewed effort default
        # for both manual and listing paths; forms never invent a contract.
        if not line.get("efforts"):
            efforts, default = discovery.declaration_efforts(provider, None, None, self.runtime.catalog.agent_efforts)
            line.update(efforts=efforts, default_effort=default)
        family = provider.get("independence_family", "unknown")
        fields = views.model_form_fields(line, key=key, family=family, edit=edit)
        values = tui.OnboardingForm("edit model" if edit else "declare model", fields,
                                   palette=self.palette, help_text=(
            "models add PROVIDER WIRE --as custom-NAME --context N --source docs --source-ref 'URL, date'\n"
            "custom- reserves local ids against future catalog keys; choose your display name freely.\n"
            "A blank new key derives from the wire id; editing keeps the key immutable.\n"
            "Capabilities and roles are recommendations, not requirements for explicit bindings.\n"
            "Family labels are preserved; unknown/unrecognized labels do not establish review independence.\n"
            "Optional: models admit KEY; models qualify KEY --agents --tool-choice forced|auto.\n"
            "Editing can stale admission/evidence. A smaller declared bound is not a per-agent window:\n"
            "the client may still use its 200K class and overflow that bound. No silent protocol fallback."
        )).run(self.win)
        if values is None:
            return
        output = io.StringIO()
        try:
            if edit and values.get("key", key) != key:
                raise ValueError("Edit keeps the local key; declare a new line to rename it")
            model_key = key if edit else values["key"]
            if not edit and not model_key:
                lcat = self.runtime.lineup_catalog()
                taken = set(lcat.lines) | set(lcat.retired) | {k.split("@", 1)[0] for k in lcat.retired}
                model_key = discovery.derived_key(values["wire"], taken)
                if model_key is None:
                    raise ValueError("no valid key derives from this wire id — enter a custom- local key")
            pairs = [x.strip().split("=", 1) for x in values["efforts"].split(",") if x.strip()]
            if not pairs or len({len(x) for x in pairs}) != 1:
                raise ValueError("Every effort needs a contract, or none does")
            efforts = {x[0]: x[1] for x in pairs} if len(pairs[0]) == 2 else [x[0] for x in pairs]
            if (line.get("wire_model") and values["wire"] != line["wire_model"]
                    and values["source"] in {"listing", "registry"}):
                raise ValueError("Listing/registry prefill belongs to the selected wire; use manual entry with explicit provenance for another id")
            declared = int(values["context"])
            previous_context = line.get("context", {})
            if previous_context.get("source") == "listing" and declared > previous_context.get("declared_tokens", declared):
                raise ValueError("Cannot raise listing context here; use discover --add --context N --over-listed REASON")
            drafted = {**line, "wire_model": values["wire"], "display": values["display"] or values["wire"],
                       "efforts": efforts, "default_effort": values["default"],
                       "context": {"declared_tokens": declared, "source": values["source"], "source_ref": values["ref"]}}
            if values["output"]:
                output_tokens = int(values["output"])
                old_output = line.get("output", {})
                drafted["output"] = (dict(old_output) if old_output.get("declared_tokens") == output_tokens else
                                     {"declared_tokens": output_tokens, "source": "operator"})
            else:
                drafted.pop("output", None)
            if not drafted["context"]["source_ref"]:
                drafted["context"].pop("source_ref")
                if "output" in drafted:
                    drafted["output"].pop("source_ref", None)
            drafted["family"] = values["family"]
            agents = values["capabilities"] == "agents"
            drafted["capabilities"] = ["lead", "agents"] if agents else ["lead"]
            drafted["roles"] = ("all" if values["roles"] == "all" else
                                [x.strip() for x in values["roles"].split(",") if x.strip()])
            if self.preview("Declaration preview — admission and diagnostics are optional; route approval is separate.\n" +
                            operator.document_bytes({model_key: drafted}).decode()):
                if edit:
                    onboarding.edit_line(self.runtime, key, drafted, confirm=self.confirm, output=output)
                else:
                    onboarding.declare(self.runtime, provider_id, model_key, drafted, output=output)
            else:
                return
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            output.write(str(exc))
        self.show("Declaration result", output.getvalue().splitlines())

    def add_models(self, provider_id, *, mode=None):
        """A (models for a provider): a consented listing or manual entry;
        ``mode`` ``"listing"``/``"manual"`` skips the chooser."""
        from claude_multi import discovery
        import claude_multi.cli.onboarding as onboarding
        if mode is not None:
            mode = 1 if mode == "manual" else 0
        else:
            items = [tui.SelectItem("List models (explicit consent)"), tui.SelectItem("Manual entry (no listing)")]
            if provider_id == "openrouter":
                items.append(tui.SelectItem("Add stealth id (optional metadata lookup)"))
            mode = tui.SelectList("add models", items,
                                  footer=(("Enter", "choose"), ("?", "help"), ("Esc", "back"))).run(
                                      self.win, self.palette, help_text=cli_text.PROVIDERS_HELP)
        if mode is None:
            return
        if mode == 1:
            self.model_form(provider_id)
            return
        if mode == 2:
            self.stealth_model()
            return
        try:
            observed = onboarding.listing(self.runtime, provider_id, confirm=self.confirm,
                                          notice=lambda text: self.show("OpenRouter listing", [text]))
            if observed is None:
                return
            call, result = observed
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            self.show("Listing unavailable — manual entry is available", [str(exc)])
            self.model_form(provider_id)
            return
        entries = result.entries
        # checked=None: the widget draws its own toggles ([x] after Space);
        # a fixed checked=False would toggle invisibly.
        labels = [entry["id"] for entry in entries]
        if provider_id == "openrouter":
            labels = [discovery.entry_text(row).replace("\t", "  ")
                      for row in onboarding.listing_rows(self.runtime, provider_id, entries)]
        picker = tui.SelectList("advertised models — nothing admitted",
                                [tui.SelectItem(label) for label in labels], multi=True,
                                footer=(("Space", "select"), ("Enter", "declare"), ("M", "manual entry"),
                                        ("?", "help"), ("Esc", "back")))
        chosen = picker.run(self.win, self.palette, shortcuts={"M": "manual", "m": "manual"},
                            help_text="Listings are observations, not admission. Select models, then review each declaration."
                            + ("\n\n" + discovery.OPENROUTER_HELP + "\n\n" + "\n\n".join(labels)
                               if provider_id == "openrouter" else ""))
        if chosen == "manual":
            self.model_form(provider_id)
        elif chosen == []:
            self.show("Nothing selected", ["No model was selected (Space selects); nothing was declared."])
        elif chosen is not None:
            for index in chosen:
                entry = entries[index]
                if provider_id == "openrouter" and ":" in entry["id"]:
                    self.show("Not addable", [entry["id"] + ": not addable in this release"])
                    continue
                try:
                    draft = onboarding.line_draft(self.runtime, provider_id, entry, call)
                except (cli_errors.ClaudeMultiError, ValueError) as exc:
                    self.show("Context required — manual entry", [str(exc)])
                    draft = {"wire_model": entry["id"]}
                key = discovery.derived_key(entry["id"], set(self.runtime.lineup_catalog().lines)) or ""
                self.model_form(provider_id, draft, key)

    def stealth_model(self):
        """Add one known id without depending on either listing advertising it."""
        from claude_multi import discovery
        import claude_multi.cli.onboarding as onboarding
        values = tui.OnboardingForm(
            "Add OpenRouter stealth id", (("wire", "stealth/<name>", "", ()),), palette=self.palette,
            help_text=discovery.OPENROUTER_HELP).run(self.win)
        if values is None:
            return
        wire = values["wire"]
        if not discovery.stealth_id(wire):
            self.show("Not addable", ["Use stealth/<name>; variant ids are not addable in this release."])
            return
        try:
            call, entry = onboarding.lookup_stealth(
                self.runtime, wire, confirm=self.confirm,
                notice=lambda text: self.show("OpenRouter lookup", [text]))
            row = onboarding.listing_rows(self.runtime, "openrouter", [entry])[0]
            self.show("Model facts", [discovery.entry_text(row)])
            try:
                draft = onboarding.line_draft(self.runtime, "openrouter", entry, call)
            except discovery.DeclarationRefusal:
                draft = {"wire_model": wire, "display": entry.get("display_name") or wire, "family": "unknown"}
            key = discovery.derived_key(wire, set(self.runtime.lineup_catalog().lines)) or ""
            self.model_form("openrouter", draft, key)
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            self.show("Lookup unavailable", [str(exc)])

    def qualify(self, key):
        fields = views.qualification_form_fields()
        values = tui.OnboardingForm("qualify — choose checks", fields, palette=self.palette,
                                   footer=views.QUALIFY_FORM_FOOTER,
                                   help_text="models qualify KEY --agents --tool-choice forced|auto\n"
                                   "Pool agents also run the offline exact-client check (zero provider calls).\n"
                                   "Unavailable proof is a warning, never a pass. Diagnostics are optional;\n"
                                   "the next default-No consent lists every request.").run(self.win)
        if values is None:
            return
        argv = ["models", "qualify", key, "--tool-choice", values["variant"], "--" + values["checks"]]
        if values["checks"] == "context":
            try:
                argv.append(str(int(values["context"])))
            except ValueError:
                self.show("Context required", ["Enter a positive integer token count."])
                return
        self.invoke(argv)
