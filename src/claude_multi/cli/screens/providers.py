"""The Providers screen and the provider flows every screen shares.

:class:`ConnectActions` holds the credential and provider flows of the
full-screen UI (set or replace an API key, remove it, the Anthropic
transport, account or API key of an account provider, account sign-in and sign-out,
route approval, apply, removing a provider you added, the consented test,
your own endpoint, a reviewed preset and a server on your network). Get started, its provider picker, Direct and the Providers
screen all use them. Every write goes through the setup layer: the plan, the
person's confirmation, the locked commit with its exact undo, the render and
the verified reload; a refusal is shown, never raised.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import quota
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import tui
from claude_multi import views
import claude_multi.cli.consent as consent
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.gateway_actions as screens_gateway
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod

# An admission refused because the gateway does not serve the current setup.
NOT_SERVED = re.compile(r"aliases served — claude-multi providers apply|is not the current render"
                        r"|does not serve the current render")


@dataclass(frozen=True)
class Outcome:
    """A flow's result line and its palette role."""

    text: str
    role: str = "accent"


def _joined(lines: Sequence[str]) -> str:
    return " · ".join(line for line in lines if line)


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    remedy = getattr(exc, "remedy", None)
    if remedy and getattr(exc, "fix", None) is None and remedy not in text:
        text += f" — {remedy}"
    return text


def key_choice(runtime: Any, provider_id: str, current: str | None = None) -> bool:
    """Whether ``provider_id`` is an account provider whose API key this
    build offers as its other transport, or uses now (``current``: its
    transport when the caller knows it): its Enter and K open the
    account-or-key chooser."""

    from claude_multi import operator as operator_mod
    from claude_multi.setup import providers as layer

    if provider_id not in layer.ACCOUNT_PROVIDERS:
        return False
    if operator_mod.transport_alternative(provider_id, operator_mod.TRANSPORT_API_KEY) is None:
        return False
    try:
        if operator_mod.transport_problem(runtime.catalog.docs, provider_id, operator_mod.TRANSPORT_API_KEY) is None:
            return True
        if current is None:
            current = layer.current_transport(runtime, provider_id)
    except (cli_errors.ClaudeMultiError, OSError, ValueError):
        return False
    return current == operator_mod.TRANSPORT_API_KEY


