"""``claude-multi plan [--assets PATH] [--json]``.

Read-only: it runs on the read-only Runtime branch (no shim refresh, no
state write, no lock, no plan file) and never contacts a provider or the
gateway. The before side is the published render (``config.yaml`` parsed
by the inverse of ``emit_yaml`` plus ``continuity.json``), never a
re-render. The after side is what a render of the current declarations —
with ``--assets``, of a candidate package's assets — would publish.
``--assets`` selects inputs only: the running launcher's identity (its
version, asset root and shims) is never changed.

The printed digest binds the inputs and the diff: an update procedure reruns
``plan`` immediately before the approved switch and requires the same
digest (no launches in between). :func:`plan_application` is the seam a
future installer calls with the plan it showed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from claude_multi import __version__
from claude_multi import catalog as catalog_mod
from claude_multi import continuity as continuity_mod
from claude_multi import errors as cli_errors
from claude_multi import layout
from claude_multi import proxy as proxy_mod
from claude_multi import served_plan
from claude_multi import strict_json
from claude_multi import termtext

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


PLAN_SCHEMA_VERSION = 1


class PlanError(cli_errors.CLIError):
    """A plan input that cannot be read (exit 2)."""


def candidate_asset_root(path: Path | str) -> Path:
    """The resources of ``--assets PATH``: an installation prefix (the
    package in ``lib/pythonX.Y/site-packages``), a source tree holding
    ``src/claude_multi``, or a resource directory itself (``catalog/`` and
    ``version.json``)."""

    resources = layout.installation_resources(path)
    if resources is None:
        raise PlanError(f"plan --assets {path}: not a claude-multi package or asset root "
                        "(no catalog/ and version.json)")
    return resources


@dataclass(frozen=True)
class CandidateIdentity:
    asset_root: str
    launcher_version: str
    catalog_version: int
    bundle_sha256: str

    def as_document(self) -> dict[str, Any]:
        return {"asset_root": self.asset_root, "launcher_version": self.launcher_version,
                "catalog_version": self.catalog_version, "bundle_sha256": self.bundle_sha256}


def candidate_identity(asset_root: Path) -> CandidateIdentity:
    try:
        version = strict_json.loads((asset_root / "version.json").read_bytes())
        bundle = catalog_mod.load_catalog(asset_root)
    except (OSError, ValueError, cli_errors.ClaudeMultiError) as exc:
        raise PlanError(f"plan: candidate assets unreadable ({termtext.visible_message(exc)})") from exc
    return CandidateIdentity(str(asset_root), str(version.get("launcher_version")),
                             int(bundle.docs["version"]["catalog_version"]), bundle.bundle_sha256)


def build(runtime: runtime_mod.Runtime, assets: Path | None = None) -> tuple[served_plan.ServedChangePlan, CandidateIdentity]:
    """The read-only plan of the declared (or candidate) render against the
    published one, for this runtime's state root."""

    asset_root = candidate_asset_root(assets) if assets is not None else Path(runtime.asset_root)
    identity = candidate_identity(asset_root)
    published = proxy_mod.published_identity(runtime.home)
    document = proxy_mod.candidate_document(
        runtime.home, environ=runtime.gateway_environ(), asset_root=asset_root,
        state_root=runtime.session_store.root,
    )
    scan = continuity_mod.scan_records(runtime.session_store.root)
    notes = []
    managed, unreadable = proxy_mod.root_authority(runtime.home)
    if unreadable is not None:
        notes.append("Root: " + served_plan.ROOT_UNREADABLE_REFUSAL.format(
            reason=unreadable, requested=runtime.session_store.root).splitlines()[0])
    elif served_plan.root_refusal(managed, str(runtime.session_store.root)) is not None:
        notes.append("Root: " + served_plan.ROOT_BLOCK.format(
            managed=managed, requested=runtime.session_store.root))
    plan = served_plan.build_plan(
        published.routes, served_plan.routes_from_document(document),
        references=served_plan.references_from_scan(scan), state_root=str(runtime.session_store.root),
        gateway=published.gateway if published.routes is not None else served_plan.gateway_label(document),
        before_unknown=published.unknown,
        inputs={"candidate": identity.as_document(), "managed_root": managed,
                **({"managed_root_unreadable": unreadable} if unreadable is not None else {})},
        notes=notes,
    )
    return plan, identity


def application_refusal(runtime: runtime_mod.Runtime, plan: served_plan.ServedChangePlan) -> str | None:
    """Why a plan must not be applied (None: it may): a transaction owner
    holds the state root's gateway inhibition (only that owner, its token in
    the environment, applies), another root holds the gateway's authority,
    or a removal/retarget has unknown live impact. A read-only
    ``plan`` still shows such a plan."""

    from claude_multi import gateway_inhibition

    record = gateway_inhibition.blocking(runtime.session_store.root,
                                         token=runtime.environ.get(gateway_inhibition.TOKEN_ENV) or None)
    if record is not None:
        message, remedy = gateway_inhibition.refusal(record, "nothing is applied")
        return f"{message} (fix: {remedy})"
    return runtime.root_authority_refusal() or plan.refusal()


def plan_application(runtime: runtime_mod.Runtime, confirmed_digest: str,
                     assets: Path | None = None) -> served_plan.ServedChangePlan:
    """The installer-facing application boundary: recompute the plan
    and refuse unless it is applicable (:func:`application_refusal`) and
    still has the digest the operator confirmed. An installer applies only
    after this returns, under the served-change phase it holds."""

    plan, _identity = build(runtime, assets)
    refusal = application_refusal(runtime, plan)
    if refusal is not None:
        raise PlanError(refusal)
    if plan.digest != confirmed_digest and plan.digest[:16] != confirmed_digest:
        raise PlanError(served_plan.CHANGED_REFUSAL)
    return plan


def command(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    assets = Path(args.assets) if getattr(args, "assets", None) else None
    plan, identity = build(runtime, assets)
    if getattr(args, "json", False):
        document = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "kind": "served-change-plan",
            "running_launcher": __version__,
            "candidate": identity.as_document(),
            **plan.as_document(),
        }
        output_stream.write(json.dumps(document, sort_keys=True, ensure_ascii=False) + "\n")
        return 0
    lines = [f"Running launcher {__version__} (unchanged by --assets) · candidate assets "
             f"{identity.asset_root} (launcher {identity.launcher_version}, catalog {identity.catalog_version})",
             *plan.lines()]
    for line in lines:
        output_stream.write(termtext.visible_text(line) + "\n")
    output_stream.flush()
    return 0
