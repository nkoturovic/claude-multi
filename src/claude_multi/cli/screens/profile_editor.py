"""The profile editor runner and its callbacks."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import errors as cli_errors
from claude_multi import lineup as lineup_mod
from claude_multi import profile as profile_mod
from claude_multi import sessions
from claude_multi import state
from claude_multi import termtext
from claude_multi import tui
from typing import Any
from typing import TextIO
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.propagation as screens_propagation
import claude_multi.cli.selection as selection
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _profile_editor_callbacks(
    runtime: runtime_mod.Runtime,
    palette: tui.Palette,
    *,
    editor_state: tui.ProfileEditorState | None = None,
) -> tui.ProfileEditorCallbacks:
    """The store access of the profile editor stack (save, named bindings, propagation).

    Every write goes through ``ProfileStore``/``BindingStore`` (their own
    locks); a successful save or binding change runs the propagation prompt
    on the editor's window.  Errors come back as text, shown verbatim with
    the edits kept.

    With ``editor_state`` a committed save is marked on it before the
    propagation prompt runs (a SIGINT there still reports the save), and
    its ``loaded_digest`` becomes the digest of the bytes this save wrote,
    so the next save still refuses when another window wrote meanwhile.

    A Rename is the profile rename of the Profiles screen (the default
    moves with it, following sessions keep their lineup), writing the
    edited document under the new name in the same step; who follows the
    profile and whether it is the default are shown and confirmed first.

    A save or rename carries the digest the editor loaded
    (``outcome.expected``): when the stored profile changed since,
    ``tui.ProfileChanged`` is raised and nothing is written.
    """

    from claude_multi.setup import model as setup_model, profiles as setup_profiles

    def rename(win: Any, outcome: tui.ProfileEditorOutcome) -> Any:
        """The setup layer's rename with the edited document (None: declined)."""

        assert outcome.old_name is not None
        plan = setup_profiles.plan_rename(runtime, outcome.old_name, outcome.name,
                                          document=outcome.document, expected=outcome.expected)
        effects = plan.lines[1:]  # the followers and the default
        if effects:
            body = [piece for line in effects for piece in screens_common.modal_lines(line, win)]
            title = cli_text.EDITOR_RENAME_TITLE.format(old=outcome.old_name, new=outcome.name)
            if not tui.Modal(title, body, buttons=cli_text.EDITOR_RENAME_BUTTONS).run(win, palette):
                return None
        return setup_profiles.apply_rename(runtime, plan, setup_model.Confirmation.given(plan))

    def save(win: Any, outcome: tui.ProfileEditorOutcome) -> tuple[bool, str]:
        store = runtime.profiles
        error: str | None = None
        preface: list[str] = []
        digest: str | None = None
        try:
            if outcome.action == "new":
                digest = store.commit({key: value for key, value in outcome.document.items() if key != "seed"},
                                      new=True)
            elif outcome.action == "rename":
                renamed = rename(win, outcome)
                if renamed is None:
                    return False, setup_model.NOTHING_CHANGED
                digest, preface = renamed.digest, list(renamed.lines[1:])
            else:
                digest = store.commit(outcome.document, target=outcome.name, expected=outcome.expected)
        except profile_mod.ProfileChangedError as exc:
            raise tui.ProfileChanged(outcome.old_name or outcome.name) from exc
        except setup_model.Stale as exc:
            if isinstance(exc.__cause__, profile_mod.ProfileChangedError):
                raise tui.ProfileChanged(outcome.old_name or outcome.name) from exc
            error = str(exc)
        except state.CommittedStateError as exc:
            error = f"Committed, durability unconfirmed: {screens_common._state_error_text(exc)}"
        except (profile_mod.ProfileError, sessions.StateMarkerError, state.StateError, OSError) as exc:
            error = screens_common._state_error_text(exc)
        except setup_model.SetupError as exc:
            error = termtext.visible_message(exc)
        if error is not None:
            return False, error
        if editor_state is not None:
            editor_state.mark_saved(outcome)
            editor_state.loaded_digest = digest
        shown = screens_propagation.run_propagation_screen(runtime, win, palette, [outcome.name], preface=preface)
        if shown == "none" and not preface:
            return True, cli_text.PROPAGATION_NO_FOLLOWERS.format(name=outcome.name)
        return True, tui.PROFILE_FORM_SAVED.format(name=outcome.name)

    def load_bindings() -> tuple[dict[str, dict[str, Any]], list[str]]:
        lcat = runtime.lineup_catalog()
        try:
            return runtime.bindings.bindings(), runtime.bindings.conflicts(lcat)
        except profile_mod.BindingError as exc:
            return {}, [str(exc)]

    def binding_users(name: str) -> int:
        return len(lineup_mod.profiles_for_binding(runtime, name))

    def set_binding(win: Any, name: str, model: str, effort: str) -> str | None:
        try:
            runtime.bindings.set(
                name,
                model,
                effort,
                cat=runtime.lineup_catalog(),
                profiles=runtime.profiles,
                effective=runtime.current_effective(),
            )
        except state.CommittedStateError as exc:
            return f"Committed, durability unconfirmed: {screens_common._state_error_text(exc)}"
        except (profile_mod.BindingError, sessions.StateMarkerError, state.StateError, cli_errors.CLIError, OSError) as exc:
            return screens_common._state_error_text(exc)
        users = lineup_mod.profiles_for_binding(runtime, name)
        if users:
            screens_propagation.run_propagation_screen(runtime, win, palette, users, binding=name)
        return None

    def delete_binding(name: str) -> str | None:
        try:
            runtime.bindings.delete(name, profiles=runtime.profiles)
        except state.CommittedStateError as exc:
            return f"Committed, durability unconfirmed: {screens_common._state_error_text(exc)}"
        except (profile_mod.BindingError, sessions.StateMarkerError, state.StateError, OSError) as exc:
            return screens_common._state_error_text(exc)
        return None

    def reload(name: str) -> tuple[dict[str, Any], str | None]:
        store = runtime.profiles
        digest = store.digest(name)
        return store.load(name), digest

    return tui.ProfileEditorCallbacks(
        save=save,
        load_bindings=load_bindings,
        binding_users=binding_users,
        set_binding=set_binding,
        delete_binding=delete_binding,
        reload=reload,
    )