class ConnectActions:
    """The provider and credential flows (see the module docstring)."""

    def __init__(self, runtime: Any, win: Any, palette: tui.Palette, *,
                 background: Callable[[Any], None] | None = None):
        self.runtime = runtime
        self.win = win
        self.palette = palette
        self.background = background

    # -- helpers -------------------------------------------------------------

    def _refused(self, exc: BaseException) -> Outcome:
        return Outcome(_error_text(exc), "warn")

    def _read_only(self) -> Outcome | None:
        from claude_multi.setup import model

        if not getattr(self.runtime, "allow_state_writes", False):
            return Outcome(model.READ_ONLY_SETUP, "warn")
        return None

    def _guard(self, verb: str) -> Outcome | None:
        try:
            consent.require_human(verb, self.runtime.gateway_environ())
        except consent.ConsentRefused as exc:
            return Outcome(str(exc), "warn")
        return None

    def _body(self, lines: Sequence[str]) -> list[str]:
        return [piece for line in lines for piece in screens_common.modal_lines(line, self.win)]

    def _confirm(self, title: str, lines: Sequence[str], buttons: Sequence[tuple[str, Any]]) -> bool:
        """A confirmation with ``buttons`` (the confirming value is True); a
        body taller than the terminal becomes a scrolling y/N view."""

        body = self._body(lines)
        height = self.win.getmaxyx()[0]
        if len(body) > max(1, height - 8):
            return bool(tui.TextView(title, list(lines), palette=self.palette, confirm=True).run(self.win))
        return bool(tui.Modal(title, body, buttons=buttons).run(self.win, self.palette, background=self.background))

    def _choose(self, title: str, items: Sequence[str], *, head: str | Sequence[str] = "",
                help_text: str = "") -> int | None:
        lines = [head] if isinstance(head, str) else list(head)
        entries = [tui.SelectItem(line, header=True) for line in self._body(lines)] if any(lines) else []
        entries += [tui.SelectItem(label) for label in items]
        picker = tui.SelectList(title, entries, footer=cli_text.CHOICE_KEYBAR, selected=len(entries) - len(items))
        index = picker.run(self.win, self.palette, help_text=help_text or title)
        if self.background is not None:
            self.background(self.win)  # what follows draws over the screen, not the list
        if not isinstance(index, int) or index < len(entries) - len(items):
            return None
        return index - (len(entries) - len(items))

    def _ask_key(self, display: str, path: str, replaces: int | None) -> str | None:
        """The masked key modal; None when cancelled. The typed value is
        cleared from the widget at once."""

        body = self._body([cli_text.KEY_MODAL_BODY.format(path=path, display=display)])
        if replaces is not None:
            body += [""] + self._body([cli_text.KEY_MODAL_REPLACE.format(n=replaces)])
        modal = tui.Modal(cli_text.KEY_MODAL_TITLE.format(display=display), body,
                          buttons=cli_text.KEY_MODAL_BUTTONS_REPLACE if replaces is not None
                          else cli_text.KEY_MODAL_BUTTONS_NEW,
                          input=tui.TextInput(mask="•", max_length=4096))
        confirmed = modal.run(self.win, self.palette, background=self.background)
        value = modal.input.value if modal.input is not None else ""
        if modal.input is not None:
            modal.input.value = ""
        if not confirmed:
            return None
        return value

    def _key_file(self) -> str:
        from claude_multi.setup import model, providers as layer

        try:
            return layer.key_file_display(self.runtime)
        except model.SetupError:
            return "the key file"

    # -- API keys --------------------------------------------------------------

    def set_key(self, provider_id: str) -> Outcome:
        """Set or replace a provider's API key (Cancel is the default when one
        is set). A saved key other providers use too is replaced only after a
        confirmation that names every provider using it (Cancel focused)."""

        from claude_multi.setup import model, providers as layer, texts

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_set_key(self.runtime, provider_id)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        refusal = self._guard(f"providers set-key {provider_id}")
        if refusal is not None:
            return refusal
        if plan.replaces_shared:
            users = plan.key_users
            body = cli_text.SHARED_KEY_REPLACE_BODY.format(name=plan.secret_name, providers=users)
            if not self._confirm(cli_text.SHARED_KEY_REPLACE_TITLE.format(providers=users), [body],
                                 cli_text.SHARED_KEY_REPLACE_BUTTONS):
                return Outcome(texts.KEY_KEPT.format(display=plan.display), "warn")
        value = self._ask_key(plan.display, plan.path_display, plan.current_length if plan.replaces else None)
        if value is None:
            return Outcome(texts.KEY_KEPT.format(display=plan.display) if plan.replaces else cli_text.KEY_CANCELLED,
                           "warn")
        if not value:
            return Outcome(texts.KEY_EMPTY, "warn")
        try:
            applied = layer.apply_set_key(self.runtime, plan, model.Confirmation.given(plan), model.Secret(value))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return Outcome(_joined([*applied.lines, self.continue_to_models(provider_id) or ""]))

    def remove_key(self, provider_id: str, *, secret: tuple[str, str] | None = None) -> Outcome:
        from claude_multi.setup import model, providers as layer

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_remove_key(self.runtime, provider_id, secret=secret)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        refusal = self._guard(f"providers remove-key {provider_id}")
        if refusal is not None:
            return refusal
        if not self._confirm(cli_text.REMOVE_KEY_TITLE.format(display=plan.display), plan.lines,
                             cli_text.REMOVE_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_remove_key(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return Outcome(_joined(applied.lines))

    # -- an account provider: its account or its API key ----------------------------

    def _transport_facts(self, provider_id: str) -> tuple[str, bool, tuple[str, ...]]:
        """(current transport, the API key is saved, signed-in accounts)."""

        from claude_multi import operator as operator_mod
        from claude_multi.setup import model, providers as layer, signin

        current = layer.current_transport(self.runtime, provider_id)
        alternative = operator_mod.transport_alternative(provider_id, operator_mod.TRANSPORT_API_KEY)
        present = bool(alternative is not None and layer._key_present(self.runtime, alternative.secret_name))
        try:
            names = signin.accounts(self.runtime, provider_id)
        except model.SetupError:
            names = ()
        return current, present, names

    def _names(self, provider_id: str) -> dict[str, str]:
        """``display``, ``account`` and ``models`` of an account provider."""

        from claude_multi.setup import providers as layer

        return layer.transport_names(self.runtime.catalog.docs, provider_id)

    def _chooser_body(self, provider_id: str, names: Mapping[str, str]) -> str:
        from claude_multi import operator as operator_mod

        alternative = operator_mod.transport_alternative(provider_id, operator_mod.TRANSPORT_API_KEY)
        if alternative is not None and alternative.explicit_models:
            reviewed = operator_mod.key_route_lines(self.runtime.catalog.docs, provider_id)
            return cli_text.TRANSPORT_CHOOSER_BODY_REVIEWED.format(names=", ".join(reviewed) or "none", **names)
        return cli_text.TRANSPORT_CHOOSER_BODY.format(**names)

    def chooser(self, provider_id: str, *, prefer: str | None = None) -> Outcome | None:
        """An account provider's account or its API key: the current way
        opens its own flow, the other one switches the transport after a
        consent (``prefer`` skips the choice)."""

        from claude_multi import operator as operator_mod
        from claude_multi.setup import model, texts

        try:
            current, present, names = self._transport_facts(provider_id)
            words = self._names(provider_id)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        account_in_use = current != operator_mod.TRANSPORT_API_KEY
        choice = prefer
        if choice is None:
            labels = (texts.PICKER_LABELS.get(f"{provider_id}:account", f"{words['account']} — sign in"),
                      texts.PICKER_LABELS.get(f"{provider_id}:api-key", f"{words['display']} API key"))
            width = max(len(label) for label in labels) + cli_text.TRANSPORT_CHOOSER_GAP
            states = (texts.STATE_TEXT["signed-in" if names else "not-signed-in"]
                      + (cli_text.TRANSPORT_IN_USE if account_in_use else ""),
                      texts.STATE_TEXT["key-set" if present else "key-missing"]
                      + ("" if account_in_use else cli_text.TRANSPORT_IN_USE))
            items = [label.ljust(width) + state for label, state in zip(labels, states)]
            index = self._choose(cli_text.TRANSPORT_CHOOSER_TITLE.format(**words), items,
                                 head=self._chooser_body(provider_id, words), help_text=cli_text.PICKER_HELP)
            if index is None:
                return None
            choice = "account" if index == 0 else "api-key"
        if choice == "account":
            if account_in_use:
                return self.account(provider_id) if names else self.sign_in(provider_id)
            return self.transport(provider_id, operator_mod.TRANSPORT_POOL)
        if not account_in_use:
            return self.set_key(provider_id)
        if not getattr(self.runtime, "allow_state_writes", False):
            return Outcome(model.READ_ONLY_SETUP, "warn")
        return self.transport(provider_id, operator_mod.TRANSPORT_API_KEY)

    def transport(self, provider_id: str, to: str) -> Outcome:
        """Switch an account provider's transport (``api-key`` asks for the
        key when none is saved; ``oauth-pool`` then offers removing the
        unused key and signing in)."""

        from claude_multi import operator as operator_mod
        from claude_multi.setup import model, providers as layer, signin

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_transport(self.runtime, provider_id, to)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        refusal = self._guard(f"providers transport {provider_id} {to}")
        if refusal is not None:
            return refusal
        words = self._names(provider_id)
        to_key = to == operator_mod.TRANSPORT_API_KEY
        title = (cli_text.TRANSPORT_TO_KEY_TITLES.get(provider_id, cli_text.TRANSPORT_TO_KEY_TITLE) if to_key
                 else cli_text.TRANSPORT_TO_ACCOUNT_TITLE).format(**words)
        if not self._confirm(title, plan.lines, cli_text.TRANSPORT_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        value: str | None = None
        if to_key and not plan.key_present:
            value = self._ask_key(words["display"], self._key_file(), None)
            if not value:
                return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_transport(self.runtime, plan, model.Confirmation.given(plan),
                                            model.Secret(value) if value else None)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        lines = list(applied.lines)
        if not to_key:
            _current, present, names = self._transport_facts(provider_id)
            if present and tui.Modal(
                    cli_text.KEEP_KEY_TITLE.format(**words),
                    self._body([cli_text.KEEP_KEY_BODY.format(path=self._key_file())]),
                    buttons=cli_text.KEEP_KEY_BUTTONS).run(self.win, self.palette, background=self.background):
                lines.append(self.remove_key(provider_id).text)
            if not names and signin.pool_offered(signin.pool_of(provider_id)) and tui.Modal(
                    cli_text.SIGN_IN_NOW_TITLE.format(**words), [], buttons=cli_text.SIGN_IN_NOW_BUTTONS).run(
                        self.win, self.palette, background=self.background):
                lines.append(self.sign_in(provider_id).text)
        return Outcome(_joined(lines))

    # -- account sign-in ---------------------------------------------------------

    def _collect(self) -> tuple[Callable[[str, str], None], list[Outcome]]:
        seen: list[Outcome] = []
        return (lambda text, role="accent": seen.append(Outcome(text, role))), seen

    def sign_in(self, provider_id: str) -> Outcome:
        import claude_multi.cli.screens.signin as screens_signin
        from claude_multi.setup import model

        say, seen = self._collect()
        screens_signin.run_sign_in_flow(self.runtime, self.win, self.palette, provider_id, say=say,
                                        background=self.background)
        return seen[-1] if seen else Outcome(model.NOTHING_CHANGED, "warn")

    def account(self, provider_id: str) -> Outcome | None:
        import claude_multi.cli.screens.signin as screens_signin

        say, seen = self._collect()
        screens_signin.account_modal(self.runtime, self.win, self.palette, provider_id, say=say,
                                     background=self.background)
        return seen[-1] if seen else None

    def sign_out(self, provider_id: str) -> Outcome | None:
        import claude_multi.cli.screens.signin as screens_signin

        say, seen = self._collect()
        screens_signin.sign_out_flow(self.runtime, self.win, self.palette, provider_id, say=say,
                                     background=self.background)
        return seen[-1] if seen else None

    # -- providers you added ----------------------------------------------------

    def approve(self, provider_id: str) -> Outcome:
        """Approve a route; when its key is not saved yet the key modal follows."""

        from claude_multi.setup import model, providers as layer

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_approve(self.runtime, provider_id)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        refusal = self._guard(f"providers approve {provider_id}")
        if refusal is not None:
            return refusal
        title = (cli_text.APPROVE_TITLE_CHANGED if plan.changed else cli_text.APPROVE_TITLE).format(id=provider_id)
        if not self._confirm(title, plan.lines, cli_text.APPROVE_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_approve(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        lines = list(applied.lines)
        if plan.secret_name and not plan.key_present:
            lines.append(self.set_key(provider_id).text)
        return Outcome(_joined(lines))

    def apply(self, *, not_served: bool = False) -> Outcome:
        """Re-render and verify the reload (after a preview), or say that
        the gateway already serves the setup.

        ``not_served``: the caller saw the running gateway not serving the
        current setup (drift, a connected provider served short, an
        admission refused for that). The plan compares the configuration
        on disk with the declared setup, not with what the gateway serves,
        so then the apply and its verified reload run even when the plan
        has no change."""

        from claude_multi.setup import model, providers as layer, texts

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_apply(self.runtime)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        if not plan.changes and not not_served:
            return Outcome(texts.NOTHING_TO_APPLY)
        lines = plan.lines if plan.changes else (texts.APPLY_NOT_SERVED, *plan.lines)
        if not self._confirm(cli_text.APPLY_TITLE, lines, cli_text.APPLY_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_apply(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return Outcome(_joined(applied.lines))

    def remove(self, provider_id: str) -> Outcome | None:
        """X on a provider you added: its API key, or the provider itself."""

        import claude_multi.cli.onboarding as onboarding
        from claude_multi.setup import model, providers as layer

        try:
            status = onboarding.key_status(self.runtime, provider_id)
        except (cli_errors.ClaudeMultiError, OSError, ValueError):
            status = None
        key_present = bool(status and status[1])
        items = ([cli_text.REMOVE_CHOICE_KEY] if key_present else []) + [
            cli_text.REMOVE_CHOICE_PROVIDER, cli_text.REMOVE_CHOICE_CANCEL]
        index = self._choose(cli_text.REMOVE_CHOICE_TITLE.format(id=provider_id), items,
                             help_text=cli_text.PROVIDERS_HELP)
        if index is None or items[index] == cli_text.REMOVE_CHOICE_CANCEL:
            return None
        if items[index] == cli_text.REMOVE_CHOICE_KEY:
            return self.remove_key(provider_id)
        refusal = self._read_only()
        if refusal is not None:
            return refusal
        try:
            plan = layer.plan_remove_provider(self.runtime, provider_id)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        if plan.blockers:
            lines = [cli_text.REMOVE_BLOCKED_HEAD] + [cli_text.REMOVE_BLOCKED_ITEM.format(fix=b.fix_tui)
                                                      for b in plan.blockers]
            tui.Modal(cli_text.REMOVE_BLOCKED_TITLE.format(id=provider_id), self._body(lines),
                      buttons=cli_text.CLOSE_BUTTON).run(self.win, self.palette, background=self.background)
            return Outcome(cli_text.REMOVE_BLOCKED_TITLE.format(id=provider_id), "warn")
        if not self._confirm(cli_text.REMOVE_PROVIDER_TITLE.format(id=provider_id), plan.lines,
                             cli_text.REMOVE_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_remove_provider(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        lines = list(applied.lines)
        if plan.key_present and plan.secret_name:
            from claude_multi.setup import texts

            if tui.Modal(cli_text.REMOVE_PROVIDER_TITLE.format(id=provider_id),
                         self._body([texts.REMOVE_KEY_TOO.format(name=plan.secret_name)]),
                         buttons=cli_text.REMOVE_KEY_TOO_BUTTONS).run(self.win, self.palette,
                                                                       background=self.background):
                lines.append(self.remove_key(provider_id, secret=(provider_id, plan.secret_name)).text)
        return Outcome(_joined(lines))

    # -- the connection test -----------------------------------------------------

    def test(self, provider_ids: Sequence[str], *, show_results: bool = False) -> Outcome:
        """One consent for every request, then one request per provider."""

        from claude_multi.setup import check, model, texts

        refusal = self._guard("providers test")
        if refusal is not None:
            return refusal
        try:
            plan = check.plan_test(self.runtime, list(provider_ids))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        if not plan.targets:
            return Outcome(_joined(plan.excluded) or texts.TEST_NOTHING, "warn")
        lines = [*plan.excluded, *plan.lines]
        if not tui.TextView(cli_text.TEST_CONSENT_TITLE, lines, palette=self.palette, confirm=True).run(self.win):
            return Outcome(cli_text.TEST_DECLINED, "warn")
        try:
            results = check.run_test(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        texts_out = [result.text for result in results]
        if show_results:
            tui.TextView(cli_text.TEST_RESULTS_TITLE, [*plan.excluded, *texts_out], palette=self.palette).run(self.win)
        role = "accent" if all(result.outcome == "pass" for result in results) else "warn"
        return Outcome(_joined(texts_out), role)

    # -- models of a provider ---------------------------------------------------

    def _provider_lines(self, provider_id: str) -> frozenset[str]:
        try:
            lines = self.runtime.lineup_catalog().lines
        except (cli_errors.ClaudeMultiError, OSError, ValueError):
            return frozenset()
        return frozenset(key for key, entry in lines.items() if entry.get("provider") == provider_id)

    def admit(self, key: str, *, retried: bool = False) -> tuple[int, Outcome | None]:
        """Admit a model you added (its own consent and test request); when
        the gateway does not serve the current setup yet, offer Apply and
        retry. Returns the admission's exit status and the apply's outcome
        (None when none ran)."""

        import claude_multi.cli.onboarding as onboarding

        action = screens_common.OnboardingActions(self.runtime, self.win, self.palette)
        output = io.StringIO()
        try:
            code = onboarding.invoke(self.runtime, ["models", "admit", key],
                                     confirm=lambda text: action.confirm(output.getvalue() + "\n" + text),
                                     output=output)
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            code = 1
            output.write(str(exc))
        if code != 0 and not retried and NOT_SERVED.search(output.getvalue()):
            body = screens_common.modal_lines(cli_text.ADMIT_APPLY_BODY.format(key=key), self.win)
            if tui.Modal(cli_text.ADMIT_APPLY_TITLE, body, buttons=cli_text.ADMIT_APPLY_BUTTONS).run(
                    self.win, self.palette, background=self.background):
                applied = self.apply(not_served=True)
                code, _again = self.admit(key, retried=True)
                return code, applied
        action.show("Action result", output.getvalue().splitlines())
        return code, None

    def continue_to_models(self, provider_id: str) -> str | None:
        """After a provider is connected: when it has no model yet, add its
        models (a listing you consent to, or by hand), then offer admitting
        each one it added, so a profile can use it. None when the provider
        already has models; else the summary line."""

        before = self._provider_lines(provider_id)
        if before:
            return None
        index = self._choose(cli_text.ADD_MODELS_TITLE.format(id=provider_id), list(cli_text.ADD_MODELS_ITEMS),
                             head=cli_text.NO_MODELS_HEAD.format(id=provider_id),
                             help_text=cli_text.PROVIDERS_HELP)
        if index is not None and index < 2:
            screens_common.OnboardingActions(self.runtime, self.win, self.palette).add_models(
                provider_id, mode="listing" if index == 0 else "manual")
        added = sorted(self._provider_lines(provider_id) - before)
        if not added:
            return cli_text.NO_MODELS_LATER.format(id=provider_id)
        admitted = []
        for key in added:
            body = self._body([cli_text.ADMIT_NOW_BODY.format(key=key)])
            if tui.Modal(cli_text.ADMIT_NOW_TITLE.format(key=key), body, buttons=cli_text.ADMIT_NOW_BUTTONS).run(
                    self.win, self.palette, background=self.background):
                code, _applied = self.admit(key)
                if code == 0:
                    admitted.append(key)
        if not admitted:
            return cli_text.NO_MODELS_NOT_ADMITTED.format(id=provider_id, keys=", ".join(added))
        return cli_text.NO_MODELS_ADMITTED.format(id=provider_id, keys=", ".join(admitted))

    # -- adding your own --------------------------------------------------------

    def _add_models(self, provider_id: str) -> str | None:
        return self.continue_to_models(provider_id)

    def endpoint(self, kind: str) -> Outcome | None:
        """Your own endpoint: the form, the preview, the route approval and
        the key; Save writes the declaration, the approval and the key in one
        transaction."""

        from claude_multi.setup import model, providers as layer, texts

        refusal = self._read_only() or self._guard("providers add")
        if refusal is not None:
            return refusal
        values = tui.OnboardingForm(cli_text.ENDPOINT_FORM_TITLE.get(kind, kind), views.endpoint_form_fields(kind),
                                    palette=self.palette, help_text=cli_text.ENDPOINT_FORM_HELP,
                                    footer=cli_text.ENDPOINT_FORM_FOOTER).run(self.win)
        if values is None:
            return None
        try:
            plan = layer.plan_add_endpoint(self.runtime, {**values, "kind": kind})
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return self._declare_keyed(plan, plan.provider_id)

    def _declare_keyed(self, plan: Any, display: str) -> Outcome:
        """A keyed declaration's preview, its route approval and its key
        (typed masked; blank saves none, a saved one is kept), then the one
        transaction and the models step. A shared saved key is used as it
        is; replacing one is confirmed first, naming every provider using
        it (Cancel is the default)."""

        from claude_multi.setup import model, providers as layer, texts

        if not tui.TextView(cli_text.ENDPOINT_PREVIEW_TITLE, plan.lines, palette=self.palette,
                            confirm="declare").run(self.win):
            return Outcome(model.NOTHING_CHANGED, "warn")
        approve = texts.APPROVE_BODY.format(name=plan.secret_name, present=texts.APPROVE_PRESENT[plan.key_present],
                                            origin=plan.origin, auth_text=texts.AUTH_TEXT.get(plan.auth, plan.auth),
                                            listing=plan.listing or "none").split("\n")
        if not self._confirm(cli_text.APPROVE_TITLE.format(id=plan.provider_id), approve, cli_text.APPROVE_BUTTONS):
            return Outcome(model.NOTHING_CHANGED, "warn")
        value: str | None = None
        if plan.key_use != layer.KEY_REUSE:
            if plan.key_use == layer.KEY_REPLACE:
                providers = ", ".join(plan.shared_with)
                body = cli_text.SHARED_KEY_REPLACE_BODY.format(name=plan.secret_name, providers=providers)
                if not self._confirm(cli_text.SHARED_KEY_REPLACE_TITLE.format(providers=providers), [body],
                                     cli_text.SHARED_KEY_REPLACE_BUTTONS):
                    return Outcome(model.NOTHING_CHANGED, "warn")
            value = self._ask_key(display, self._key_file(), plan.key_length if plan.key_present else None)
            if value is None or (plan.key_use == layer.KEY_REPLACE and not value):
                return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                             model.Secret(value) if value else None)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return Outcome(_joined([*applied.lines, self._add_models(plan.provider_id) or ""]))

    def _preset_address(self, name: str) -> str:
        """The base URL preset ``name`` documents ("" when it does not read)."""

        from claude_multi import operator as operator_mod

        info = operator_mod.presets(self.runtime.asset_root).get(name)
        return info.base_url if info is not None else ""

    def preset(self, name: str, display: str) -> Outcome | None:
        """A reviewed preset with an API key: its name (the preset's by
        default) and its address (the preset's by default; another address
        the vendor documents is checked like ``providers add --preset
        --base-url``), then your own endpoint's preview, route approval and
        key, filled in from the preset."""

        from claude_multi.setup import model, providers as layer

        refusal = self._read_only() or self._guard("providers add")
        if refusal is not None:
            return refusal
        address = self._preset_address(name)
        values = tui.OnboardingForm(cli_text.PRESET_FORM_TITLE.format(display=display),
                                    views.preset_form_fields(name, address), palette=self.palette,
                                    help_text=cli_text.PRESET_FORM_HELP,
                                    footer=cli_text.ENDPOINT_FORM_FOOTER).run(self.win)
        if values is None:
            return None
        provider_id = values["id"].strip()
        typed = values.get("url", "").strip()
        # Blank or unchanged keeps the preset's own address (and its bytes).
        base_url = typed if typed and typed != address else None
        try:
            plan = layer.plan_add_preset(self.runtime, name, provider_id, base_url)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        if not isinstance(plan, layer.EndpointPlan):
            return Outcome(model.NOTHING_CHANGED, "warn")  # a keyless server is the network form's
        if plan.preset_sharers:
            plan = self._preset_key(name, provider_id, plan, base_url=base_url)
            if plan is None:
                return Outcome(model.NOTHING_CHANGED, "warn")
            if isinstance(plan, Outcome):
                return plan
        return self._declare_keyed(plan, display)

    def _preset_key(self, name: str, provider_id: str, plan: Any, *, base_url: str | None = None) -> Any:
        """Another provider already uses the preset's API key: a new key of
        its own (the default), the saved key shared (when one is saved), or
        that key replaced. The plan of the choice; None when cancelled; an
        Outcome when the choice is refused."""

        from claude_multi.setup import providers as layer, texts

        providers = ", ".join(plan.preset_sharers)
        words = {"id": provider_id, "own": plan.secret_name, "name": plan.preset_secret, "providers": providers}
        own, reuse, replace = zip(cli_text.PRESET_KEY_ITEMS, cli_text.PRESET_KEY_DETAILS,
                                  (layer.KEY_OWN, layer.KEY_REUSE, layer.KEY_REPLACE))
        choices = [own]
        if layer._key_present(self.runtime, plan.preset_secret):
            choices.append(reuse)
        choices.append(replace)
        # Short items; the details (the generated key name in full) wrap above them.
        head = [texts.PRESET_KEY_SHARED.format(**words), *(detail.format(**words) for _label, detail, _key in choices)]
        index = self._choose(cli_text.PRESET_KEY_TITLE.format(id=provider_id), [label for label, _d, _k in choices],
                             head=head, help_text=cli_text.PRESET_KEY_HELP)
        if index is None:
            return None
        key = choices[index][2]
        if key == layer.KEY_OWN:
            return plan
        try:
            return layer.plan_add_preset(self.runtime, name, provider_id, base_url, key=key)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)

    def lan(self, preset: str) -> Outcome | None:
        """A server on your network: the form, the preview, then the declaration."""

        from claude_multi.setup import model, providers as layer

        refusal = self._read_only()
        if refusal is not None:
            return refusal
        values = tui.OnboardingForm(cli_text.LAN_FORM_TITLE, views.lan_form_fields(), palette=self.palette,
                                    help_text=cli_text.LAN_FORM_HELP, footer=cli_text.LAN_FORM_FOOTER).run(self.win)
        if values is None:
            return None
        try:
            plan = layer.plan_add_preset(self.runtime, preset, values["id"].strip(), values["url"].strip() or None)
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        if not tui.TextView(cli_text.LAN_PREVIEW_TITLE, plan.lines, palette=self.palette,
                            confirm="declare").run(self.win):
            return Outcome(model.NOTHING_CHANGED, "warn")
        try:
            applied = layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            return self._refused(exc)
        return Outcome(_joined([*applied.lines, self._add_models(plan.provider_id) or ""]))

    # -- a picker entry ---------------------------------------------------------

    def own(self, provider_id: str) -> Outcome | None:
        """A provider you added: approve its route, set its key, or its state."""

        from claude_multi.setup import status

        conn = next((c for c in status.connections(self.runtime) if c.provider_id == provider_id), None)
        if conn is not None and conn.state in ("route-unapproved", "route-changed"):
            return self.approve(provider_id)
        if conn is not None and conn.state in ("key-missing", "key-invalid"):
            return self.set_key(provider_id)
        return None

    def connect(self, entry: Any) -> Outcome | None:
        """Connect one picker entry (its own flow per kind)."""

        from claude_multi import operator as operator_mod

        from claude_multi.setup import providers as layer

        if not entry.available:
            return Outcome(cli_text.PICKER_DETAIL["unavailable"].format(note=entry.note or entry.state_text), "warn")
        if entry.kind == "api-key" and entry.provider_id in layer.ACCOUNT_PROVIDERS:
            # An account provider's API key is its other transport.
            return self.chooser(entry.provider_id, prefer="api-key")
        if entry.kind == "account":
            current, _present, names = self._transport_facts(entry.provider_id)
            if current == operator_mod.TRANSPORT_API_KEY:
                return self.transport(entry.provider_id, operator_mod.TRANSPORT_POOL)
            return self.account(entry.provider_id) if names else self.sign_in(entry.provider_id)
        if entry.kind == "api-key":
            return self.set_key(entry.provider_id)
        if entry.kind == "preset":
            return self.preset(entry.id.split(":", 1)[1], entry.group_title)
        if entry.kind == "own":
            return self.own(entry.provider_id)
        if entry.kind == "endpoint":
            return self.endpoint(entry.id.split(":", 1)[1])
        if entry.kind == "lan":
            return self.lan(entry.id.split(":", 2)[2])
        if entry.kind == "manual":
            screens_common.OnboardingActions(self.runtime, self.win, self.palette).new_provider(manual_only=True)
            return None
        return None


class _ProvidersScreen:
    """The Providers screen: local facts per provider and every provider
    action (Enter does the row's main thing; see ``PROVIDERS_HELP``).

    Facts are names and counts only (``_provider_facts`` and the setup
    layer's connections). The journal is read once on open and once per
    ``R`` through the ``journal=`` seam (default ``_read_gateway_journal``,
    which returns None under injected loopback seams, so no test reads the
    host journal); it feeds the failing sign-in cell and the quota detail.
    """

    what = "the providers screen"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        *,
        palette: tui.Palette,
        journal: Callable[[], str | None] | None = None,
        now: datetime | None = None,
        tz=None,
    ):
        self.runtime = runtime
        self.now = now
        self.tz = tz
        self.pool = quota.PoolStatus("seam")
        self.palette = palette
        self.journal = journal if journal is not None else (lambda: gateway_facts._read_gateway_journal(runtime))
        self.selected = 0
        self.offset = 0
        self.message: str | None = None
        self.message_role = "accent"
        self.apply_offer = False
        self.admit_failed = False
        self.journal_facts = gateway_facts._journal_facts(runtime, self.journal())
        self._load_facts(refresh_pool=True)

    # -- data --------------------------------------------------------------

    def _load_facts(self, *, refresh_pool: bool = False) -> None:
        from claude_multi.setup import status

        loaded = gateway_facts._provider_facts(self.runtime)
        self.facts = loaded.facts
        self.config_drift = loaded.config_drift
        self.gateway_down = loaded.gateway_down
        if self.gateway_down:
            self.pool = quota.PoolStatus("down")
        elif refresh_pool:
            self.pool = self.runtime.pool_status()
        try:
            eff = self.runtime.current_effective()
        except cli_errors.CLIError:
            eff = screens_common._default_effective(self.runtime)
        self.eff = eff
        try:
            self.connections = {item.provider_id: item
                                for item in status.connections(self.runtime, facts=loaded.facts)}
        except (cli_errors.ClaudeMultiError, OSError, ValueError):
            self.connections = {}
        self.rows = self._rows()
        self.selected = min(self.selected, max(0, len(self.rows) - 1))
        self.apply_offer = self._apply_needed()

    def _apply_needed(self) -> bool:
        """Drift, a connected provider served below its expected set, or an
        admission that failed for lack of serving."""

        if self.config_drift or self.admit_failed:
            return True
        # A provider with no model line serves only retained aliases: an
        # apply never changes what it serves.
        no_lines = {str(fact["id"]) for fact in self.facts if fact.get("models_note")}
        for pid, conn in self.connections.items():
            if pid in no_lines or conn.state != "connected" or "/" not in conn.served:
                continue
            served, _slash, expected = conn.served.partition("/")
            if served.isdigit() and expected.isdigit() and int(served) < int(expected):
                return True
        return False

    def _rows(self, *, width: int = 77) -> tuple[views.ProviderRow, ...]:
        return views.provider_rows(
            self.facts, eff=self.eff, journal=self.journal_facts, pool=self.pool,
            now=self.now or gateway_facts._doctor_now(), tz=self.tz, login_commands=gateway_facts._OAUTH_LOGIN_COMMANDS,
            restart_hint=gateway_facts.gateway_service_hint("restart", self.runtime), detail_width=width,
        )

    def _refresh(self) -> None:
        self.journal_facts = gateway_facts._journal_facts(self.runtime, self.journal())
        self._load_facts(refresh_pool=True)

    def _banner(self) -> str | None:
        attention = ("Attention: " + "; ".join(dict.fromkeys(self.runtime.gateway_attention))
                     if self.runtime.gateway_attention else "")
        if self.gateway_down:
            return (gateway_facts.gateway_down_banner(self.runtime) + " (W: start it or read its log)"
                    + ("; " + attention if attention else ""))
        if attention:
            return attention
        if self.apply_offer:
            return cli_text.PROVIDERS_APPLY_BANNER
        return None

    # -- the selected row ----------------------------------------------------

    def _fact(self) -> Mapping[str, Any] | None:
        return self.facts[self.selected] if 0 <= self.selected < len(self.facts) else None

    def _own(self, fact: Mapping[str, Any]) -> bool:
        return fact.get("source") == "operator"

    def _account_capable(self, fact: Mapping[str, Any]) -> bool:
        """The provider keeps account sign-ins (L lists them whichever
        transport is active)."""

        from claude_multi.setup import providers as layer

        return str(fact["id"]) in layer.ACCOUNT_PROVIDERS

    def _key_choice(self, fact: Mapping[str, Any]) -> bool:
        """An account provider whose API key this build offers (or uses):
        Enter and K open its account-or-key chooser."""

        pid = str(fact["id"])
        conn = self.connections.get(pid)
        return self._account_capable(fact) and key_choice(
            self.runtime, pid, (conn.transport or None) if conn is not None else None)

    def _account(self, fact: Mapping[str, Any]) -> bool:
        """The provider's active connection is an account sign-in (its row
        offers account actions; on its API key it offers key actions)."""

        return self._account_capable(fact) and fact.get("kind") == "oauth-pool"

    def _keyless(self, fact: Mapping[str, Any]) -> bool:
        return fact.get("kind") != "oauth-pool" and fact.get("auth", "none") in ("none", None)

    def _primary(self, fact: Mapping[str, Any]) -> str:
        """The row's Enter: approve, set-key, connect, sign-in, account or details."""

        pid = str(fact["id"])
        conn = self.connections.get(pid)
        if self._own(fact) and fact.get("route") in ("unapproved", "changed"):
            return "approve"
        if self._key_choice(fact):
            return "connect"
        if self._account(fact):
            return "account" if conn is not None and conn.accounts else "sign-in"
        if not self._keyless(fact) and str(fact.get("credential", "")).endswith(" missing"):
            return "set-key"
        return "details"

    def _keybar_bindings(self) -> list[tuple[str, str]]:
        fact = self._fact()
        if fact is None:
            bindings = list(cli_text.PROVIDERS_KEYBAR)
        else:
            primary = cli_text.PROVIDERS_PRIMARY[self._primary(fact)]
            if self._own(fact):
                if self._keyless(fact):
                    bindings = list(cli_text.PROVIDERS_KEYBAR_OWN_KEYLESS)
                else:
                    set_ = str(fact.get("credential", "")).endswith(" present")
                    label = cli_text.PROVIDERS_KEY_LABELS["replace" if set_ else "set"]
                    bindings = [(key, text.format(primary=primary, key_label=label))
                                for key, text in cli_text.PROVIDERS_KEYBAR_OWN]
            elif self._account(fact):
                bindings = [(key, text.format(primary=primary)) for key, text in cli_text.PROVIDERS_KEYBAR_ACCOUNT]
            elif self._account_capable(fact):
                set_ = str(fact.get("credential", "")).endswith(" present")
                label = cli_text.PROVIDERS_KEY_LABELS["replace" if set_ else "set"]
                bindings = [(key, text.format(primary=primary, key_label=label))
                            for key, text in cli_text.PROVIDERS_KEYBAR_ACCOUNT_KEY]
            elif self._keyless(fact):
                bindings = list(cli_text.PROVIDERS_KEYBAR_KEYLESS)
            elif str(fact.get("credential", "")).endswith(" missing"):
                bindings = list(cli_text.PROVIDERS_KEYBAR_KEY_MISSING)
            else:
                bindings = list(cli_text.PROVIDERS_KEYBAR_KEY_SET)
        if self.apply_offer:
            bindings.insert(1 if bindings and bindings[0][0] == "Enter" else 0, cli_text.PROVIDERS_APPLY_BINDING)
        if self.gateway_down:
            bindings.insert(len(bindings) - 2, cli_text.PROVIDERS_GATEWAY_BINDING)
        return bindings

    # -- layout ------------------------------------------------------------

    def _head_lines(self, width: int) -> tuple[list[str], list[str]]:
        legend = screens_common._wrapped(cli_text.PROVIDERS_LEGEND, width - 4)
        banner = self._banner()
        return legend, (screens_common._wrapped(banner, width - 4) if banner else [])

    def _bar_rows(self, width: int) -> int:
        bars = [cli_text.PROVIDERS_KEYBAR_KEY_MISSING, cli_text.PROVIDERS_KEYBAR_KEY_SET,
                cli_text.PROVIDERS_KEYBAR_ACCOUNT, cli_text.PROVIDERS_KEYBAR_ACCOUNT_KEY, cli_text.PROVIDERS_KEYBAR_OWN,
                cli_text.PROVIDERS_KEYBAR_OWN_KEYLESS, cli_text.PROVIDERS_KEYBAR_KEYLESS]
        return max(tui.KeyBar((bar[0], cli_text.PROVIDERS_APPLY_BINDING, *bar[1:-2],
                               cli_text.PROVIDERS_GATEWAY_BINDING, *bar[-2:])).rows(width) for bar in bars)

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor: chrome + min(list, 3) + detail reserve + bar."""

        legend, banner = self._head_lines(width)
        chrome = 3 + len(legend) + len(banner) + 1 + 1  # top/title/rule, header, message
        floor = views.ScreenFloor.compute(
            chrome=chrome,
            list_rows=len(self.rows),
            detail_reserve=cli_text.PROVIDERS_DETAIL_RESERVE,
            bar_rows=self._bar_rows(width),
            cols=cli_text.PROVIDERS_MIN_COLS,
        )
        return floor.as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = tui.KeyBar(self._keybar_bindings())
        bar_rows = keybar.rows(width)
        tui.safe_add(win, 1, 2, cli_text.PROVIDERS_TITLE, palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        row = 3
        legend, banner = self._head_lines(width)
        for line in legend:
            tui.safe_add(win, row, 2, line, palette.attr("dim"))
            row += 1
        for line in banner:
            tui.safe_add(win, row, 2, line, palette.attr("warn"))
            row += 1
        table = tui.Table(
            views.PROVIDERS_COLUMNS,
            [list(r.cells) for r in self.rows],
            selected=self.selected,
            min_widths=cli_text.PROVIDERS_MIN_WIDTHS,
            gap=cli_text.PROVIDERS_GAP,
            row_roles=["normal" if r.enabled else "dim" for r in self.rows],
            cursor=True,
        )
        message_row = height - bar_rows - 1
        detail_top = message_row - cli_text.PROVIDERS_DETAIL_RESERVE
        visible = max(1, detail_top - (row + 1))
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + visible:
            self.offset = self.selected - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.rows) - visible)))
        # Content ends at ``width - 1`` (the rule's right edge).
        table.draw(win, row, 2, width - 1, palette, max_rows=visible, start=self.offset)
        # A provider with no model line prints its span across ``served``:
        # drawn from the served column to the content edge.
        widths = table._widths(width - 1 - 4)
        served_x = 4 + sum(widths[:5]) + table.gap * 5
        quota_x = 4 + sum(widths[:3]) + table.gap * 3
        for index in range(self.offset, min(len(self.rows), self.offset + visible)):
            item = self.rows[index]
            focused = index == self.selected
            attr = palette.attr("normal" if item.enabled else "dim")
            if item.kind == "oauth-pool" and self.pool.state == "ok":
                pool_name = self.facts[index].get("pool")
                cell = quota.provider_cell(
                    (c for c in self.pool.credentials if c.provider == pool_name),
                    self.now or gateway_facts._doctor_now(), width=widths[3],
                )
                tui.safe_add(win, row + 1 + index - self.offset, quota_x,
                             cell.ljust(widths[3]), attr | (tui.curses.A_REVERSE if focused else 0))
            span = item.span
            if span is None:
                continue
            tui.safe_add(
                win, row + 1 + index - self.offset, served_x,
                views.clip(span, width - 1 - served_x),
                attr | (tui.curses.A_REVERSE if focused else 0),
            )
        if self.rows:
            detail_row = row + 1 + min(visible, len(self.rows) - self.offset)
            details = self._rows(width=width - 3)[self.selected].details
            for offset, line in enumerate(details[:cli_text.PROVIDERS_DETAIL_RESERVE]):
                tui.safe_add(win, detail_row + offset, 2, views.clip(line, width - 3), palette.attr("dim"))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3),
                         palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- actions -----------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _show(self, outcome: Outcome | None) -> None:
        self._load_facts()
        if outcome is not None and outcome.text:
            self._say(outcome.text, outcome.role)

    def _actions(self, win: Any) -> ConnectActions:
        return ConnectActions(self.runtime, win, self.palette, background=self._draw)

    def _toggle(self) -> None:
        row = self.rows[self.selected]
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return
        import claude_multi.cli.onboarding as onboarding

        try:
            # The shared authority preview and one served-change phase; the
            # toggle itself changes no served selector.
            plan = onboarding.toggle_provider(
                self.runtime, row.id, not row.enabled,
                catalog=self.runtime.lineup_catalog(),
                custom_registry=custom.load_registry(self.runtime.environ),
            )
        except (
            settings_mod.SettingsError,
            sessions.StateMarkerError,
            sessions.MigrationBusyError,
            state.StateError,
            custom.CustomModelsError,
            cli_errors.ClaudeMultiError,
            OSError,
        ) as exc:
            self._say(str(exc), "warn")
            return
        self._load_facts()
        word = "disabled" if row.enabled else "enabled"
        message = f"{row.id} {word} — {cli_text._APPLIES_NEXT}; running sessions keep their routes"
        if plan.served_changed and plan.before_unknown is None:
            # Never expected from a toggle; say so rather than hide it.
            message += " (the declared render also differs from the published one: claude-multi plan)"
        self._say(message)

    def _primary_action(self, win: Any) -> None:
        fact = self._fact()
        if fact is None:
            return
        pid = str(fact["id"])
        primary = self._primary(fact)
        actions = self._actions(win)
        if primary == "approve":
            self._show(actions.approve(pid))
        elif primary == "set-key":
            self._show(actions.set_key(pid))
        elif primary == "connect":
            self._show(actions.chooser(pid))
        elif primary == "sign-in":
            self._show(actions.sign_in(pid))
        elif primary == "account":
            self._show(actions.account(pid))
        else:
            self._details(win)

    def _key(self, win: Any) -> None:
        fact = self._fact()
        if fact is None:
            return
        pid = str(fact["id"])
        actions = self._actions(win)
        if self._key_choice(fact):
            self._show(actions.chooser(pid, prefer="api-key"))
        else:
            self._show(actions.set_key(pid))

    def _remove(self, win: Any) -> None:
        fact = self._fact()
        if fact is None:
            return
        pid = str(fact["id"])
        actions = self._actions(win)
        if self._own(fact):
            self._show(actions.remove(pid))
        elif self._account(fact) and fact.get("kind") == "oauth-pool":
            self._show(actions.sign_out(pid))
        else:
            self._show(actions.remove_key(pid))

    def _sign_in_modal(self, win: Any) -> None:
        fact = self._fact()
        if fact is None:
            return
        pid = str(fact["id"])
        if not self._account_capable(fact):
            self._say(cli_text.L_NOT_ACCOUNT.format(display=fact.get("display", pid)), "warn")
            return
        self._show(self._actions(win).account(pid))

    def _apply(self, win: Any) -> None:
        if not self.apply_offer:
            from claude_multi.setup import texts

            self._say(texts.NOTHING_TO_APPLY)
            return
        # Offered because the running gateway does not serve the setup.
        outcome = self._actions(win).apply(not_served=True)
        self.admit_failed = False
        self._show(outcome)

    def _edit(self, win: Any) -> None:
        """E on a provider you added: its declaration in a prefilled form,
        saved through the providers edit transaction."""

        import claude_multi.cli.onboarding as onboarding

        fact = self._fact()
        if fact is None:
            return
        if not self._own(fact):
            self._say(cli_text.E_CATALOG.format(display=fact.get("display", fact["id"])), "warn")
            return
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return
        pid = str(fact["id"])
        try:
            # Read, validated and secret-screened before any default is
            # drawn; the bytes stay the baseline of the save.
            previous, block = onboarding.provider_edit_snapshot(self.runtime, pid)
            fields = views.provider_edit_form_fields(block)
        except (cli_errors.ClaudeMultiError, OSError, ValueError, KeyError, TypeError) as exc:
            self._say(cli_text.EDIT_UNREADABLE.format(id=pid, problem=_error_text(exc)), "warn")
            return
        values = tui.OnboardingForm(cli_text.EDIT_FORM_TITLE.format(id=pid), fields, palette=self.palette,
                                    help_text=cli_text.EDIT_FORM_HELP, footer=cli_text.EDIT_FORM_FOOTER).run(win)
        if values is None:
            return
        action = screens_common.OnboardingActions(self.runtime, win, self.palette)
        output = io.StringIO()
        try:
            code = onboarding.edit_provider(self.runtime, pid, values, previous=previous, output=output,
                                            confirm=lambda text: action.confirm(output.getvalue() + "\n" + text))
        except (cli_errors.ClaudeMultiError, ValueError, OSError) as exc:
            code = 1
            output.write(_error_text(exc))
        action.show("Action result", output.getvalue().splitlines())
        self._load_facts()
        if code == 0:
            self._say(cli_text.EDIT_DONE.format(id=pid))
        else:
            self._say(cli_text.EDIT_NOT_SAVED.format(id=pid), "warn")

    def _new(self, win: Any) -> None:
        import claude_multi.cli.screens.get_started as screens_get_started

        outcome = screens_get_started.run_picker(self.runtime, win, self.palette, only="other")
        self._show(outcome)

    def _details(self, win: Any) -> None:
        from claude_multi.setup import signin

        fact = self._fact()
        if fact is None:
            return
        pid = str(fact["id"])
        row = self.rows[self.selected]
        lines = list(self._rows(width=4096)[self.selected].details)
        conn = self.connections.get(pid)
        if pid in signin.ACCOUNT_POOLS:
            names = conn.accounts if conn is not None else ()
            if names and not self._account(fact):
                lines.insert(0, cli_text.DETAILS_SAVED_ACCOUNTS.format(accounts=", ".join(names)))
            elif names:
                lines.insert(0, cli_text.DETAILS_SIGNED_IN.format(accounts=", ".join(names)))
                if len(names) > 1:
                    lines.insert(1, cli_text.DETAILS_MULTI_ACCOUNT.format(n=len(names)))
            if self._key_choice(fact) and conn is not None and conn.transport:
                from claude_multi.setup import providers as layer

                words = layer.transport_names(self.runtime.catalog.docs, pid)
                transport = (cli_text.DETAILS_TRANSPORT_KEY.format(**words) if conn.transport == "api-key"
                             else words["account"])
                lines.insert(0, cli_text.DETAILS_TRANSPORT.format(transport=transport))
        if row.span is not None:
            lines.append(views.PROVIDERS_NO_MODELS)
        pool_name = fact.get("pool")
        if pool_name is not None:
            lines += [""] + quota.command_lines(
                self.pool, provider_by_pool={pool_name: row.id},
                login_commands=gateway_facts._OAUTH_LOGIN_COMMANDS,
                restart_hint=gateway_facts.gateway_service_hint("restart", self.runtime),
                now=self.now or gateway_facts._doctor_now(), tz=self.tz)[1]
        tui.TextView(cli_text.DETAILS_TITLE, lines, palette=self.palette).run(win)

    def run(self, win: Any) -> None:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = None
            letter = key.ch.upper() if key.kind == "char" and key.ch else ""
            if key.kind == "up":
                self.selected = max(0, self.selected - 1)
            elif key.kind == "down":
                self.selected = min(max(0, len(self.rows) - 1), self.selected + 1)
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self.selected = max(0, len(self.rows) - 1)
            elif letter == "?":
                screens_common._help_modal(win, self.palette, "providers — help", cli_text.PROVIDERS_HELP, self._draw)
            elif letter == "R":
                self._refresh()
                self._say("refreshed.")
            elif letter == "N":
                self._new(win)
            elif letter == "W":
                message = screens_gateway.dialog(self.runtime, win, self.palette, background=self._draw)
                self._refresh()
                if message:
                    self._say(message)
            elif letter == "P":
                self._apply(win)
            elif not self.rows:
                continue
            elif letter == " ":
                self._toggle()
            elif letter == "A":
                screens_common.OnboardingActions(self.runtime, win, self.palette).add_models(self.rows[self.selected].id)
                self._load_facts()
            elif letter == "E":
                self._edit(win)
            elif letter == "Q":
                self._details(win)
            elif letter == "K":
                self._key(win)
            elif letter == "X":
                self._remove(win)
            elif letter == "L":
                self._sign_in_modal(win)
            elif letter == "T":
                self._show(self._actions(win).test([self.rows[self.selected].id]))
            elif key.kind == "enter":
                self._primary_action(win)


def run_providers_screen(
    runtime: runtime_mod.Runtime,
    win: Any,
    palette: tui.Palette,
    *,
    journal: Callable[[], str | None] | None = None,
) -> None:
    """Open the Providers screen on ``win``; returns when it is closed."""

    _ProvidersScreen(runtime, palette=palette, journal=journal).run(win)
