"""The ``profile`` command and its ``compose``/``show`` aliases."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import errors as cli_errors
from claude_multi import lineup as lineup_mod
from claude_multi import migrate_profiles
from claude_multi import profile as profile_mod
from claude_multi import readiness as readiness_mod
from claude_multi import strict_json
from claude_multi import termtext
from pathlib import Path
from typing import Any
from typing import TextIO
import argparse
import os
import shlex
import subprocess
import sys
import claude_multi.cli.parser as parser_mod
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.profile_editor as screens_profile_editor
import claude_multi.cli.selection as selection
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


PROFILE_LIST_HEADER = "profile\torigin\tstate\tused\tnotes"
EDITED_SEED_ORIGIN = "seed·edited"
DELETE_PROMPT = "Delete {name}? A copy is kept. [y/N] "
RESEED_PROMPT = "Replace your version with the shipped one? A copy is kept. [y/N] "
NEEDS_TERMINAL = "profile {verb} {name}: needs a terminal to confirm, or --yes"
NOTHING_CHANGED = "Nothing changed."


PROFILE_LIST_SCHEMA = 1


def profile_list_document(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """The ``profile list --json`` document, in the listing's order
    (connected first). ``ready`` is ``null`` for a profile that cannot be
    loaded; ``last_used`` is ``null`` when no session used it; ``seed`` is
    the shipped-copy state (``null`` for a profile of yours)."""

    from claude_multi.setup import profiles as setup_profiles

    return {
        "schema_version": PROFILE_LIST_SCHEMA,
        "list": "profiles",
        "profiles": [
            {
                "name": row.name,
                "origin": row.origin,
                "seed": row.seed_state,
                "loadable": row.loadable,
                "ready": row.ready,
                "state": row.state_text,
                "reason": row.reason_line or None,
                "last_used": row.last_used,
                "default": row.is_default,
                "fallback_for": row.fallback_for,
                "newer_shipped": row.seed_stale,
                "notes": list(row.notes),
            }
            for row in setup_profiles.rows(runtime)
        ],
    }


def _profile_list(runtime: runtime_mod.Runtime, output_stream: TextIO, *, as_json: bool = False) -> int:
    """One row per profile, connected ones first: origin, readiness, last use, notes."""

    from claude_multi.setup import profiles as setup_profiles

    if as_json:
        output_stream.write(strict_json.pretty_file_bytes(profile_list_document(runtime)).decode("utf-8"))
        return 0
    output_stream.write(PROFILE_LIST_HEADER + "\n")
    for row in setup_profiles.rows(runtime):
        origin = row.origin
        if row.seed_state in ("edited", "edited-stale", "unknown"):
            origin = EDITED_SEED_ORIGIN
        used = session_facts._last_used_age(row.last_used) if row.last_used else "-"
        cells = (row.name, origin, row.state_text, used, " · ".join(row.notes))
        output_stream.write("\t".join(termtext.visible_text(cell) for cell in cells) + "\n")
    return 0


def _confirmed(
    args: argparse.Namespace,
    *,
    verb: str,
    name: str,
    prompt: str,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int | None:
    """None to go ahead (``--yes``, or y on a terminal); else the exit code
    (1 off a terminal without ``--yes``, 3 declined)."""

    if getattr(args, "yes", False):
        return None
    if not interactive:
        sys.stderr.write(NEEDS_TERMINAL.format(verb=verb, name=termtext.visible_text(name)) + "\n")
        return 1
    sys.stderr.write(prompt)
    sys.stderr.flush()
    if (input_stream.readline() or "").strip().lower() not in ("y", "yes"):
        output_stream.write(NOTHING_CHANGED + "\n")
        return 3
    return None


def _write_lines(output_stream: TextIO, lines: Any) -> None:
    for line in lines:
        output_stream.write(termtext.visible_text(line) + "\n")


def _copy_profile(runtime: runtime_mod.Runtime, source: str, target: str, *, keep_fallback: bool,
                  output_stream: TextIO) -> int:
    from claude_multi.setup import model as setup_model
    from claude_multi.setup import profiles as setup_profiles

    selection._profile_name_or_composition(runtime, source)
    plan = setup_profiles.plan_copy(runtime, source, target, keep_fallback=keep_fallback)
    setup_profiles.apply_copy(runtime, plan, setup_model.Confirmation.given(plan))
    output_stream.write(
        f"Created {termtext.visible_text(target)!r} from {termtext.visible_text(source)!r}.\n"
    )
    return 0


def _profile_default(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    """``claude-multi profile default [NAME | --clear]``: show, set or clear the default profile."""

    from claude_multi.setup import defaults
    from claude_multi.setup import model as setup_model

    name = getattr(args, "name", None)
    clear = bool(getattr(args, "clear", False))
    if name is None and not clear:
        chosen = defaults.chosen_default(runtime)
        output_stream.write((defaults.DEFAULT_SHOW_SET.format(name=termtext.visible_text(chosen))
                             if chosen else defaults.DEFAULT_SHOW_AUTO) + "\n")
        choice = defaults.resolve_default(runtime)
        output_stream.write(defaults.DEFAULT_SHOW_HERE.format(
            resolved=termtext.visible_text(choice.name), reason_text=choice.reason_text) + "\n")
        return 0
    try:
        plan = defaults.set_default_plan(runtime, None if clear else name)
        defaults.apply_default(runtime, plan, setup_model.Confirmation.given(plan))
    except setup_model.SetupError as exc:
        sys.stderr.write(f"claude-multi: {termtext.visible_message(exc)}\n")
        return 1
    _write_lines(output_stream, plan.lines)
    return 0


def _profile_show(runtime: runtime_mod.Runtime, name: str | None, output_stream: TextIO) -> int:
    name = name or catalog.DEFAULT_SEED
    selection._profile_name_or_composition(runtime, name)
    document = runtime.profiles.load(name)
    lcat = runtime.lineup_catalog()
    eff = runtime.current_effective()
    evaluation = profile_mod.evaluate(
        document,
        lcat,
        bindings=runtime.bindings.bindings(),
        effective=eff,
        ad_hoc=False,
    )
    if evaluation.errors:
        raise cli_errors.CLIError(
            f"profile {name} is invalid:\n" + "\n".join(f"- {error}" for error in evaluation.errors)
        )
    text = lineup_mod.render_text(
        evaluation.lineup, header=f"profile {name}", profile_view=True, cat=lcat,
        workflow_default=lineup_mod.workflow_role(lcat, eff, evaluation.lineup),
    )
    for line in text.splitlines():
        output_stream.write(termtext.visible_text(line) + "\n")
    return 0


def _propagate_saved(
    runtime: runtime_mod.Runtime,
    names: list[str],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> None:
    """§8 saved from the CLI: preview, ask on a terminal, then apply per record lock."""

    found, unreadable = lineup_mod.followers(runtime, names)
    if not found and not unreadable:
        return
    running = [f for f in found if f.live and not f.has_pending and f.generation >= 1]
    output_stream.write(
        f"{len(found)} session(s) follow {', '.join(sorted(set(names)))} "
        f"({len(running)} running)\n"
    )
    apply_live = False
    if interactive and running:
        output_stream.write(f"apply to {len(running)} running session(s) now? [y/N] ")
        output_stream.flush()
        answer = (input_stream.readline() or "").strip().lower()
        apply_live = answer in ("y", "yes")
    lineup_mod.on_saved(runtime, names, apply_live=apply_live, out=output_stream)


def _edit_profile(
    runtime: runtime_mod.Runtime,
    document: dict[str, Any] | None,
    *,
    name: str,
    verb: str,
    output_stream: TextIO,
    raw: bytes | None = None,
    expected: str | None = None,
) -> bool:
    """``$VISUAL``/``$EDITOR`` on a 0600 temp copy; parse + evaluate + save (§11.2).

    ``raw`` edits a file that cannot be loaded as it is stored. ``expected``
    (the digest read before editing) refuses the save when the profile
    changed meanwhile. Errors are shown, the temp path is kept and False is
    returned (exit 2).
    """

    import tempfile

    editor = runtime.environ.get("VISUAL") or runtime.environ.get("EDITOR")
    if not editor:
        raise cli_errors.CLIError(cli_text.PROFILE_EDITOR_REFUSAL.format(verb=verb))
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"claude-multi-profile-{name}-", suffix=".json"
    )
    path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw if raw is not None else strict_json.pretty_file_bytes(document))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    env = dict(runtime.environ)
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    completed = subprocess.run([*shlex.split(editor), str(path)], env=env, check=False)
    problems: list[str] = []
    edited: dict[str, Any] | None = None
    if completed.returncode != 0:
        problems.append(f"the editor exited with status {completed.returncode}")
    else:
        try:
            edited = profile_mod.parse(strict_json.loads(path.read_bytes()))
            edited["name"] = name
            evaluation = profile_mod.evaluate(
                edited,
                runtime.lineup_catalog(),
                bindings=runtime.bindings.bindings(),
                effective=runtime.current_effective(),
                ad_hoc=False,
            )
            problems.extend(evaluation.errors)
        except profile_mod.ProfileValidationError as exc:
            problems.extend(exc.errors)
        except (OSError, ValueError) as exc:
            problems.append(str(exc))
    if problems or edited is None:
        output_stream.write(f"profile {name} was not saved:\n")
        for problem in problems:
            output_stream.write(f"- {termtext.visible_text(problem)}\n")
        output_stream.write(cli_text.RAW_EDIT_KEPT.format(tmp=path) + "\n")
        return False
    if verb == "new":
        runtime.profiles.new(edited)
    else:
        try:
            runtime.profiles.save(edited, target=name, expected=expected)
        except profile_mod.ProfileChangedError:
            output_stream.write(f"profile {name} changed while you were editing; not saved\n")
            output_stream.write(cli_text.RAW_EDIT_KEPT.format(tmp=path) + "\n")
            return False
    path.unlink(missing_ok=True)
    output_stream.write(f"Saved profile {termtext.visible_text(name)!r}.\n")
    return True


def _profile_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    command: str,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """``claude-multi profile …`` and its legacy ``compose``/``show`` aliases."""

    from claude_multi.setup import model as setup_model

    try:
        return _profile_subcommand(runtime, args, command, input_stream=input_stream,
                                   output_stream=output_stream, interactive=interactive)
    except setup_model.SetupError as exc:
        # The command-line form of a setup refusal (it names commands, not keys).
        raise cli_errors.CLIError(exc.line_text) from exc


def _profile_subcommand(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    command: str,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:

    if command == "migrate":
        # Dispatched before the install_seeds preamble. The migration's 2 is
        # a blocking report: a refused conversion, exit 1 in the command table.
        code = migrate_profiles.run_command(runtime, apply=bool(getattr(args, "profile_migrate_apply", False)),
                                            output=output_stream)
        return 1 if code == 2 else code
    if command == "list":
        return _profile_list(runtime, output_stream, as_json=bool(getattr(args, "json_output", False)))
    if command == "show":
        return _profile_show(runtime, getattr(args, "name", None), output_stream)
    if command == "starter":
        return _profile_starter(runtime, args, input_stream=input_stream, output_stream=output_stream,
                                interactive=interactive)
    if command == "default":
        return _profile_default(runtime, args, output_stream)
    store = runtime.profiles
    if runtime.allow_state_writes:
        store.install_seeds()  # Write-allowed profile commands only
    if command == "duplicate":
        return _copy_profile(runtime, args.source, args.target,
                             keep_fallback=bool(getattr(args, "keep_fallback", False)), output_stream=output_stream)
    if command == "new":
        source = getattr(args, "from_profile", None)
        if source is not None:
            return _copy_profile(runtime, source, args.name,
                                 keep_fallback=bool(getattr(args, "keep_fallback", False)),
                                 output_stream=output_stream)
        store.require_new_target(args.name)
        if not interactive:
            raise cli_errors.CLIError(
                f"profile new {args.name} needs a terminal for the editor, or --from SRC"
            )
        if screens_common._curses_ok(bool(getattr(args, "line", False)), input_stream, output_stream):
            # The full-screen editor on a capable terminal;
            # None = curses could not start here: the $EDITOR path below.
            code = screens_profile_editor._run_profile_editor(
                runtime, args.name, "new", input_stream=input_stream,
                output_stream=output_stream, no_color=bool(getattr(args, "no_color", False)),
            )
            if code is not None:
                return code
        document = store.load(catalog.DEFAULT_SEED)
        document.pop("seed", None)
        document["name"] = args.name
        return 0 if _edit_profile(
            runtime, document, name=args.name, verb="new", output_stream=output_stream
        ) else 2
    if command == "edit":
        selection._profile_name_or_composition(runtime, args.name)
        if not interactive:
            raise cli_errors.CLIError(f"profile edit {args.name} needs a terminal for the editor")
        loaded_digest = store.digest(args.name)
        try:
            store.load(args.name)
        except profile_mod.ProfileLoadError:
            # A file that cannot be loaded is edited as it is stored.
            if not _edit_profile(runtime, None, name=args.name, verb="edit", output_stream=output_stream,
                                 raw=store.raw_bytes(args.name), expected=loaded_digest):
                return 1
            _propagate_saved(runtime, [args.name], input_stream=input_stream, output_stream=output_stream,
                             interactive=interactive)
            return 0
        if screens_common._curses_ok(bool(getattr(args, "line", False)), input_stream, output_stream):
            # The full-screen editor on a capable terminal;
            # None = curses could not start here: the $EDITOR path below.
            code = screens_profile_editor._run_profile_editor(
                runtime, args.name, "edit", input_stream=input_stream,
                output_stream=output_stream, no_color=bool(getattr(args, "no_color", False)),
            )
            if code is not None:
                return code
        document = store.load(args.name)
        if not _edit_profile(
            runtime, document, name=args.name, verb="edit", output_stream=output_stream,
            expected=loaded_digest,
        ):
            return 1
        _propagate_saved(
            runtime,
            [args.name],
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )
        return 0
    if command == "rm":
        from claude_multi.setup import model as setup_model
        from claude_multi.setup import profiles as setup_profiles

        if not store.contains(args.name):
            selection._profile_name_or_composition(runtime, args.name)
        delete_plan = setup_profiles.plan_delete(runtime, args.name)
        _write_lines(output_stream, delete_plan.lines)
        code = _confirmed(args, verb="rm", name=args.name, prompt=DELETE_PROMPT.format(name=args.name),
                          input_stream=input_stream, output_stream=output_stream, interactive=interactive)
        if code is not None:
            return code
        outcome = setup_profiles.apply_delete(runtime, delete_plan, setup_model.Confirmation.given(delete_plan))
        _write_lines(output_stream, outcome.lines)
        return 0
    if command == "rename":
        from claude_multi.setup import model as setup_model
        from claude_multi.setup import profiles as setup_profiles

        selection._profile_name_or_composition(runtime, args.source)
        rename_plan = setup_profiles.plan_rename(runtime, args.source, args.target)
        renamed = setup_profiles.apply_rename(runtime, rename_plan, setup_model.Confirmation.given(rename_plan))
        output_stream.write(
            f"Renamed {termtext.visible_text(args.source)!r} to {termtext.visible_text(args.target)!r}.\n"
        )
        _write_lines(output_stream, renamed.lines[1:])
        if renamed.default_moved:
            output_stream.write(f"Default profile: {termtext.visible_text(args.target)}.\n")
        return 0
    if command == "reseed":
        from claude_multi.setup import model as setup_model
        from claude_multi.setup import profiles as setup_profiles

        selection._profile_name_or_composition(runtime, args.name)
        reseed_plan = setup_profiles.plan_reseed(runtime, args.name)
        _write_lines(output_stream, reseed_plan.lines)
        if reseed_plan.nothing:
            return 0
        code = _confirmed(args, verb="reseed", name=args.name, prompt=RESEED_PROMPT,
                          input_stream=input_stream, output_stream=output_stream, interactive=interactive)
        if code is not None:
            return code
        reseeded = setup_profiles.apply_reseed(runtime, reseed_plan, setup_model.Confirmation.given(reseed_plan))
        _write_lines(output_stream, reseeded.lines)
        _propagate_saved(
            runtime,
            [args.name],
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )
        return 0
    raise cli_errors.CLIError(f"unsupported profile command {command!r}")


def _profile_starter(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """``claude-multi profile starter [--name NAME] [--apply]``.

    Preview by default (writes nothing). ``--apply`` confirms and saves
    through ``ProfileStore.new``, which refuses an existing name (a seed or
    a user profile) under its lock. Never an eighth seed; never an
    admission, qualification or provider call."""

    name = str(getattr(args, "starter_name", readiness_mod.STARTER_NAME))
    store = runtime.profiles
    if store.contains(name):
        sys.stderr.write(f"profile starter: profile {termtext.visible_text(name)!r} already exists; "
                         "choose another --name — nothing written\n")
        return 1
    plan, warnings = runtime.starter_plan(name)
    if plan.document is None:
        sys.stderr.write(termtext.visible_text(str(plan.refusal)) + "\n")
        return 1
    # The legend precedes the exact preview block, never inside it;
    # the spend notes come right before its footer.
    from claude_multi.setup import defaults

    evaluation = profile_mod.evaluate(plan.document, runtime.lineup_catalog(), bindings=runtime.bindings.bindings(),
                                      effective=runtime.current_effective(), ad_hoc=False)
    notes = defaults.spend_notes(runtime, evaluation.lineup) if evaluation.lineup is not None else ()
    preview = readiness_mod.starter_preview(plan, name=name, warnings=warnings).splitlines()
    output_stream.write(readiness_mod.LEGEND + "\n")
    for line in [*preview[:-1], *notes, *preview[-1:]]:
        output_stream.write(termtext.visible_text(line) + "\n")
    if not getattr(args, "starter_apply", False):
        output_stream.write(f"save it: claude-multi profile starter --name {name} --apply\n")
        return 0
    if not interactive:
        raise cli_errors.CLIError("profile starter --apply needs a terminal to confirm the save")
    output_stream.write(readiness_mod.STARTER_CONFIRM.format(name=name))
    output_stream.flush()
    answer = input_stream.readline()
    if answer.strip().lower() not in ("y", "yes"):
        output_stream.write("Nothing written.\n")
        return 1
    store.new(plan.document)
    output_stream.write(f"Saved profile {termtext.visible_text(name)!r} (launch it: claude-multi --profile {name}).\n")
    return 0


def _compose_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """The ``compose`` command: the earlier spelling of ``profile``."""

    # Every `compose` subcommand is an alias of `profile`.
    command = args.compose_command
    mapped = parser_mod._COMPOSE_ALIASES[command]
    if mapped == "rm":
        args.yes = True  # as in the earlier launcher: the alias never asked
    if command == "restore-default":
        sys.stderr.write(cli_text.COMPOSE_RESTORE_DEFAULT_NOTE + "\n")
        if interactive:
            output_stream.write("overwrite the balanced seed with its shipped version? [y/N] ")
            output_stream.flush()
            answer = (input_stream.readline() or "").strip().lower()
            if answer not in ("y", "yes"):
                output_stream.write("Nothing was changed.\n")
                return 0
        args.name = catalog.DEFAULT_SEED
        args.yes = True  # confirmed above on a terminal; as in the earlier launcher off one
    else:
        sys.stderr.write(
            cli_text.ALIAS_NOTE.format(old=f"compose {command}", new=f"profile {mapped}") + "\n"
        )
    return _profile_command(
        runtime,
        args,
        mapped,
        input_stream=input_stream,
        output_stream=output_stream,
        interactive=interactive,
    )


def _show_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """The ``show`` command: the earlier spelling of ``profile show``."""

    # `show [NAME]` is the earlier spelling of `profile show [NAME]`.
    sys.stderr.write(cli_text.ALIAS_NOTE.format(old="show", new="profile show") + "\n")
    args.name = args.show_composition
    return _profile_command(
        runtime,
        args,
        "show",
        input_stream=input_stream,
        output_stream=output_stream,
        interactive=interactive,
    )
