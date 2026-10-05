"""Deterministic compilation of a resolved lineup into native launch form.

:func:`compile_lineup_launch` is the one compile path for every session:
one argv contract, the v2 process env, the lineup-independent lead appendix
and its content-addressed prompt path. Its only effect is the optional
one-shot ``git rev-parse`` of :func:`git_work_tree`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import catalog as catalog_mod
from . import errors
from . import lineup_files
from . import secret_store
from . import settings as settings_mod
from . import strict_json
from .composition import (
    AUTO_COMPACT_OUTPUT_RESERVE,
    AUTO_COMPACT_REACTIVE_HEADROOM,
    LEAD_ID,
    OPERATING_WINDOW_CEILING,
    operating_window,
)

if TYPE_CHECKING:  # avoid the runtime cycle: scope.py imports this module
    from . import profile
    from .scope import Fence, ScopePlan


class CompilerError(errors.ClaudeMultiError, ValueError):
    """Raised when compilation cannot proceed (fail closed)."""


# Launcher-owned structural flags. Aliases included.
_LONG_BLOCKED_BASE = frozenset(
    {
        "--model",
        "--effort",
        "--agent",
        "--agents",
        "--session-id",
        "--resume",
        "--fork-session",
        "--name",
        "--settings",
        "--disallowedTools",
        "--disallowed-tools",
        "--plugin-dir",
        "--plugin-url",
        "--fallback-model",
        "--continue",
        "--disable-slash-commands",
    }
)
# Contingency lead delivery (the only mode) owns the prompt flags.
_LONG_BLOCKED_CONTINGENCY = frozenset(
    {"--system-prompt", "--append-system-prompt", "--append-system-prompt-file"}
)

_FLAG_OWNERS = {
    "--model": "composition lead/model",
    "--effort": "composition lead effort",
    "--agent": "managed sessions keep the lead on the main thread",
    "--agents": "generated variant definitions",
    "--session-id": "launcher session identity",
    "--resume": "launcher session identity",
    "--fork-session": "launcher session identity",
    "--name": "launcher display metadata",
    "--settings": "trusted settings asset",
    "--disallowedTools": "compiled native-agent policy",
    "--disallowed-tools": "compiled native-agent policy",
    "--plugin-dir": "v2 has no generated plugin",
    "--plugin-url": "v2 has no generated plugin",
    "--fallback-model": "automatic fallback is forbidden",
    "--continue": "launcher per-CWD session memory",
    # Sessions started with --disable-slash-commands never watch agent
    # dirs — a silent durability loss for managed sessions.
    "--disable-slash-commands": "agent-directory watching is load-bearing for managed sessions",
    "--system-prompt": "contingency lead delivery",
    "--append-system-prompt": "contingency lead delivery",
    "--append-system-prompt-file": "contingency lead delivery",
}

# A passthrough flag that the skill-deny probe proves to
# defeat the managed skill deny (``scope.MANAGED_SKILL_DENIES``) is listed
# here with its surface, and the launch prints one stderr notice when it is
# passed; the launcher never blocks the flag without an operator decision.
# The probe at the pinned 2.1.281 proved no such surface: every permission mode
# (both bypass spellings, auto and the manual alias included) crossed with
# no CLI allow, ``--allowedTools Skill(code-review)`` and ``--allowedTools
# Skill`` (and the ``--allowed-tools``, ``--allow-dangerously-skip-permissions``
# surfaces) kept the deny. Re-run the probe at every client re-pin.
SKILL_POLICY_BYPASS_FLAGS: dict[str, str] = {}


def skill_policy_bypass_flags(argv: Iterable[str]) -> tuple[str, ...]:
    """The :data:`SKILL_POLICY_BYPASS_FLAGS` present in ``argv`` (``--flag`` or ``--flag=v``)."""

    found: list[str] = []
    for token in argv:
        flag = str(token).split("=", 1)[0]
        if flag in SKILL_POLICY_BYPASS_FLAGS and flag not in found:
            found.append(flag)
    return tuple(found)


FORK_UNVERIFIED_GUIDANCE = (
    "fork with --resume OLD --fork-session --session-id NEW is unverified for "
    "the pinned Claude CLI; fork natively (`claude --resume OLD "
    "--fork-session`) and adopt the resulting session with "
    "`claude-multi sessions link UUID` instead"
)


@dataclass(frozen=True)
class SessionAction:
    """Stable claude-multi identity plus the native UUID targeted by argv."""

    kind: str  # "fresh" | "resume"
    managed_id: str
    runtime_session_id: str

    @property
    def session_id(self) -> str:
        """Compatibility alias for scope/prompt identity during migration."""

        return self.managed_id


def build_fresh(
    managed_id: str, runtime_session_id: str | None = None
) -> SessionAction:
    return SessionAction(
        kind="fresh",
        managed_id=managed_id,
        runtime_session_id=runtime_session_id or managed_id,
    )


def build_resume(
    managed_id: str, runtime_session_id: str | None = None
) -> SessionAction:
    return SessionAction(
        kind="resume",
        managed_id=managed_id,
        runtime_session_id=runtime_session_id or managed_id,
    )


def compile_native_policy(
    policy: dict[str, str], generic_aliases: Iterable[str] = ()
) -> tuple[dict[str, str], list[str]]:
    """Compile built-in policy plus contract-recorded generic native aliases.

    Deny order is deterministic: built-ins (Explore, Plan, general-purpose)
    followed by the sorted unique contract generic aliases (e.g.
    ``Agent(claude)``). Recording an alias compiles a deny only; it asserts no
    functional verification beyond policy compilation.
    """

    env: dict[str, str] = {}
    denies: list[str] = []
    explore_native = policy["explore"] == "native"
    plan_native = policy["plan"] == "native"
    if not explore_native and not plan_native:
        env["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"] = "1"
    else:
        if not explore_native:
            denies.append("Agent(Explore)")
        if not plan_native:
            denies.append("Agent(Plan)")
    if policy["general_purpose"] == "off":
        denies.append("Agent(general-purpose)")
    for alias in sorted(set(generic_aliases)):
        deny = f"Agent({alias})"
        if deny not in denies:
            denies.append(deny)
    return env, denies


def validate_passthrough(args: list[str]) -> list[str]:
    """Reject launcher-owned structural flags; pass everything else unchanged."""

    blocked = set(_LONG_BLOCKED_BASE) | _LONG_BLOCKED_CONTINGENCY
    for token in args:
        if token.startswith("--"):
            flag = token.split("=", 1)[0]
            if flag in blocked:
                owner = _FLAG_OWNERS.get(flag, "launcher-owned")
                raise CompilerError(
                    f"passthrough argument {token!r} is launcher-owned ({owner}); "
                    "remove it or use the owning launcher feature"
                )
        elif token.startswith("-") and token != "-":
            if token == "-c" or token.startswith("-r") or token.startswith("-n"):
                raise CompilerError(
                    f"passthrough argument {token!r} is launcher-owned "
                    "(launcher session identity/display); use `claude-multi -c`, "
                    "`claude-multi -r UUID`, or the launcher --name equivalent"
                )
    return list(args)


FAMILY_DEFAULT_LINES = (
    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
    ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
)


def family_default_env(
    models: dict[str, Any], providers: dict[str, Any]
) -> dict[str, str]:
    """Anthropic family defaults from the family lines' current wire.

    Each of the ``opus``, ``fable`` and ``sonnet`` lines contributes its
    ``wire_model`` (``[1m]`` when the line is a 1M line) — the canonical
    first-party id, so the catalog line alone decides the generation (no
    route-name parsing, no route order). ``models`` is the v1 view, which
    omits ``status: new`` lines: an absent (or New · Off) line means the
    variable is omitted. A custom synthetic entry (``family: custom``) is
    never a family default. A family line on a non-Anthropic provider is a
    trusted-catalog defect and fails closed.

    Rationale: with a plain ``ANTHROPIC_BASE_URL`` the client resolves bare
    family aliases to the current generation, not an older one (observed
    on the pinned client). The variables stay for explicit pinning — the client's own alias
    table never decides which generation a bare ``opus``/``fable``/``sonnet``
    reaches — and, compiled into the scope's settings env, they survive the
    daemon's ``ANTHROPIC_*`` scrub on takeover.
    """

    env: dict[str, str] = {}
    for key, variable in FAMILY_DEFAULT_LINES:
        entry = models.get(key)
        if entry is None or entry.get("family") == "custom":
            continue
        provider = providers.get(entry["provider"])
        if provider is None or provider["adapter"] != "cliproxy-oauth-claude-v1":
            raise CompilerError(
                f"family default line {key!r} is not an Anthropic line"
            )
        suffix = "[1m]" if entry["context"]["client_tokens"] >= 1_000_000 else ""
        env[variable] = entry["wire_model"] + suffix
    return env


@dataclass(frozen=True)
class CompileResult:
    """Pure launch plan; effects (state writes, readiness, exec) happen later."""

    argv: list[str]
    env_set: dict[str, str]
    env_unset: tuple[str, ...]
    policy_env: dict[str, str]
    agents_json: str
    lead_prompt: str
    lead_prompt_path: Path
    session_action: SessionAction
    snapshot: dict[str, Any]
    composition_name: str
    durable: bool = False
    scope_plan: ScopePlan | None = None
    scope_dir: Path | None = None
    passthrough_add_dirs: tuple[str, ...] = ()
    write_lead_prompt: bool = True
    # The compiled split fence and the lineup generation (v2 path only).
    fence: Fence | None = None
    lineup_generation: int | None = None
    # What the launch did with the user's session_env_keep names (names only).
    env_keep: "EnvKeep | None" = None


def lead_prompt_path(
    state_root: Path, composition_hash: str, session_id: str
) -> Path:
    """Session-scoped lead prompt path: composition digest + exact session UUID.

    Deterministic per (composition, session): fresh launches with different
    UUIDs never overwrite each other's sentinel, resume of the same UUID is
    stable, and a composition transition for the same UUID lands on a new
    digest-named file. The launcher never prunes a prompt file, so a file
    that could belong to an active session is never removed.
    """

    digest = composition_hash.removeprefix("sha256:")[:16]
    return state_root / f"lead-prompt-{digest}-{session_id}.md"


def _extract_add_dirs(args: list[str]) -> tuple[str, ...]:
    """Passthrough ``--add-dir`` values (split and equals forms), in order."""

    add_dirs: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--add-dir":
            if index + 1 >= len(args):
                raise CompilerError("passthrough --add-dir requires a value")
            add_dirs.append(args[index + 1])
            index += 2
            continue
        if token.startswith("--add-dir="):
            add_dirs.append(token.split("=", 1)[1])
        index += 1
    return tuple(add_dirs)


def session_display_name(prefix: str, cwd: Path | str | None) -> str:
    """Session ``--name`` with the project basename appended.

    Sessions across projects shared one generic name in the Claude UI
    (``cm:<composition>`` / ``cg:<model>``); the basename makes them
    distinguishable while staying short and printable.
    """

    if cwd is None:
        return prefix
    base = "".join(ch for ch in Path(cwd).name if ch.isprintable()).strip()
    if not base:
        return prefix
    return f"{prefix}@{base[:24]}"


def _require_token_helper(plan: ScopePlan, command: str) -> None:
    """No launch may fall back to a credential inherited from process env."""

    if not command or plan.settings.get("apiKeyHelper") != command:
        raise CompilerError(
            "every launch plan must carry the scope apiKeyHelper "
            "(helper-only gateway auth)"
        )


# ------------------------------------------------------------------ v2
# The lineup-based compile path (the only one).


def reactive_trigger(window: int, percent: int) -> int:
    """The pinned client's reactive compact threshold at ``percent``.

    ``composition.auto_compact_trigger`` with the percentage as an input:
    ``reactive_trigger(w, 90) == auto_compact_trigger(w)``. Fails closed when
    the percent is outside 1-100 or the window cannot hold the output
    reserve plus the reactive headroom.
    """

    if not isinstance(percent, int) or isinstance(percent, bool) or not 1 <= percent <= 100:
        raise CompilerError(f"compaction percent {percent!r} is outside 1-100")
    if not isinstance(window, int) or isinstance(window, bool) or (
        window <= AUTO_COMPACT_OUTPUT_RESERVE + AUTO_COMPACT_REACTIVE_HEADROOM
    ):
        raise CompilerError(
            f"compaction window {window!r} is too small for the pinned client policy"
        )
    prompt_budget = window - AUTO_COMPACT_OUTPUT_RESERVE
    return min(prompt_budget * percent // 100, prompt_budget - AUTO_COMPACT_REACTIVE_HEADROOM)


@dataclass(frozen=True)
class LeadContext:
    """Process compaction facts of a lead set (one value per session)."""

    lead_class: str
    window: int
    trigger: int
    percent: int
    scalar: int | None
    min_provider_tokens: int
    min_client_tokens: int
    validated: bool
    custom_bound: bool = False

    def as_document(self) -> dict[str, Any]:
        """The ``lead-set.json`` ``context`` object."""

        return {
            "percent": self.percent,
            "scalar": self.scalar,
            "trigger": self.trigger,
            "window": self.window,
        }

    @property
    def policy(self) -> profile.WindowPolicy:
        """The window/percent policy every role's class is decided under."""

        from . import profile as profile_mod

        return profile_mod.WindowPolicy(window=self.window, percent=self.percent, scalar=self.scalar)


