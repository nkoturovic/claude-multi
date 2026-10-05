"""The ``custom`` command: the earlier custom-model registry (``custom.json``)."""

from __future__ import annotations

from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import operator as operator_mod
from claude_multi import termtext
from typing import TextIO
import argparse
import os
import sys
import claude_multi.cli.consent as consent
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _not_found(name: str) -> int:
    """A removal of an entry the registry does not hold: refused, exit 1."""

    sys.stderr.write(f"claude-multi: not found: {termtext.visible_text(name)}; nothing was removed\n")
    return 1


def _custom_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    output_stream: TextIO,
) -> int | None:
    """The ``custom`` command: list, add and remove registry entries."""

    command = args.custom_command
    if command != "list":
        marker = operator_mod.marker_path({**runtime.environ, "HOME": str(runtime.home)})
        if os.path.lexists(marker):
            # After migrate-custom the legacy registry is no longer read;
            # writing it would change nothing served.
            replacement = {
                "add-provider": "claude-multi providers add", "remove-provider": "claude-multi providers rm",
                "add-model": "claude-multi models add", "remove-model": "claude-multi models rm",
            }[command]
            raise cli_errors.CLIError(
                f"custom {command}: custom.json was migrated to providers.d and is no longer read — use {replacement}"
            )
    if command in ("add-provider", "add-model"):
        # A legacy add introduces an active model or a credential route,
        # so it passes the same human guard as its replacements (never an
        # in-session bypass), before any write.
        consent.require_human(f"custom {command}", runtime.environ)
    if command == "list":
        registry = custom.load_registry(runtime.environ)
        if not registry["providers"] and not registry["models"]:
            output_stream.write("(no custom providers or models registered)\n")
            return 0
        for provider_id, spec in sorted(registry["providers"].items()):
            output_stream.write(
                f"provider {termtext.visible_text(provider_id)}\t{spec['base_url']} · "
                f"{spec['auth_kind']} · env:{spec['secret_env']}\n"
            )
        for model_id, spec in sorted(registry["models"].items()):
            output_stream.write(
                f"model {termtext.visible_text(model_id)}\twire={spec['wire_model']} · "
                f"provider={spec['provider']} · context={spec['context_tokens']} · "
                f"{spec['created_via']}\n"
            )
        return 0
    if command == "add-provider":
        custom.add_provider(
            runtime.environ,
            args.name,
            base_url=args.base_url,
            auth_kind=args.auth,
            secret_env=args.secret_env,
            header=args.header,
            display=args.display,
            catalog_providers=runtime.catalog.providers,
        )
        output_stream.write(
            f"provider {args.name} registered — apply with "
            f"{cli_text.APPLY_GATEWAY_COMMAND}\n"
        )
        return 0
    if command == "remove-provider":
        removed, cascaded = custom.remove_provider(runtime.environ, args.name)
        if not removed:
            return _not_found(args.name)
        output_stream.write(f"Removed: {args.name} — apply with {cli_text.APPLY_GATEWAY_COMMAND}\n")
        if cascaded:
            output_stream.write(f"Removed custom models: {', '.join(cascaded)}\n")
        return 0
    if command == "add-model":
        custom.add_model(
            runtime.environ,
            args.name,
            wire_model=args.wire,
            provider=args.provider,
            context_tokens=args.context,
            display=args.display,
            created_via="manual",
            catalog_providers=runtime.catalog.providers,
            catalog_models=custom.catalog_line_ids(runtime.catalog.docs),
            retired_models=custom.retired_model_ids(runtime.catalog.docs),
        )
        output_stream.write(
            f"model {args.name} marked — apply with "
            f"{cli_text.APPLY_GATEWAY_COMMAND}\n"
        )
        return 0
    if command == "remove-model":
        removed = custom.remove_model(runtime.environ, args.name)
        if not removed:
            return _not_found(args.name)
        output_stream.write(f"Removed: {args.name} — apply with {cli_text.APPLY_GATEWAY_COMMAND}\n")
        return 0
