"""Account sign-in, the account modal and sign-out in the full-screen UI.

The sign-in runs the gateway program's own sign-in in this terminal with
curses suspended: the typed personal-use acknowledgement first (once per
text), then, when the sign-in can open more than one way (a browser
page here or an address for another device), how it opens. The record diff
decides the outcome (the layer's ``run_sign_in``); nothing changes unless
the sign-in completes. Sign-out moves the account's records into a kept
backup and reloads the gateway (the layer's ``apply_sign_out``).
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import Any, Callable

from claude_multi import errors as cli_errors
from claude_multi import tui
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.text as cli_text


# A Modal shows at most its height less these rows of body text (the
# frame, the title, the field and the buttons: ``tui.Modal``'s geometry).
ACK_MODAL_CHROME = 5


def _kind(pool: str) -> str:
    from claude_multi.setup import texts

    return texts.ACCOUNT_KINDS.get(pool, f"{pool} account")


def _result(win: Any, palette: tui.Palette, title: str, lines: list[str] | tuple[str, ...],
            background: Callable[[Any], None] | None) -> None:
    body = [piece for line in lines for piece in screens_common.modal_lines(line, win)]
    tui.Modal(title, body, buttons=cli_text.CLOSE_BUTTON).run(win, palette, background=background)


def _wait_for_enter(stream: Any = None) -> None:
    out = sys.stdout
    out.write("\n" + cli_text.SIGNIN_RETURN + "\n")
    out.flush()
    try:
        (stream or sys.stdin).readline()
    except (OSError, ValueError, KeyboardInterrupt):
        pass  # Ctrl-C here only returns to the screen


def run_sign_in_flow(runtime: Any, win: Any, palette: tui.Palette, provider_id: str, *,
                     say: Callable[[str, str], None] | None = None,
                     background: Callable[[Any], None] | None = None,
                     method: str | None = None) -> Any:
    """Sign in to the account of ``provider_id`` in this terminal; the
    layer's ``SignInOutcome``, or None when nothing ran (a refusal, a
    wrong word or Cancel). ``say(text, role)`` receives the result line."""

    from claude_multi.setup import model, signin, texts

    def tell(text: str, role: str = "accent") -> None:
        if say is not None:
            say(text, role)

    try:
        pool = signin.pool_of(provider_id)
    except model.SetupError as exc:
        tell(str(exc), "warn")
        return None
    kind = _kind(pool)
    if not getattr(runtime, "allow_state_writes", False):
        tell(model.READ_ONLY_SETUP, "warn")
        return None
    if not signin.pool_offered(pool):
        tell(texts.SIGNIN_STRICT, "warn")
        return None
    try:
        consent.require_human(f"providers sign-in {provider_id}", runtime.gateway_environ())
    except consent.ConsentRefused as exc:
        tell(str(exc), "warn")
        return None
    ack = signin.current_ack(runtime.environ, pool)
    if ack is None:
        _text_id, text = signin.ack_text(pool)
        title = cli_text.ACK_TITLE.format(kind=kind)
        body = screens_common.modal_lines(text, win)
        if len(body) + ACK_MODAL_CHROME > win.getmaxyx()[0]:
            # The whole text and the field do not fit: the text is read in a
            # scrolling view first, then the word is typed under its last line.
            read = screens_common._ScrollModal(title, body, button=cli_text.ACK_READ_BUTTON)
            if not read.run(win, palette, background=background):
                return None
            body = screens_common.modal_lines(text.rsplit("\n", 1)[-1], win)
        field = tui.TextInput(max_length=32)
        modal = tui.Modal(title, body, buttons=cli_text.ACK_BUTTONS, input=field)
        if not modal.run(win, palette, background=background):
            return None
        try:
            ack = signin.record_ack(runtime, pool, field.value)
        except model.SetupError as exc:
            tell(str(exc), "warn")
            return None
    chosen = method
    values = list(signin.methods(pool))
    if chosen is None and len(values) > 1:
        # A sign-in that can open more than one way: the person chooses,
        # the environment's default preselected.
        default = signin.method_default(pool, runtime.environ, wsl=signin._is_wsl(runtime))
        labels = dict(cli_text.SIGNIN_METHODS)
        picker = tui.SelectList(cli_text.SIGNIN_METHOD_TITLE.format(kind=kind),
                                [tui.SelectItem(labels.get(value, value)) for value in values],
                                footer=cli_text.CHOICE_KEYBAR,
                                selected=values.index(default) if default in values else 0)
        index = picker.run(win, palette, help_text=cli_text.PICKER_DETAIL["account"])
        if not isinstance(index, int):
            return None
        chosen = values[index]
    try:
        # Preparing the gateway program reports unavailable providers on
        # stderr; the Providers screen shows those, and curses owns the
        # terminal until the sign-in starts.
        with contextlib.redirect_stderr(io.StringIO()):
            plan = signin.plan_sign_in(runtime, provider_id, method=chosen)
    except (model.SetupError, cli_errors.ClaudeMultiError) as exc:
        remedy = getattr(exc, "remedy", None)
        tell(str(exc) + (f" — {remedy}" if remedy and remedy not in str(exc) else ""), "warn")
        return None
    runner = getattr(runtime, "signin_runner", None)
    outcome = None
    error = ""
    with tui.suspended_curses(win):
        sys.stdout.write(signin.header(plan))
        sys.stdout.flush()
        try:
            outcome = signin.run_sign_in(runtime, plan, ack=ack, runner=runner)
        except (model.SetupError, consent.ConsentRefused) as exc:
            error = str(exc)
            sys.stdout.write(f"{error}\n")
        _wait_for_enter()
    if outcome is None:
        tell(error or texts.SIGNIN_CANCELLED, "warn")
        return None
    lines = signin.outcome_lines(plan, outcome)
    _result(win, palette, cli_text.SIGNIN_RESULT_TITLE.format(kind=kind), lines, background)
    tell(lines[0], "accent" if outcome.status == "signed-in" else "warn")
    return outcome


def sign_out_flow(runtime: Any, win: Any, palette: tui.Palette, provider_id: str, *,
                  say: Callable[[str, str], None] | None = None,
                  background: Callable[[Any], None] | None = None) -> Any:
    """The sign-out confirmation, then the move into the kept backup and the
    reload; the layer's ``SignOutOutcome`` or None."""

    from claude_multi import termtext
    from claude_multi.setup import model, signin, texts

    def tell(text: str, role: str = "accent") -> None:
        if say is not None:
            say(text, role)

    if not getattr(runtime, "allow_state_writes", False):
        tell(model.READ_ONLY_SETUP, "warn")
        return None
    try:
        consent.require_human(f"providers sign-out {provider_id}", runtime.gateway_environ())
        plan = signin.plan_sign_out(runtime, provider_id)
    except consent.ConsentRefused as exc:
        tell(str(exc), "warn")
        return None
    except model.SetupError as exc:
        tell(str(exc), "warn")
        return None
    kind = _kind(plan.pool)
    body = [piece for line in plan.lines for piece in screens_common.modal_lines(line, win)]
    if not tui.Modal(cli_text.SIGNOUT_TITLE.format(kind=kind), body,
                     buttons=cli_text.SIGNOUT_BUTTONS).run(win, palette, background=background):
        tell(model.NOTHING_CHANGED)
        return None
    try:
        outcome = signin.apply_sign_out(runtime, plan, model.Confirmation.given(plan))
    except (model.SetupError, cli_errors.ClaudeMultiError, OSError) as exc:
        tell(str(exc), "warn")
        return None
    undo = texts.SIGNED_OUT_UNDO.format(backup=outcome.backup,
                                        auth_dir=termtext.visible_text(str(signin.auth_dir(runtime))))
    _result(win, palette, cli_text.SIGNIN_RESULT_TITLE.format(kind=kind), [*outcome.lines, undo], background)
    tell(outcome.lines[0], "accent" if outcome.complete else "warn")
    return outcome