def lead_set_context(fence: Any, percent: int, *, ceiling: int = OPERATING_WINDOW_CEILING) -> LeadContext:
    """The session's compaction policy over the lead set.

    window = min(``ceiling``, the smallest provider bound); scalar = the
    smallest ``scalar_tokens`` (capped by the ceiling) only when every
    lead-set client window is below 1M (``profile.window_policy``, the one
    rule evaluation decides agent classes with); trigger =
    :func:`reactive_trigger` at ``percent`` (the profile override applied by
    the caller through ``settings.compaction_percent_for``, a Settings-level
    override only). ``ceiling`` is the effective window ceiling
    (``settings.Effective.window_ceiling``). Rows of one line repeat its
    values, so per-effort rows never change the result.
    """

    from . import profile as profile_mod

    rows = tuple(fence.lead_set)
    if not rows:
        raise CompilerError(f"lead set for class {fence.lead_class!r} is empty")
    min_provider = min(row.provider_tokens for row in rows)
    min_client = min(row.client_tokens for row in rows)
    policy = profile_mod.window_policy(
        ((row.provider_tokens, row.client_tokens, row.scalar_tokens) for row in rows), percent, ceiling=ceiling
    )
    window = policy.window
    return LeadContext(
        lead_class=fence.lead_class,
        window=window,
        trigger=reactive_trigger(window, percent),
        percent=percent,
        scalar=policy.scalar,
        min_provider_tokens=min_provider,
        min_client_tokens=min_client,
        validated=all(
            row.source != "custom" and row.validated_tokens >= row.provider_tokens
            for row in rows
        ),
        custom_bound=any(row.source == "custom" for row in rows),
    )


