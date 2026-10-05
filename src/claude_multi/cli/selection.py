"""Selection."""

from __future__ import annotations

from claude_multi import errors as cli_errors

from typing import Any
from typing import TextIO
import argparse
from claude_multi import catalog
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types
from claude_multi import errors
from claude_multi import profile as profile_mod
import claude_multi.cli.session_facts as session_facts
from claude_multi import sessions
from claude_multi import state
from claude_multi import strict_json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _pick_order_from(
    names: list[str], here: dict[str, str], elsewhere: dict[str, str]
) -> list[str]:
    """Most-recently-used pick order: this directory's compositions first
    (recency desc, name asc on ties), then globally-recent ones, then
    never-launched names alphabetically. Store names are the namespace —
    records of deleted compositions never resurrect them.
    """

    namespace = set(names)
    recent_here = {k: v for k, v in here.items() if k in namespace}
    # "Elsewhere" is strictly compositions used ONLY in other directories —
    # a name used here AND elsewhere appears once, in the here tier.
    recent_else = {
        k: v
        for k, v in elsewhere.items()
        if k in namespace and k not in recent_here
    }

    def _rank(table: dict[str, str]) -> list[str]:
        # stable sort: names ascending, then stamps descending over that
        return sorted(sorted(table), key=lambda n: table[n], reverse=True)

    ranked = _rank(recent_here) + _rank(recent_else)
    remainder = sorted(namespace - set(recent_here) - set(recent_else))
    return ranked + remainder