def run_named_bindings(runtime: runtime_mod.Runtime, win: Any, palette: tui.Palette) -> None:
    """The named bindings screen outside a profile (Profiles' B), writing
    through the same callbacks as the editor's."""

    try:
        effective = runtime.current_effective()
    except cli_errors.CLIError:
        effective = screens_common._default_effective(runtime)
    tui.NamedBindingsScreen(runtime.lineup_catalog(), effective,
                            callbacks=_profile_editor_callbacks(runtime, palette), palette=palette).run(win)


def _fallback_owners(runtime: runtime_mod.Runtime, *, exclude: str | None) -> dict[str, tuple[str, ...]]:
    """Every loadable profile that is the fallback for a provider, by
    provider (``exclude``: the profile being edited)."""

    store = runtime.profiles
    owners: dict[str, list[str]] = {}
    for other in store.names():
        if other == exclude:
            continue
        try:
            primary = store.load(other).get("primary_provider")
        except profile_mod.ProfileError:
            continue
        if isinstance(primary, str) and primary:
            owners.setdefault(primary, []).append(other)
    return {provider: tuple(names) for provider, names in owners.items()}


def _draft_editor_state(runtime: runtime_mod.Runtime, document: dict[str, Any]) -> tui.ProfileEditorState:
    """The editor on a profile that does not exist yet (its save is ``new``)."""

    draft = {key: value for key, value in document.items() if key != "seed"}
    return tui.ProfileEditorState(
        draft,
        cat=runtime.lineup_catalog(),
        bindings=selection._editor_bindings(runtime),
        effective=runtime.current_effective(),
        origin=None,
        is_seed=False,
        fallback_owners=_fallback_owners(runtime, exclude=None),
    )


def _profile_editor_state(runtime: runtime_mod.Runtime, name: str, verb: str) -> tui.ProfileEditorState:
    """The editor state for ``profile new|edit NAME`` (§4.13; ``new`` = the default seed renamed)."""

    store = runtime.profiles
    loaded: str | None = None
    if verb == "new":
        document = store.load(catalog.DEFAULT_SEED)
        document.pop("seed", None)
        document["name"] = name
        origin: str | None = None
        is_seed = False
        seed_update = None
    else:
        loaded = store.digest(name)
        document = store.load(name)
        origin = name
        is_seed = store.is_seed(name)
        seed_update = next(
            ((installed, shipped) for seed, installed, shipped in store.seed_updates() if seed == name),
            None,
        )
    return tui.ProfileEditorState(
        document,
        cat=runtime.lineup_catalog(),
        bindings=selection._editor_bindings(runtime),
        effective=runtime.current_effective(),
        origin=origin,
        is_seed=is_seed,
        seed_update=seed_update,
        loaded_digest=loaded,
        fallback_owners=_fallback_owners(runtime, exclude=origin),
    )


def _run_profile_editor(
    runtime: runtime_mod.Runtime,
    name: str,
    verb: str,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool = False,
) -> int | None:
    """``profile new|edit NAME`` on a capable terminal: the profile editor.

    The editor runs in its own curses session; saves, named-binding writes and
    the propagation prompt happen inside it through the store APIs
    (:func:`_profile_editor_callbacks`).  Nothing is performed or exec'd.

    Returns None only when curses could not start (the caller then takes the
    ``$EDITOR`` path).  An error raised once the session is up (a store
    write, a lock, the propagation) is reported, never fallen back from
    (AGENTS.md §4 fail closed): a save that committed before it is still
    reported, then the error propagates to ``main``.
    """

    state_ = _profile_editor_state(runtime, name, verb)
    palette = tui.detect_palette(
        runtime.environ, no_color=no_color, tty_in=input_stream, tty_out=output_stream
    )
    started = False

    def on_start() -> None:
        nonlocal started
        started = True

    try:
        outcome = tui.run_profile_editor(
            state_,
            input_stream=input_stream,
            output_stream=output_stream,
            environ=runtime.environ,
            palette=palette,
            callbacks=_profile_editor_callbacks(runtime, palette, editor_state=state_),
            initial_focus="general" if verb == "new" else None,
            on_start=on_start,
        )
    except Exception as exc:
        curses_failure = isinstance(exc, (tui.CursesError, OSError))
        if curses_failure and not started:
            return None
        if state_.saved is not None:
            output_stream.write(f"Saved profile {termtext.visible_text(state_.saved.name)!r}.\n")
        if curses_failure and not isinstance(exc, state.StateError):
            raise cli_errors.CLIError(f"the profile editor stopped: {exc}") from exc
        raise
    if outcome is None:
        output_stream.write("Nothing was saved.\n")
        return 0
    output_stream.write(f"Saved profile {termtext.visible_text(outcome.name)!r}.\n")
    return 0