def agent_context_gaps(lineup: profile.ResolvedLineup, window: int) -> tuple[str, ...]:
    """Bound agents whose provider bound undercuts the process window.

    An agent whose ``provider_context_tokens`` is below both its client
    window and the session's process compaction ``window`` would be asked to
    hold more context than its route accepts (agents never shrink the lead
    window). An agent whose 1M class was narrowed to the 200K class
    (``profile.role_window``) is no gap unless its bound is below that
    class too. Returns the offending agent ids in lineup order; unbound
    lines are never checked. Also the live-apply re-check (with
    ``lead-set.json``'s ``context.window``).
    """

    return tuple(
        rid
        for rid, agent in lineup.agents.items()
        if agent.binding.provider_context_tokens < agent.binding.client_context_tokens
        and agent.binding.provider_context_tokens < window
    )


def lineup_session_name(lineup: profile.ResolvedLineup, cwd: Path | str | None) -> str:
    """``--name`` of a v2 session.

    ``cm:<profile name>``, or ``cm:direct:<lead key>`` for an ad-hoc direct
    lineup (``lineup.name is None``), plus the legacy ``@<project>`` suffix.
    """

    prefix = (
        f"cm:{lineup.name}" if lineup.name is not None else f"cm:direct:{lineup.lead.binding.key}"
    )
    return session_display_name(prefix, cwd)


