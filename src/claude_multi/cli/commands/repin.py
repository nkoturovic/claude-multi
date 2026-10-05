"""``claude-multi-dev repin``: the maintainer's evidence-gated Claude Code re-pin.

The flow itself lives in ``claude_multi.upgrade``. This module turns the dev
flags into a run: the checkout (``--repo``, default: the checkout this
command runs from), ``--manifest-dir`` for offline verification and
``--settings-keys`` for a reviewed settings-key list (when the keys cannot
be read from the candidate). The new pin reaches users with the next
release built from the checkout; nothing here builds or installs.
"""

from __future__ import annotations

from claude_multi import errors as cli_errors
from claude_multi import pin
from claude_multi import sessions
from claude_multi import termtext
from pathlib import Path
from typing import Any, Mapping, TextIO
import os
import sys
import claude_multi.cli.consent as consent


USAGE = "usage: claude-multi-dev repin [--repo PATH] [--manifest-dir DIR] [--settings-keys FILE]"


def repin(
    flags: Mapping[str, Any], *, environ: Mapping[str, str] | None = None,
    output_stream: TextIO | None = None, input_stream: TextIO | None = None,
) -> int:
    """Run the re-pin; returns the process exit code."""

    from claude_multi import catalog, dev, layout, upgrade as upgrade_mod

    env = dict(os.environ if environ is None else environ)
    out = sys.stdout if output_stream is None else output_stream
    unknown = sorted(set(flags) - {"repo", "manifest-dir", "settings-keys"})
    if unknown:
        raise cli_errors.CLIError(f"unknown repin option --{unknown[0]}; {USAGE}")
    for name in ("repo", "manifest-dir", "settings-keys"):
        if flags.get(name) is True:
            raise cli_errors.CLIError(f"--{name} needs a value; {USAGE}")
    settings_keys = None
    if "settings-keys" in flags:
        try:
            settings_keys = upgrade_mod.read_settings_keys_file(Path(flags["settings-keys"]))
        except upgrade_mod.UpgradeError as exc:
            raise cli_errors.CLIError(str(exc)) from exc
    checkout = dev.verify_repo(Path(flags["repo"]) if "repo" in flags else upgrade_mod.default_checkout())
    resources = layout.checkout_resources(checkout)
    contract = catalog.load_raw(resources)["docs"]["native-contract"]

    def ask_fetch(request_plan: Any) -> bool:
        return consent.confirm(request_plan.text(), input_stream=input_stream)

    def progress(line: str) -> None:
        out.write(f"… {termtext.visible_text(line)}\n")
        out.flush()

    manifest_dir = flags.get("manifest-dir")
    try:
        outcome = upgrade_mod.run_repin(
            checkout_root=checkout,
            native_contract=contract,
            today=sessions._now()[:10],
            env=env,
            manifest_dir=Path(manifest_dir) if manifest_dir else None,
            progress=progress,
            fetch_consent=consent.update_fetch_consent(env, ask_fetch),
            settings_keys=settings_keys,
        )
    except upgrade_mod.UpgradeError as exc:
        raise cli_errors.CLIError(str(exc)) from exc
    except KeyboardInterrupt:
        raise cli_errors.CLIError(
            f"repin interrupted (Ctrl-C) — check `git -C {checkout} status` and the pinned "
            f"Claude Code {pin.version(contract)}"
        ) from None
    for line in outcome.messages:
        out.write(f"{termtext.visible_text(line)}\n")
    return 0