def _profile_choice_items(runtime: runtime_mod.Runtime, *, current: str | None = None, direct: bool = True) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Profile names in MRU order (``profile_pick_order``), plus ``direct — choose a model…``.

    Returns ``(labels, choices)``; a choice is ``("profile", name)`` or
    ``("direct", None)``.
    """

    names = profile_pick_order(runtime)
    labels = [name for name in names]
    choices: list[tuple[str, str | None]] = [("profile", name) for name in names]
    if current in names:
        labels[names.index(current)] = f"{current}  (current)"
    if direct:
        labels.append(cli_text.SESSIONS_DIRECT_ITEM)
        choices.append(("direct", None))
    return labels, choices


def _editor_bindings(runtime: runtime_mod.Runtime) -> dict[str, dict[str, Any]]:
    try:
        return runtime.bindings.bindings()
    except profile_mod.BindingError as exc:
        raise cli_errors.CLIError(str(exc)) from exc


def _profile_recency(runtime: runtime_mod.Runtime) -> tuple[dict[str, str], dict[str, str]]:
    """Per-profile last use from v4 records, split this cwd / elsewhere (§4.1.2).

    Records with a null ``profile`` (ad-hoc direct) and v1–3 records are
    ignored; the stamp is ``_record_last_seen``.
    """

    here: dict[str, str] = {}
    elsewhere: dict[str, str] = {}
    for record in session_facts._session_records(runtime):
        if record.get("version") != sessions.RECORD_VERSION:
            continue
        name = record.get("profile")
        if not isinstance(name, str) or not name:
            continue
        try:
            stamp = session_facts._record_last_seen(record)
        except KeyError:
            continue
        table = here if record.get("cwd") == runtime.cwd else elsewhere
        if stamp > table.get(name, ""):
            table[name] = stamp
    return here, elsewhere


def profile_pick_order(runtime: runtime_mod.Runtime, *, current: str | None = None) -> list[str]:
    """The profile order for Tab and the pickers: connected profiles first,
    then the others, then those that cannot be loaded; within a group this
    directory's most recently used first, then those used only elsewhere,
    then the never-used ones alphabetically (``setup.defaults.profile_choices``).
    A record naming a profile that no longer exists never resurrects it.
    ``current`` (the card's profile) goes first.
    """

    from claude_multi.setup import defaults

    return [choice.name for choice in defaults.profile_choices(runtime, current=current)]


def _card_action(plan: cli_types.PreparedLaunch) -> str:
    """``fresh`` / ``resume`` / ``relaunch`` of a prepared launch (the card's action word)."""

    if plan.target.kind == "relaunch":
        return "relaunch"
    if plan.result.session_action.kind == "fresh":
        return "fresh"
    return "resume"


def _record_resume_target(record: dict[str, Any]) -> cli_types.LaunchTarget:
    """The resume target ``_resume_flow`` builds for a record."""

    mid = sessions.managed_id(record)
    return cli_types.LaunchTarget(
        "record",
        None,
        record.get("profile") if record.get("version") == sessions.RECORD_VERSION else None,
        bool(record.get("follow", False)),
        f"Session {mid[:8]} lineup",
    )


def _profile_name_or_composition(runtime: runtime_mod.Runtime, name: str) -> None:
    """Distinguish a legacy composition name from an unknown profile."""

    if runtime.profiles.contains(name):
        return
    try:
        is_composition = runtime.compositions.contains(name)
    except (cli_errors.CLIError, state.StateError, OSError):
        is_composition = False
    if is_composition:
        raise cli_errors.CLIError(cli_text.PROFILE_IS_COMPOSITION.format(name=name))
    raise profile_mod.ProfileError(f"profile {name!r} does not exist")


def _resolve_resume_target(runtime: runtime_mod.Runtime, value: str) -> str:
    """The managed id of the session a resume names (``session_facts.resolve_session``)."""

    return sessions.managed_id(session_facts.resolve_session(runtime, value))


def _refuse_resume_override(
    profile_name: str | None,
    record: dict[str, Any],
    *,
    profile_file: str | None = None,
) -> None:
    """A resume always uses the session's lineup."""

    rid = sessions.runtime_session_id(record)
    if profile_file is not None:
        raise cli_errors.CLIError(cli_text.RESUME_PROFILE_FILE_REFUSAL.format(rid=rid))
    if profile_name is None:
        return
    if record.get("version") == sessions.RECORD_VERSION:
        recorded = record["profile"]
    else:
        recorded = record.get("composition_name")
    if profile_name != recorded:
        raise cli_errors.CLIError(
            cli_text.RESUME_PROFILE_OVERRIDE_REFUSAL.format(
                name=profile_name, mid=sessions.managed_id(record), rid=rid
            )
        )


def _load_profile_argument(value: str, inp: TextIO) -> dict[str, Any]:
    """An unsaved profile from a file path or stdin ('-'), parsed.

    The stdin read is bounded at the strict-JSON byte limit plus one (the
    legacy ``--composition-file -`` bound), so an oversized pipe is rejected
    before an unbounded allocation.
    """

    try:
        if value == "-":
            limit = strict_json.DEFAULT_LIMITS.max_bytes
            raw_stream = getattr(inp, "buffer", None)
            if raw_stream is not None:
                raw = raw_stream.read(limit + 1)
            else:  # injected text streams (tests) have no .buffer
                raw = inp.read(limit + 1).encode("utf-8")
            if len(raw) > limit:
                raise ValueError(f"$: input exceeds the {limit}-byte limit")
            return profile_mod.parse(strict_json.loads(raw))
        return profile_mod.load_profile_file(value)
    except profile_mod.ProfileValidationError as exc:
        raise cli_errors.CLIError(
            f"cannot load profile file {value!r}: " + "; ".join(exc.errors)
        ) from exc
    except (OSError, ValueError) as exc:
        raise cli_errors.CLIError(f"cannot load profile file {value!r}: {exc}") from exc


def remembered_profile(runtime: runtime_mod.Runtime) -> str:
    """The profile a launch uses when none is named (``default_profile``)."""

    return default_profile(runtime)[0]


def default_profile(runtime: runtime_mod.Runtime) -> tuple[str, str | None]:
    """``(name, warning)`` of ``setup.defaults.resolve_default``: this
    directory's most recently used profile, never replaced; else the chosen
    default when connected, the most recently used connected profile, the
    first connected shipped profile, the first connected profile of yours;
    else ``DEFAULT_SEED`` with the warning. The decision is kept on the
    runtime (``selection_default``). Nothing is persisted and no request
    goes upstream; passive quota never switches the choice."""

    from claude_multi.setup import defaults

    choice = defaults.resolve_default(runtime)
    runtime.selection_default = choice
    warning = choice.notice.removeprefix("! ") if choice.notice is not None else None
    return choice.name, warning


def _cwd_recent_profile(runtime: runtime_mod.Runtime) -> str | None:
    best: tuple[str, str] | None = None
    profiles = runtime.profiles
    for record in session_facts._session_records(runtime):
        if record.get("version") != sessions.RECORD_VERSION or record.get("cwd") != runtime.cwd:
            continue
        name = record.get("profile")
        if not name:
            continue
        try:
            if not profiles.contains(name):
                continue
        except profile_mod.ProfileError:
            continue
        stamp = str(record.get("last_seen_at", ""))
        if best is None or stamp > best[0]:
            best = (stamp, name)
    return best[1] if best is not None else None


def _normalize_profile_args(runtime: runtime_mod.Runtime, args: argparse.Namespace) -> None:
    """The legacy ``--composition``/``--composition-file`` aliases."""

    name = getattr(args, "composition", None)
    if name is not None:
        sys.stderr.write(cli_text.ALIAS_NOTE.format(old="--composition", new="--profile") + "\n")
        if runtime.profiles.contains(name):
            args.profile = name
        elif runtime.compositions.contains(name):
            raise cli_errors.CLIError(cli_text.PROFILE_IS_COMPOSITION.format(name=name))
        else:
            raise profile_mod.ProfileError(f"profile {name!r} does not exist")
    path = getattr(args, "composition_file", None)
    if path is not None:
        sys.stderr.write(
            cli_text.ALIAS_NOTE.format(old="--composition-file", new="--profile-file") + "\n"
        )
        args.profile_file = path


def _fresh_target(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, inp: TextIO, *, read_only: bool
) -> cli_types.LaunchTarget:
    """The fresh-launch target of ``--profile``/``--profile-file``/the default (§6.8)."""

    if args.profile_file is not None:
        document = _load_profile_argument(args.profile_file, inp)
        source = (
            "Profile from stdin"
            if args.profile_file == "-"
            else f"Profile file {args.profile_file}"
        )
        return cli_types.LaunchTarget("profile-file", document, None, False, source)
    explicit = args.profile is not None
    if explicit:
        name = args.profile
    else:
        name, warning = default_profile(runtime)
        if warning is not None:
            runtime.selection_notices.append(warning)
    if runtime.allow_state_writes and not read_only:
        runtime.profiles.install_seeds()
    runtime.selection_load_error = None
    try:
        document = runtime.profiles.load(name)
    except profile_mod.ProfileLoadError as exc:
        from claude_multi.setup import defaults

        exc.remedy = defaults.UNLOADABLE_FIX.format(name=name)
        if explicit:
            raise
        # A profile chosen by default that cannot be loaded opens a blocked
        # card naming the file and line; another profile can still be picked.
        runtime.selection_load_error = exc
        return cli_types.LaunchTarget("unloadable", None, name, False, f"Profile {name}")
    return cli_types.LaunchTarget("profile", document, name, True, f"Profile {name}")
