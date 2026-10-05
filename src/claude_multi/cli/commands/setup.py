"""``claude-multi setup``: the setup steps in line mode.

A bare run prints where setup stands and runs every step that is not done,
in order (``--redo`` runs the done ones too); ``--step NAME`` runs one step,
also when it is done; ``--status`` only reports (exit 0 when every required
step is done). ``--answers FILE`` runs a prepared setup and ``--keys-file
PATH`` selects an existing key file. ``--step claude`` puts claude-multi's
own copy of the pinned Claude Code in place (``claude_multi.acquire``);
``--step gateway [--proxy URL | --no-proxy]`` records the gateway's outbound
proxy and starts the gateway. Nothing is saved until the person confirms;
inside a Claude session the steps that need a person (a download, providers,
the test, the key file) refuse.

Exit statuses: 0 ok; 1 a step refused, failed or is not done; 2 usage or an
invalid answers file; 3 declined; 130 cancelled.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable, TextIO

from claude_multi import acquire
from claude_multi import errors
from claude_multi import errors as cli_errors
from claude_multi import paths
from claude_multi import pin
from claude_multi import retention
from claude_multi import termtext
import claude_multi.cli.consent as consent
import claude_multi.cli.runtime as runtime_mod

# Exit codes: done; refused (the human guard), not set up or failed; usage;
# declined; cancelled.
EXIT_READY, EXIT_FAILED, EXIT_USAGE, EXIT_DECLINED, EXIT_CANCELLED = 0, 1, 2, 3, 130


def _setup_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO | None,
    output_stream: TextIO,
    interactive: bool = True,
) -> int:
    import claude_multi.cli.parser as parser_mod
    from claude_multi.setup import texts

    step = args.step
    if step is None:
        # An option of one step selects that step.
        implied = [other for other, names in parser_mod.SETUP_STEP_OPTIONS.items()
                   if any(getattr(args, name, None) not in (None, False) for name in names)]
        if len(implied) == 1:
            step = args.step = implied[0]
    misplaced = [name for other, names in parser_mod.SETUP_STEP_OPTIONS.items() if other != step
                 for name in names if getattr(args, name, None) not in (None, False)]
    if misplaced:
        flags = ", ".join("--" + name.replace("_", "-") for name in misplaced)
        consent.prompt_stream().write(
            f"claude-multi setup: {flags} belongs to another step than --step {step}\n")
        return EXIT_USAGE
    modes = [flag for flag, on in (("--status", getattr(args, "setup_status", False)),
                                   ("--answers", getattr(args, "answers", None) is not None),
                                   ("--keys-file", getattr(args, "keys_file", None) is not None),
                                   ("--step", step is not None)) if on]
    if len(modes) > 1:
        consent.prompt_stream().write(f"claude-multi setup: {' and '.join(modes)} exclude each other\n")
        return EXIT_USAGE
    write = _writer(output_stream)
    try:
        if getattr(args, "setup_status", False):
            return _status(runtime, write)
        if getattr(args, "answers", None) is not None:
            return _answers(runtime, args.answers, input_stream=input_stream, write=write)
        if getattr(args, "keys_file", None) is not None:
            return _keys_file(runtime, args.keys_file, input_stream=input_stream, write=write)
        if step == "claude":
            return claude_step(runtime, claude_from=args.claude_from,
                               input_stream=input_stream, output_stream=output_stream)
        if step == "gateway":
            import claude_multi.cli.commands.gateway as commands_gateway

            return commands_gateway.setup_command(runtime, args, input_stream=input_stream,
                                                  output_stream=output_stream)
        if not interactive:
            # A valid command this place cannot run: refused (1), not usage.
            consent.prompt_stream().write(texts.SETUP_NEEDS_TERMINAL + "\n")
            return EXIT_FAILED
        if step is not None:
            return _run_step(runtime, step, input_stream=input_stream, output_stream=output_stream, write=write)
        return _bare(runtime, redo=bool(getattr(args, "redo", False)), input_stream=input_stream,
                     output_stream=output_stream, write=write)
    except KeyboardInterrupt:
        write(texts.SETUP_CANCELLED)
        return EXIT_CANCELLED


def _writer(output_stream: TextIO) -> Callable[[str], None]:
    def write(line: str) -> None:
        output_stream.write("".join(termtext.visible_text(item) + "\n" for item in str(line).split("\n")))
        output_stream.flush()

    return write


def _fail(write: Callable[[str], None], exc: BaseException) -> int:
    from claude_multi.setup import model

    text = exc.line_text if isinstance(exc, model.SetupError) else str(exc)
    remedy = getattr(exc, "remedy", None)
    consent.prompt_stream().write(f"claude-multi: {termtext.visible_message(text)}\n")
    if remedy and remedy not in text:
        consent.prompt_stream().write(f"  fix: {termtext.visible_message(remedy)}\n")
    return EXIT_DECLINED if isinstance(exc, model.Declined) else EXIT_FAILED


# ------------------------------------------------------------------ status


def _status(runtime: runtime_mod.Runtime, write: Callable[[str], None]) -> int:
    from claude_multi.setup import status

    state = status.snapshot(runtime)
    for line in status.status_lines(state):
        write(line)
    return EXIT_READY if state.ready else EXIT_FAILED


# ------------------------------------------------------------------ bare run and steps


def _bare(runtime: runtime_mod.Runtime, *, redo: bool, input_stream: TextIO | None, output_stream: TextIO,
          write: Callable[[str], None]) -> int:
    from claude_multi.setup import status, texts

    write(texts.SETUP_HEADER)
    state = status.snapshot(runtime)
    for line in status.status_lines(state):
        write(line)
    for step in status.STEP_ORDER:
        current = status.snapshot(runtime).step(step)
        if current.state == "done" and not redo:
            continue
        if step == "test" and not redo and current.state == "optional":
            pass
        code = _run_step(runtime, step, input_stream=input_stream, output_stream=output_stream, write=write)
        if code == EXIT_CANCELLED or (code != EXIT_READY and step != "test"):
            return code
    write(texts.SETUP_DONE)
    return EXIT_READY


def _in_session(runtime: runtime_mod.Runtime, verb: str) -> str | None:
    marker = consent.session_marker(runtime.environ)
    return consent.guard_text(verb, f"{marker} set") if marker else None


def _run_step(runtime: runtime_mod.Runtime, step: str, *, input_stream: TextIO | None, output_stream: TextIO,
              write: Callable[[str], None]) -> int:
    from claude_multi.setup import model

    handlers: dict[str, Callable[..., int]] = {
        "preflight": _step_preflight, "claude": _step_claude, "gateway": _step_gateway,
        "providers": _step_providers, "test": _step_test, "profile": _step_profile, "check": _step_check,
    }
    try:
        return handlers[step](runtime, input_stream=input_stream, output_stream=output_stream, write=write)
    except (model.SetupError, cli_errors.ClaudeMultiError) as exc:
        return _fail(write, exc)


def _step_preflight(runtime: runtime_mod.Runtime, *, write: Callable[[str], None], **_: Any) -> int:
    from claude_multi.setup import firstrun, texts

    items = firstrun.checks(runtime)[:3]
    for line in firstrun.render_lines(items, surface="cli"):
        write(line)
    if any(item.state == "fail" for item in items):
        write(texts.SETUP_PREFLIGHT_STOP)
        return EXIT_FAILED
    return EXIT_READY


def _step_claude(runtime: runtime_mod.Runtime, *, input_stream: TextIO | None, output_stream: TextIO,
                 **_: Any) -> int:
    return claude_step(runtime, input_stream=input_stream, output_stream=output_stream)


def _step_gateway(runtime: runtime_mod.Runtime, *, write: Callable[[str], None], **_: Any) -> int:
    from claude_multi.setup import external, texts

    write(texts.SETUP_GATEWAY_STARTING)
    outcome = external.ensure_gateway(runtime)
    if outcome is None:
        write("The local gateway is running.")
        return EXIT_READY
    for line in outcome.lines():
        write(line)
    return EXIT_READY if outcome.ok else EXIT_FAILED


def _namespace(**values: Any) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _step_providers(runtime: runtime_mod.Runtime, *, input_stream: TextIO | None, output_stream: TextIO,
                    write: Callable[[str], None]) -> int:
    """The numbered menu of ways to connect; each choice runs its command
    form (keys typed hidden, the acknowledgement typed, approvals y/N).

    A choice cancelled with Ctrl-C ends setup at once (130). Leaving the
    menu reports the last choice's outcome when it was not a success (3 when
    you said no, 1 when it was refused or failed), else 0 when a provider is
    connected and 1 when none is."""

    from claude_multi.setup import providers, status, texts

    refusal = _in_session(runtime, "setup --step providers")
    if refusal is not None:
        consent.prompt_stream().write(refusal + "\n")
        return EXIT_FAILED
    last = EXIT_READY
    while True:
        entries = providers.picker_entries(runtime)
        numbered: list[Any] = []
        group = None
        for entry in entries:
            if entry.group_title != group:
                group = entry.group_title
                write(texts.SETUP_MENU_GROUP.format(title=group))
            if not entry.available:
                write(texts.SETUP_MENU_UNAVAILABLE.format(label=entry.label, note=entry.note or entry.state_text))
                continue
            numbered.append(entry)
            write(texts.SETUP_MENU_ITEM.format(n=len(numbered), label=entry.label, state=entry.state_text or "—"))
        prompt = consent.prompt_stream()
        prompt.write(texts.SETUP_PROVIDERS_QUESTION)
        prompt.flush()
        answer = (consent._answer_stream(input_stream).readline() or "").strip().lower()
        if answer in ("", "q"):
            if last != EXIT_READY:
                return last
            return EXIT_READY if status.connected(runtime) else EXIT_FAILED
        if not answer.isdigit() or not 1 <= int(answer) <= len(numbered):
            write(texts.SETUP_WRONG_CHOICE)
            continue
        last = _connect(runtime, numbered[int(answer) - 1], input_stream=input_stream, output_stream=output_stream,
                        write=write)
        if last == EXIT_CANCELLED:
            write(texts.SETUP_CANCELLED)
            return EXIT_CANCELLED


def _connect(runtime: runtime_mod.Runtime, entry: Any, *, input_stream: TextIO | None, output_stream: TextIO,
             write: Callable[[str], None]) -> int:
    import claude_multi.cli.commands.providers as providers_cmd
    from claude_multi.setup import model, providers, texts

    kind = entry.id.split(":")
    try:
        # An account provider's account and API key are its two transports.
        account_provider = entry.provider_id in providers.ACCOUNT_PROVIDERS
        if entry.kind == "account":
            if account_provider and not entry.current:
                code = providers_cmd._providers_transport(
                    runtime, _namespace(provider_id=entry.provider_id, transport_choice="oauth-pool",
                                        secret_file=None),
                    input_stream=input_stream, output_stream=output_stream)
                if code != EXIT_READY:
                    return code
            return providers_cmd._providers_sign_in(
                runtime, _namespace(provider_id=entry.provider_id, no_browser=False),
                input_stream=input_stream, output_stream=output_stream)
        if entry.kind == "api-key" and account_provider:
            return providers_cmd._providers_transport(
                runtime, _namespace(provider_id=entry.provider_id, transport_choice="api-key", secret_file=None),
                input_stream=input_stream, output_stream=output_stream)
        if entry.kind == "api-key":
            return providers_cmd._providers_set_key(
                runtime, _namespace(provider_id=entry.provider_id, secret_file=None),
                input_stream=input_stream, output_stream=output_stream)
        if entry.kind == "own":
            conn = entry.state_text
            if conn in (texts.STATE_TEXT["route-unapproved"], texts.STATE_TEXT["route-changed"]):
                return providers_cmd._providers_approve(runtime, entry.provider_id, input_stream=input_stream,
                                                        output_stream=output_stream)
            if conn in (texts.STATE_TEXT["key-missing"], texts.STATE_TEXT["key-invalid"]):
                return providers_cmd._providers_set_key(
                    runtime, _namespace(provider_id=entry.provider_id, secret_file=None),
                    input_stream=input_stream, output_stream=output_stream)
            write(f"{entry.provider_id}: {conn or 'connected'}")
            return EXIT_READY
        if entry.kind == "endpoint":
            return _connect_endpoint(runtime, kind[1], input_stream=input_stream, write=write)
        if entry.kind == "preset":
            return _connect_preset(runtime, kind[1], input_stream=input_stream, write=write)
        if entry.kind == "lan":
            return _connect_lan(runtime, kind[2], input_stream=input_stream, write=write)
    except (model.SetupError, cli_errors.ClaudeMultiError) as exc:
        return _fail(write, exc)
    return EXIT_READY


def _ask(prompt: str, input_stream: TextIO | None, default: str = "") -> str:
    out = consent.prompt_stream()
    out.write(prompt + (f" [{default}]" if default else "") + ": ")
    out.flush()
    answer = (consent._answer_stream(input_stream).readline() or "").strip()
    return answer or default


def _connect_endpoint(runtime: runtime_mod.Runtime, endpoint_kind: str, *, input_stream: TextIO | None,
                      write: Callable[[str], None]) -> int:
    from claude_multi.setup import model, providers, texts

    consent.require_human("providers add", runtime.gateway_environ())
    values = {
        "kind": endpoint_kind,
        "id": _ask("Name for this provider (a–z, 0–9, -)", input_stream),
        "url": _ask("Base URL from your vendor's documentation (https://…)", input_stream),
        "auth": (_ask("How the key is sent (header or bearer)", input_stream, "header")
                 if endpoint_kind == "anthropic-compatible" else "bearer"),
        "family": _ask("Who makes the models (a family like deepseek; unknown if unsure)", input_stream, "unknown"),
        "listing": _ask("Model list URL (optional)", input_stream),
    }
    plan = providers.plan_add_endpoint(runtime, values)
    for line in plan.lines:
        write(line)
    if not consent.confirm(texts.APPROVE_QUESTION, input_stream=input_stream):
        raise model.Declined()
    typed = consent.read_secret(texts.KEY_PROMPT.format(display=plan.provider_id), input_stream=input_stream)
    value = model.Secret(typed) if typed else None
    applied = providers.apply_add_endpoint(runtime, plan, model.Confirmation.given(plan), value)
    for line in applied.lines:
        write(line)
    write(f"next: add models — claude-multi discover {plan.provider_id} --add WIRE, or claude-multi models add "
          f"{plan.provider_id} WIRE …")
    return EXIT_READY


def _connect_preset(runtime: runtime_mod.Runtime, preset: str, *, input_stream: TextIO | None,
                    write: Callable[[str], None]) -> int:
    """A reviewed preset with an API key: its name, which key it uses when
    another provider already uses the preset's key (its own by default),
    the preview of where the key goes, the route approval, then the key
    (hidden; none when the saved key is shared, and replacing a shared key
    is confirmed first, naming every provider using it)."""

    from claude_multi.setup import model, providers, texts

    consent.require_human("providers add", runtime.gateway_environ())
    provider_id = _ask(texts.PRESET_NAME_PROMPT, input_stream, preset)
    plan = providers.plan_add_preset(runtime, preset, provider_id, None)
    if isinstance(plan, providers.EndpointPlan) and plan.preset_sharers:
        key = _preset_key(runtime, plan, input_stream=input_stream, write=write)
        if key != providers.KEY_OWN:
            plan = providers.plan_add_preset(runtime, preset, provider_id, None, key=key)
    for line in plan.lines:
        write(line)
    if not consent.confirm(texts.APPROVE_QUESTION, input_stream=input_stream):
        raise model.Declined()
    value = None
    key_use = getattr(plan, "key_use", providers.KEY_OWN)
    if key_use == providers.KEY_REPLACE:
        sharers = ", ".join(plan.shared_with)
        if not consent.confirm(texts.PRESET_KEY_REPLACE_QUESTION.format(name=plan.secret_name, providers=sharers),
                               input_stream=input_stream):
            raise model.Declined()
    if key_use != providers.KEY_REUSE:
        typed = consent.read_secret(texts.KEY_PROMPT.format(display=plan.provider_id), input_stream=input_stream)
        value = model.Secret(typed) if typed else None
    applied = providers.apply_add_preset(runtime, plan, model.Confirmation.given(plan), value)
    for line in applied.lines:
        write(line)
    if getattr(plan, "listing", None):
        write(f"next: add models — claude-multi discover {plan.provider_id} --add WIRE, or claude-multi models "
              f"add {plan.provider_id} WIRE …")
    else:
        write(f"next: add models — claude-multi models add {plan.provider_id} WIRE …")
    return EXIT_READY


def _preset_key(runtime: runtime_mod.Runtime, plan: Any, *, input_stream: TextIO | None,
                write: Callable[[str], None]) -> str:
    """Which key one more provider from a preset uses when another provider
    already uses the preset's key: 1 its own (the default), the saved key
    shared (offered when one is saved), or that key replaced."""

    from claude_multi.setup import model, providers, texts

    words = {"id": plan.provider_id, "own": plan.secret_name, "name": plan.preset_secret,
             "providers": ", ".join(plan.preset_sharers)}
    own, reuse, replace = (label.format(**words) for label in texts.PRESET_KEY_CHOICES)
    choices = [(own, providers.KEY_OWN)]
    if providers._key_present(runtime, plan.preset_secret):
        choices.append((reuse, providers.KEY_REUSE))
    choices.append((replace, providers.KEY_REPLACE))
    write(texts.PRESET_KEY_SHARED.format(**words))
    for number, (label, _key) in enumerate(choices, 1):
        write(texts.PRESET_KEY_ITEM.format(n=number, label=label))
    answer = _ask(texts.PRESET_KEY_QUESTION.format(id=plan.provider_id), input_stream, "1")
    if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
        raise model.Declined()
    return choices[int(answer) - 1][1]


def _connect_lan(runtime: runtime_mod.Runtime, preset: str, *, input_stream: TextIO | None,
                 write: Callable[[str], None]) -> int:
    from claude_multi.setup import model, providers

    provider_id = _ask("Name for this server", input_stream, "lan")
    url = _ask("Server address (http://host:port/v1)", input_stream)
    plan = providers.plan_add_preset(runtime, preset, provider_id, url or None)
    for line in plan.lines:
        write(line)
    if not consent.confirm("Declare it? [y/N] ", input_stream=input_stream):
        raise model.Declined()
    applied = providers.apply_add_preset(runtime, plan, model.Confirmation.given(plan))
    for line in applied.lines:
        write(line)
    return EXIT_READY


def _step_test(runtime: runtime_mod.Runtime, *, input_stream: TextIO | None, output_stream: TextIO,
               write: Callable[[str], None]) -> int:
    import claude_multi.cli.commands.providers as providers_cmd
    from claude_multi.setup import status, texts

    refusal = _in_session(runtime, "setup --step test")
    if refusal is not None:
        consent.prompt_stream().write(refusal + "\n")
        return EXIT_FAILED
    linked = [item.provider_id for item in status.connected(runtime)]
    if not linked:
        write(texts.TEST_NOTHING)
        return EXIT_FAILED
    if not consent.confirm(texts.SETUP_TEST_QUESTION, input_stream=input_stream):
        return EXIT_READY
    return providers_cmd._providers_test(runtime, _namespace(provider_ids=linked), input_stream=input_stream,
                                         output_stream=output_stream)


def _step_profile(runtime: runtime_mod.Runtime, *, input_stream: TextIO | None, write: Callable[[str], None],
                  **_: Any) -> int:
    from claude_multi import profile as profile_mod
    from claude_multi.setup import defaults, model, profiles as setup_profiles, status, texts

    if not status.connected(runtime):
        write(texts.SETUP_PROFILE_WAITING)
        return EXIT_FAILED
    choice = defaults.resolve_default(runtime)
    if choice.ready:
        write(texts.SETUP_PROFILE_FIT.format(name=choice.name, why=choice.reason_text))
        try:
            document = runtime.profiles.load(choice.name)
            evaluation = profile_mod.evaluate(document, runtime.lineup_catalog(),
                                              bindings=runtime.bindings.bindings(),
                                              effective=runtime.current_effective(), ad_hoc=False)
            if evaluation.lineup is not None:
                for note in defaults.spend_notes(runtime, evaluation.lineup):
                    write(f"! {note}")
        except (cli_errors.ClaudeMultiError, OSError):
            pass
        prompt = consent.prompt_stream()
        prompt.write(texts.SETUP_KEEP_PROFILE.format(name=choice.name))
        prompt.flush()
        answer = (consent._answer_stream(input_stream).readline() or "").strip().lower()
        if answer in ("", "y", "yes"):
            return EXIT_READY
        write("Other profiles: claude-multi profile list · make one the default: claude-multi profile default NAME")
        return EXIT_READY
    taken = set(runtime.profiles.names())
    name, n = "starter", 2
    while name in taken:
        name, n = f"starter-{n}", n + 1
    write(texts.SETUP_STARTER_INTRO)
    try:
        plan = setup_profiles.plan_new_starter(runtime, name)
    except model.Refused:
        write(texts.SETUP_STARTER_NO_LEAD)
        linked = [item.provider_id for item in status.connected(runtime)]
        for pid in status.without_models(runtime, linked):
            write(texts.NO_MODELS_NEXT.format(id=pid))
        return EXIT_FAILED
    for line in plan.lines:
        write(line)
    if not consent.confirm(texts.SETUP_SAVE_STARTER.format(name=name), input_stream=input_stream):
        raise model.Declined()
    setup_profiles.apply_new_starter(runtime, plan, model.Confirmation.given(plan))
    default = defaults.set_default_plan(runtime, name)
    defaults.apply_default(runtime, default, model.Confirmation.given(default))
    write(texts.SETUP_STARTER_SAVED.format(name=name))
    return EXIT_READY


def _step_check(runtime: runtime_mod.Runtime, *, write: Callable[[str], None], **_: Any) -> int:
    from claude_multi.setup import firstrun, texts

    items = firstrun.checks(runtime)
    for line in [texts.FIRSTRUN_HEADER, *firstrun.render_lines(items, surface="cli"),
                 *firstrun.info_lines(runtime), firstrun.summary(items)]:
        write(line)
    return EXIT_FAILED if firstrun.failing(items) else EXIT_READY


# ------------------------------------------------------------------ the key file


def _keys_file(runtime: runtime_mod.Runtime, path: Path, *, input_stream: TextIO | None,
               write: Callable[[str], None]) -> int:
    import claude_multi.cli.commands.providers as providers_cmd
    from claude_multi import secret_store
    from claude_multi.setup import model, providers, texts

    try:
        consent.require_human("setup --keys-file", runtime.environ)
        environ = runtime.gateway_environ()
        target = Path(path).expanduser() if str(path).startswith("~") else Path(path)
        if not target.is_absolute():
            target = Path.cwd() / target
        secret_store.check_key_file(target, environ)
        shown = paths.display(target, runtime.environ)
        if not consent.confirm(texts.KEYS_FILE_QUESTION.format(path=shown), input_stream=input_stream):
            raise model.Declined()
        applied = providers.select_key_file(runtime, target)
        for line in applied.lines:
            write(line)
        return providers_cmd.applied_exit(applied)
    except (model.SetupError, cli_errors.ClaudeMultiError, OSError) as exc:
        return _fail(write, exc)


# ------------------------------------------------------------------ answers


def _answers(runtime: runtime_mod.Runtime, path: Path, *, input_stream: TextIO | None,
             write: Callable[[str], None]) -> int:
    import claude_multi.cli.commands.providers as providers_cmd
    from claude_multi.setup import answers, model, texts

    try:
        # Every file the answers name is checked before anything is planned,
        # shown or written: an unusable one makes the whole file invalid.
        document = answers.load(path, runtime.gateway_environ())
        items = answers.build_items(runtime, document, progress=lambda line: write(f"… {line}"))
    except answers.AnswersError as exc:
        consent.prompt_stream().write(f"claude-multi setup: {termtext.visible_message(exc)}\n")
        return EXIT_USAGE
    except (model.SetupError, cli_errors.ClaudeMultiError) as exc:
        return _fail(write, exc)
    attended = consent.session_marker(runtime.environ) is None and consent.stdio_ttys()
    if attended:
        # The one confirmation covers exactly this preview: each item runs
        # only while its plan still shows what is listed here.
        write(texts.ANSWERS_PLAN_HEAD)
        for item in items:
            for line in item.lines:
                write(f"  {line}")
            if item.problem is not None:
                write("    " + texts.ANSWERS_CANNOT_RUN.format(reason=item.problem))
        if not consent.confirm(texts.ANSWERS_QUESTION, input_stream=input_stream):
            write("Nothing was written.")
            return EXIT_DECLINED
    done: list[model.Applied] = []
    skipped = failed = 0
    for item in items:
        if item.guarded and not attended:
            write(texts.ANSWERS_SKIPPED.format(id=item.subject, verb=item.verb or item.kind))
            skipped += 1
            continue
        try:
            result = item.run()
        except KeyboardInterrupt:
            raise
        except (model.SetupError, cli_errors.ClaudeMultiError, OSError) as exc:
            _fail(write, exc)
            failed += 1
            continue
        for line in result.lines:
            write(line)
        done.append(result)
    write(texts.ANSWERS_SUMMARY.format(applied=len(done), skipped=skipped, failed=failed))
    # A change the gateway did not accept the local key for is kept, but
    # the gateway is not ready: that is a failure too.
    return EXIT_READY if not (skipped or failed or providers_cmd.applied_exit(*done)) else EXIT_FAILED


# ------------------------------------------------------------------ the Claude step


def claude_step(
    runtime: runtime_mod.Runtime,
    *,
    claude_from=None,
    input_stream: TextIO | None,
    output_stream: TextIO,
) -> int:
    """Copy (or, after a yes, download) the pinned Claude Code; then prune.

    Exit 0 when the owned copy is in place; 3 when the download was offered
    and declined; 1 when it could not be offered (the human guard refused)
    or the step failed; 130 when cancelled (a partial download stays for
    the next run to resume).
    """

    def write(line: str) -> None:
        output_stream.write(f"{termtext.visible_text(line)}\n")
        output_stream.flush()

    try:
        return _claude_step(runtime, claude_from=claude_from, input_stream=input_stream, write=write)
    except KeyboardInterrupt:
        write(f"setup --step claude: cancelled — rerun `{pin.SETUP_COMMAND}` to finish "
              "(an interrupted download resumes where it stopped).")
        return EXIT_CANCELLED


def _claude_step(runtime: runtime_mod.Runtime, *, claude_from, input_stream: TextIO | None, write) -> int:
    contract = runtime.catalog.docs["native-contract"]
    environ = runtime.environ
    declined = False

    def ask(plan: acquire.DownloadPlan) -> bool:
        nonlocal declined
        try:
            consent.require_human(consent.SETUP_DOWNLOAD_VERB, environ)
        except consent.ConsentRefused as exc:
            write(str(exc))
            return False
        answer = consent.confirm(plan.text(environ), input_stream=input_stream)
        declined = not answer
        return answer

    try:
        outcome = acquire.acquire(
            contract, environ, claude_from=claude_from, consent=ask,
            progress=lambda line: write(f"… {line}"),
        )
    except (acquire.AcquireError, pin.PinError) as exc:
        write(f"claude-multi: setup --step claude: {exc}")
        return EXIT_FAILED
    for note in outcome.notes:
        write(note)
    if outcome.path is None:
        write(f"Claude Code {outcome.version} is not set up for claude-multi.")
        return EXIT_DECLINED if declined else EXIT_FAILED
    shown = paths.display(outcome.path, environ)
    if outcome.state == "present":
        write(f"Claude Code {outcome.version} is set up for claude-multi ({shown}).")
    elif outcome.state == "copied":
        write(f"Claude Code {outcome.version} copied from {outcome.source} to {shown} (size and sha256 verified).")
    else:
        write(f"Claude Code {outcome.version} downloaded to {shown} (size and sha256 verified).")
    for line in retention.prune_lines(retention.prune_copies(contract, environ), environ):
        write(line)
    return EXIT_READY