def account_modal(runtime: Any, win: Any, palette: tui.Palette, provider_id: str, *,
                  say: Callable[[str, str], None] | None = None,
                  background: Callable[[Any], None] | None = None) -> Any:
    """The account modal (L): who is signed in, sign in (again) or sign out."""

    from claude_multi.setup import model, signin

    try:
        pool = signin.pool_of(provider_id)
        names = signin.accounts(runtime, provider_id)
    except model.SetupError as exc:
        if say is not None:
            say(str(exc), "warn")
        return None
    kind = _kind(pool)
    if names:
        lines = [cli_text.L_BODY_SIGNED_IN.format(accounts=", ".join(names))]
        if signin.client_account(pool):
            lines.append(cli_text.L_BODY_SEPARATE)
        buttons = cli_text.L_BUTTONS_SIGNED_IN
    else:
        lines = [cli_text.L_BODY_NOT_SIGNED_IN]
        buttons = cli_text.L_BUTTONS_NOT
    body = [piece for line in lines for piece in screens_common.modal_lines(line, win)]
    choice = tui.Modal(cli_text.L_TITLE.format(kind=kind), body, buttons=buttons).run(
        win, palette, background=background)
    if choice == "signin":
        return run_sign_in_flow(runtime, win, palette, provider_id, say=say, background=background)
    if choice == "signout":
        return sign_out_flow(runtime, win, palette, provider_id, say=say, background=background)
    return None