def git_work_tree(cwd: Path | str) -> bool:
    """True when ``cwd`` is inside a git work tree.

    One ``git rev-parse --is-inside-work-tree`` (read-only, 5 s bound). Any
    failure — no git, not a repository, a bare repository, a timeout — is
    False: writer grades need worktree isolation, which needs a work tree.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(cwd), "rev-parse", "--is-inside-work-tree"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == b"true"


def workflow_default_line(selector: str, native_agents: Mapping[str, str]) -> str:
    """The lineup.md / lead-appendix line for Settings ``workflow_default_binding``.

    Pinned by the SettingsEnvPlacementProbe case (iii) on the pinned
    client: ``CLAUDE_CODE_SUBAGENT_MODEL`` in flag settings env
    becomes the model of a workflow ``agent()`` without an agentType
    (an observed client fact) and of native general-purpose spawns; native Explore
    (inherit cap on or off) and Plan keep their own model rule, and cm-*
    agents keep their frontmatter binding.
    """

    general = native_agents.get("general_purpose") != "off"
    who = "Workflow agents without a cm-* agentType" + (
        " and native general-purpose agents" if general else ""
    )
    return (
        f"- {who} run on `{selector}` (Settings workflow_default_binding); "
        "native Explore and Plan are not affected."
    )


def generate_session_appendix(
    lineup: profile.ResolvedLineup,
    context: LeadContext,
    eff: settings_mod.Effective,
    *,
    managed_id: str,
    lineup_path: str,
    no_subagents: bool,
    workflow_default: str | None = None,
) -> str:
    """The v2 cm-lead appendix.

    Lineup-independent: it reads only relaunch-only inputs — the
    lead class (through ``context``), ``native_agents``, ``workflows``,
    ``no_subagents``, Settings (the Explore inherit cap, the workflow
    default selector) — plus the managed id and the absolute ``lineup.md``
    path. Bindings, the profile name and the lead model never appear, so a
    live lineup apply keeps the prompt bytes and path.
    """

    policy = lineup.native_agents
    workflows_off = lineup.workflows == "off" or no_subagents
    lines: list[str] = [
        "## Session lineup (generated)",
        "",
        "- Your agent lineup — every bound `cm-*` agent with its model and effort, the "
        "review routing table and the review round cap — is in "
        f"`{lineup_path}` and reaches you as the lineup notice. Read that file whenever "
        "no lineup notice is in your context. Agent descriptions never name a model.",
        # Lineup-independent, so it names the family instead of
        # reading the bindings.
        "- A quiet Sonnet agent is not necessarily hung: Sonnet can move its progress "
        "notes into thinking whose summary is omitted, so a productive agent may show no "
        "text for a while. For a Sonnet agent, silence alone is neither a death nor a "
        "rate-limited provider (this qualifies the silence clause under Failure handling): "
        "recover or reroute it only on actual failure evidence (an API error, a 429, a "
        "crash or a timeout), and otherwise wait for its result.",
        "",
        "## Native-agent policy (generated)",
        "",
    ]
    explore = policy["explore"]
    if explore == "replace":
        lines.append("- Explore: replaced by `cm-explorer` (native Explore denied).")
    elif explore == "native":
        if eff.explore_inherit_cap_disabled:
            lines.append("- Explore: native, inheriting your model.")
        else:
            # Measured on 2.1.286 (S9): the client caps Explore
            # only under a Fable lead; GPT and the other Anthropic leads
            # inherit whether or not the cap is disabled.
            lines.append(
                "- Explore: native, inheriting your model; under a Fable lead the client "
                "caps it to the Opus family default."
            )
    else:
        lines.append("- Explore: disabled without replacement.")
    lines.append(f"- Plan: {policy['plan']}.")
    lines.append(f"- general-purpose: {policy['general_purpose']}.")
    if workflows_off:
        lines.append("- Workflows: off (the Workflow tool is disabled).")
    else:
        lines.append("- Workflows: native.")
    if workflow_default is not None:
        # The text follows the SettingsEnvPlacementProbe case (iii). Same
        # line as lineup.md.
        lines.append(workflow_default_line(workflow_default, policy))
    if no_subagents:
        # --no-subagents also turns workflows off.
        lines.append(
            "- Delegation: disabled for this session (`--no-subagents`: the Agent tool "
            "is denied and workflows are off)."
        )
    lines += [
        "",
        "## Context policy (generated)",
        "",
        f"- Lead class `{context.lead_class}`: process compaction window {context.window} "
        f"tokens; deterministic reactive trigger {context.trigger} ({context.percent}% of "
        "the prompt budget). Proactive summary preparation is runtime-controlled and may "
        "occur earlier. These numbers hold for every model in your lead set.",
    ]
    if context.window < context.min_provider_tokens:
        lines.append(
            "- Operating ceiling: the process window is capped below the lead set's "
            "smallest provider bound by local operating policy; it is not a "
            "route-capability claim."
        )
    if context.scalar is not None:
        lines.append(
            f"- Process scalar: CLAUDE_CODE_MAX_CONTEXT_TOKENS={context.scalar} is exported "
            "for this lead class."
        )
    if context.custom_bound:
        lines.append(
            "- Context qualification: the lead set includes an operator-declared custom "
            "model (custom.json); its provider bound is user-attested, not benchmark-verified."
        )
    elif not context.validated:
        lines.append(
            "- Context qualification: at least one lead-set provider bound is not "
            "near-limit benchmark-verified; it follows explicit route or operator "
            "attestation."
        )
    native_on = not no_subagents and (
        explore == "native" or policy["plan"] == "native" or policy["general_purpose"] == "on"
    )
    if context.min_client_tokens < 1_000_000 and native_on:
        lines.append(
            "- Native agents inherit this lower-context lead and the process-wide "
            "compaction window. Keep delegated prompts and loaded skills bounded; an "
            "enabled 1M cm-* selector does not raise this process window."
        )
    lines += [
        "",
        "## Standing rules (generated)",
        "",
        "- One writer owns an overlapping file scope at a time.",
        "- Invoke `cm-*` agents by exact id; never pass a per-invocation model override.",
        "- `/model` switches only within your lead set; tell the user to press `s` (this "
        "session only) so the choice is not saved into their global settings.",
        "",
        "## Session sentinel (generated)",
        "",
        f"- Managed session: {managed_id}.",
        "- If a selected cm-* type is unavailable, stop delegation. Never substitute a "
        "native or generic agent.",
        f"- Exact relaunch after interruption: ask the user to run `claude-multi -r "
        f"{managed_id}`.",
        "",
    ]
    return "\n".join(lines)


# Process env keys every v2 launch clears before applying its sets (§3.10).
# Moved keys (family defaults and the Explore inherit cap live in the
# flag settings env) are unset so an inherited value never leaks when the
# settings omit them; CLAUDE_CODE_SUBAGENT_MODEL(_FORCE) stay unset (the
# compiled workflow default lives only in flag settings).
V2_ENV_UNSET = (
    *catalog_mod.CREDENTIAL_ENV_KEYS,
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
    "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS",
    "CLAUDE_CODE_DISABLE_WORKFLOWS",
    "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP",
)

# Policy-defeating keys (also in catalog.RESERVED_LEAD_ENV_KEYS).
POLICY_DEFEATING_ENV_KEYS = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "DISABLE_AUTO_COMPACT",
    "DISABLE_COMPACT",
    "MAX_THINKING_TOKENS",
    "CLAUDE_CODE_DISABLE_THINKING",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_COORDINATOR_FORCE_WORKER_INHERIT_MODEL",
)

# Gateway-only secrets never enter a managed or direct session.
GATEWAY_SECRET_ENV_KEYS = ("MANAGEMENT_PASSWORD",)

# Never unset these, whatever a provider secret_ref or
# the launch env's ``*_API_KEY`` scan names (CLAUDE_CODE_MESSAGING_TOKEN and
# the secret-file path CLAUDE_MULTI_SECRET_ENV). Owned by secret_store's
# shared credential policy; the same object.
ENV_UNSET_KEEP = secret_store.ENV_UNSET_KEEP
_API_KEY_SUFFIX = "_API_KEY"


def v2_env_unset(
    *,
    scalar: int | None,
    secret_env_names: Iterable[str],
    launch_environ: Mapping[str, str] | None = None,
    env_keep: Iterable[str] = (),
) -> tuple[str, ...]:
    """The v2 process ``env_unset`` keeps provider secrets out of sessions.

    :data:`V2_ENV_UNSET` (+ ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` when no scalar
    is exported), then :data:`POLICY_DEFEATING_ENV_KEYS`, gateway secret keys
    (including inherited case variants), then every provider
    ``secret_ref`` env name of the merged provider set (catalog ∪ custom,
    ``scope.line_view``; sorted), then every ``*_API_KEY`` present in
    ``launch_environ`` (sorted) except the ``env_keep`` names (the user's
    checked ``session_env_keep``: they leave only that generic scan, never
    another set). :data:`ENV_UNSET_KEEP` is never listed; each name appears
    once, first position wins. A managed session therefore never inherits a
    provider credential (the gateway render reads secrets from the secret
    file, never the process env).
    """

    names: list[str] = [*V2_ENV_UNSET]
    if scalar is None:
        names.append("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    names += POLICY_DEFEATING_ENV_KEYS
    names += GATEWAY_SECRET_ENV_KEYS
    if launch_environ is not None:
        names += sorted(key for key in launch_environ if key.upper() in GATEWAY_SECRET_ENV_KEYS)
    names += sorted(set(secret_env_names))
    if launch_environ is not None:
        kept = frozenset(env_keep)
        names += sorted(key for key in launch_environ if key.endswith(_API_KEY_SUFFIX) and key not in kept)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen or name in ENV_UNSET_KEEP:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered)


@dataclass(frozen=True)
class EnvKeep:
    """What one launch does with the user's ``session_env_keep`` names
    (names only, never a value): ``kept`` are present in the launch
    environment and reach the session; ``stripped`` every ``*_API_KEY`` of
    the launch environment the session does not get; ``refused`` the kept
    names this launch strips anyway, each with its reason (checked again at
    every launch and resume against the current providers)."""

    kept: tuple[str, ...] = ()
    stripped: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()


def credential_env_names(secret_env_names: Iterable[str]) -> frozenset[str]:
    """Every provider API-key name a keep entry may never take: the merged
    providers' ``env:`` references and the reviewed transport alternatives'
    key names."""

    from . import operator as operator_mod

    alternatives = {alternative.secret_name for alternative in operator_mod.TRANSPORT_ALTERNATIVES.values()}
    return frozenset(secret_env_names) | frozenset(alternatives)


def checked_env_keep(env_keep: Iterable[str], *, credential_names: Iterable[str]
                     ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """``(usable names, (name, reason) refused)`` of a keep list, checked
    with ``secret_store.env_keep_problem`` against ``credential_names``. A
    refused entry that looks like a key value is named by
    ``secret_store.shown_env_name`` (never itself), so no display of the
    refusals can echo it."""

    credentials = frozenset(credential_names)
    usable: list[str] = []
    refused: list[tuple[str, str]] = []
    for name in dict.fromkeys(env_keep):
        problem = secret_store.env_keep_problem(name, credential_names=credentials)
        if problem is None:
            usable.append(name)
        else:
            refused.append((secret_store.shown_env_name(name), problem))
    return tuple(usable), tuple(refused)


def env_keep_report(env_unset: Iterable[str], usable: Iterable[str], refused: Iterable[tuple[str, str]],
                    launch_environ: Mapping[str, str] | None) -> EnvKeep:
    """The names-only :class:`EnvKeep` of a computed ``env_unset``."""

    environ = launch_environ or {}
    unset = frozenset(env_unset)
    return EnvKeep(
        kept=tuple(sorted(name for name in usable if name in environ and name not in unset)),
        stripped=tuple(sorted(name for name in environ if name.endswith(_API_KEY_SUFFIX) and name in unset)),
        refused=tuple(refused),
    )


PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def proxy_effective(environ: Mapping[str, str]) -> bool:
    return any(value for key, value in environ.items()
               if key.lower() in ("https_proxy", "http_proxy", "all_proxy"))


def loopback_no_proxy(
    launch_environ: Mapping[str, str], *settings_envs: Mapping[str, str],
    previous: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Durable bypass: loopback first, then all existing entries in layer order.

    Only proxy presence is examined; never copy a proxy URL into a scope.
    An already compiled bypass is sticky, even without the original shell.
    """
    layers = (launch_environ, *settings_envs, previous or {})
    if not any(proxy_effective(layer) for layer in layers) and not any(
        (previous or {}).get(key) for key in ("NO_PROXY", "no_proxy")
    ):
        return {}
    hosts = list(LOOPBACK_HOSTS)
    for layer in layers:
        for key in ("NO_PROXY", "no_proxy"):
            hosts.extend(host.strip() for host in layer.get(key, "").split(",") if host.strip())
    value = ",".join(dict.fromkeys(hosts))
    return {"NO_PROXY": value, "no_proxy": value}


def compile_lineup_launch(
    *,
    docs: Mapping[str, Any],
    prompt_bodies: Mapping[str, bytes],
    lineup: profile.ResolvedLineup,
    effective: settings_mod.Effective,
    session_action: SessionAction,
    lineup_generation: int,
    state_root: Path,
    scope_dir: Path,
    hook_command: str,
    token_helper_command: str,
    launch_epoch: int = 0,
    passthrough: Sequence[str] = (),
    no_subagents: bool = False,
    session_cwd: Path | str | None = None,
    launch_environ: Mapping[str, str] | None = None,
    worktree_available: bool | None = None,
    proxy_env: Mapping[str, str] | None = None,
    feedback_drafts: str = settings_mod.FEEDBACK_DRAFTS_DEFAULT,
    agent_gate: Any = None,
    default_mode: str | None = None,
    env_keep: Sequence[str] = (),
) -> CompileResult:
    """The one v2 launch plan for every session, managed or direct.

    ``agent_gate`` is the ``profile.AgentGate`` the lineup was
    evaluated with; the fence admits T2 selectors only for bound agents and
    an eligible operator workflow default.

    Pure except the git seam: with ``worktree_available=None`` and
    a ``session_cwd``, one :func:`git_work_tree` probe decides whether
    ``lineup.md`` marks the writer grades unavailable (``None`` cwd: assumed
    available). ``docs`` is a loaded or ``custom.merge_docs`` docs map
    (callers pass ``Runtime.ordinary_docs``); only v2 data is read.
    ``lineup`` must be resolved with the same ``effective``; the compaction
    percent is the profile override when present, else Settings.
    ``launch_environ`` is the environment the session will inherit: its
    ``*_API_KEY`` names join ``env_unset``. Fails closed
    (``CompilerError``/``ScopeError``) on every gap. ``default_mode`` is the
    launch's permission-mode decision (``scope.PERMISSION_DEFAULT_MODE``
    when no settings layer configures one, else None).
    """

    from . import profile as profile_mod
    from . import scope as scope_mod

    safe_args = validate_passthrough(list(passthrough))
    add_dirs = _extract_add_dirs(safe_args)
    if scope_dir is None:
        raise CompilerError("a v2 launch requires a scope directory")
    if not hook_command:
        raise CompilerError("a v2 launch requires the protocol-3 lifecycle hook command")

    cat = profile_mod.LineupCatalog.from_docs(docs, agent_gate=agent_gate)
    meta = scope_mod.catalog_meta_v2(docs)
    view = scope_mod.line_view(cat, effective)
    fence = scope_mod.compile_fence(lineup, cat, effective)
    try:
        percent = settings_mod.compaction_percent_for(effective, lineup.settings_overrides)
    except settings_mod.SettingsError as exc:
        raise CompilerError(str(exc)) from exc
    context = lead_set_context(fence, percent, ceiling=profile_mod.window_ceiling(effective))
    if lineup.policy is not None and lineup.policy.window != context.window:
        # The agents' classes were decided for another window than the one
        # this launch exports: their agent files would disagree with the
        # process (fail closed; evaluate and compile with the same inputs).
        raise CompilerError(
            f"the lineup was resolved for a session window of {lineup.policy.window} tokens, "
            f"but this launch compiles {context.window}; resolve it again with the same Settings"
        )
    gaps = agent_context_gaps(lineup, context.window)
    if gaps:
        detail = ", ".join(
            f"{rid} (provider bound {lineup.agents[rid].binding.provider_context_tokens} "
            f"< client window {lineup.agents[rid].binding.client_context_tokens})"
            for rid in gaps
        )
        raise CompilerError(
            f"bound agents cannot hold the session's process window {context.window}: "
            f"{detail}; bind another model or relaunch with a smaller lead class"
        )

    if worktree_available is None:
        worktree_available = True if session_cwd is None else git_work_tree(session_cwd)
    bypass = loopback_no_proxy(launch_environ or {}, previous=proxy_env)
    plan = scope_mod.compile_lineup_scope(
        lineup,
        cat,
        effective,
        prompt_bodies,
        meta,
        lineup_generation=lineup_generation,
        managed_id=session_action.managed_id,
        hook_command=hook_command,
        launch_epoch=launch_epoch,
        token_helper_command=token_helper_command,
        no_subagents=no_subagents,
        worktree_available=worktree_available,
        secret_denies=scope_mod.secret_path_denies(launch_environ or {}),
        proxy_env=bypass,
        feedback_drafts=feedback_drafts,
        default_mode=default_mode,
    )
    _require_token_helper(plan, token_helper_command)

    policy_env, _denies = compile_native_policy(
        dict(lineup.native_agents), meta.generic_agent_aliases
    )
    for key in lineup.lead.env:
        if key in catalog_mod.RESERVED_LEAD_ENV_KEYS:
            raise CompilerError(f"lead environment key {key!r} is compiler-owned and reserved")
    env_set: dict[str, str] = {
        "ANTHROPIC_BASE_URL": meta.gateway_base_url,
        "CLAUDE_MULTI_GATEWAY": "1",
        # Updater hygiene: non-load-bearing; no shared-daemon claim.
        "DISABLE_AUTOUPDATER": "1",
        # The pinned client honours it: no update of any kind (background or
        # an in-session update) from a managed session, so the owned copy
        # never touches the user's own install.
        "DISABLE_UPDATES": "1",
        # No fast-mode availability prefetch (it sends
        # the helper's gateway key to api.anthropic.com) and no cached
        # fast-mode speed on managed requests; reserved against lead.env.
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    }
    if context.scalar is not None:
        env_set["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context.scalar)
    env_set.update(lineup.lead.env)
    env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context.window)
    env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(context.percent)
    env_set.update(policy_env)
    env_set.update(bypass)
    if no_subagents:
        # Belt to the scope's Agent deny (as the earlier direct launch did).
        env_set["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"] = "1"
    env_set["CLAUDE_MULTI_MANAGED_ID"] = session_action.managed_id
    env_set["CLAUDE_MULTI_SESSION_ID"] = session_action.managed_id
    env_set["CLAUDE_MULTI_LAUNCH_EPOCH"] = str(launch_epoch)
    keep_usable, keep_refused = checked_env_keep(
        env_keep, credential_names=credential_env_names(view.secret_env_names))
    env_unset = v2_env_unset(
        scalar=context.scalar,
        secret_env_names=view.secret_env_names,
        launch_environ=launch_environ,
        env_keep=keep_usable,
    )

    appendix = generate_session_appendix(
        lineup,
        context,
        effective,
        managed_id=session_action.managed_id,
        lineup_path=str(Path(scope_dir) / lineup_files.LINEUP_MD),
        no_subagents=no_subagents,
        workflow_default=scope_mod.workflow_default_selector(cat, effective, policy=context.policy),
    )
    lead_body = prompt_bodies.get(LEAD_ID)
    if lead_body is None:
        raise CompilerError("the catalog has no cm-lead prompt body")
    lead_prompt = bytes(lead_body).decode("utf-8") + "\n" + appendix
    prompt_path = lead_prompt_path(
        Path(state_root),
        "sha256:" + strict_json.sha256_hex(lead_prompt.encode("utf-8")),
        session_action.managed_id,
    )

    argv: list[str] = []
    if session_action.kind == "fresh":
        argv += ["--session-id", session_action.runtime_session_id]
    elif session_action.kind == "resume":
        argv += ["--resume", session_action.runtime_session_id]
    else:
        raise CompilerError(f"unknown session action {session_action.kind!r}")
    argv += [
        "--name",
        lineup_session_name(lineup, session_cwd),
        "--settings",
        str(Path(scope_dir) / "settings.json"),
        "--model",
        lineup.lead.binding.selector,
        "--effort",
        lineup.lead.session_effort,
        "--add-dir",
        str(scope_dir),
        "--append-system-prompt-file",
        str(prompt_path),
    ]
    argv += safe_args

    return CompileResult(
        argv=argv,
        env_set=env_set,
        env_unset=env_unset,
        policy_env=policy_env,
        agents_json="",
        lead_prompt=lead_prompt,
        lead_prompt_path=prompt_path,
        session_action=session_action,
        snapshot={"applied": lineup.applied_bindings(), "lineup_generation": lineup_generation},
        composition_name=lineup.name or "",
        durable=True,
        scope_plan=plan,
        scope_dir=Path(scope_dir),
        passthrough_add_dirs=add_dirs,
        write_lead_prompt=True,
        fence=fence,
        lineup_generation=lineup_generation,
        env_keep=env_keep_report(env_unset, keep_usable, keep_refused, launch_environ),
    )
