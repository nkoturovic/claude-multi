"""The ``quota`` command and ``/cm quota``.

Every surface names the typed condition (``quota.condition_line``; JSON
``quota-condition``). Exit 0 when the local gateway reported the accounts'
quota, whatever it says (available, stale or exhausted); 1 when no reading
was made (management-disabled: off or not reachable with its key, a restart
or the key fixes it, never another sign-in; unavailable: the read failed or
cannot run here; not-ours: the listener is not ours). ``/cm quota`` prints
the same text with status 0.
"""

from __future__ import annotations

from dataclasses import replace
import sys

from claude_multi import observations, quota, service, termtext
from typing import TextIO
import claude_multi.cli.gateway_facts as gateway_facts
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _backend(runtime: runtime_mod.Runtime) -> str:
    """The gateway backend that names where credential health is read."""

    return service.backend_of(runtime.home).name


def _cmd_quota(runtime: runtime_mod.Runtime, output_stream: TextIO, *, json_output: bool = False) -> int:
    pool = runtime.pool_status()
    now = quota._now()
    if json_output:
        report = quota.report(pool, now)
        condition = quota.condition(pool, now, pools=tuple(runtime.pool_providers()))
        report = replace(report, facts=report.facts + (
            observations.Fact("quota-condition", "source", "management", "known", condition, source="management",
                              classification="info" if condition == "available" else "attention"),))
        if runtime.gateway_attention:
            report = replace(report, diagnostics=report.diagnostics + (
                observations.Diagnostic("attention", "gateway-attention", "Gateway check needs attention; see stderr for details."),))
            for message in runtime.gateway_attention:
                print(f"Attention: {termtext.visible_message(message)}", file=sys.stderr)
        output_stream.write(report.json())
        return quota.CONDITION_EXIT[condition]
    code, lines = quota.command_lines(
        pool, provider_by_pool=runtime.pool_providers(), login_commands=gateway_facts._OAUTH_LOGIN_COMMANDS,
        restart_hint=gateway_facts.gateway_service_hint("restart"), now=now, backend=_backend(runtime),
    )
    for message in runtime.gateway_attention:
        print(f"Attention: {termtext.visible_message(message)}", file=output_stream)
    print("\n".join(lines), file=output_stream)
    return code


def session_quota_text(runtime: runtime_mod.Runtime) -> tuple[int, str]:
    """``/cm quota``: the same Runtime collector (its 60 s cache and no-retry
    rule) and the same formatter on the session surface; reads only, writes
    nothing."""

    pool = runtime.pool_status()
    code, lines = quota.command_lines(
        pool, provider_by_pool=runtime.pool_providers(), login_commands=gateway_facts._OAUTH_LOGIN_COMMANDS,
        restart_hint=gateway_facts.gateway_service_hint("restart"), now=quota._now(), surface="session",
        backend=_backend(runtime),
    )
    attention = [f"Attention: {termtext.visible_message(message)}" for message in runtime.gateway_attention]
    return code, "\n".join([*attention, *lines])
