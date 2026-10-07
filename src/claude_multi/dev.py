"""Developer onboarding for claude-multi: drafts, check, review, promote.

Draft → Check → Review exact diff/hash → Promote source. Promotion writes
trusted repository JSON only: it never builds, activates, stages, commits,
restarts services, resolves real secrets, or contacts a provider. Dummy
secrets are used for every check/review render. The pre/post-image hash
contract fails closed on any source drift.

This entrypoint also hosts the explicitly gated disposable probe
harness: `probe init --allow-local-claude --fixture-root PATH` and
`probe run --allow-local-claude --fixture-root PATH --native-contract FILE
[--allow-real-execution] [-- args]`; both consent flags are presence-based
(their values are ignored and never relax any other check). It never
touches live Claude config, provider credentials, real providers, user
transcripts, or the live shared daemon.
"""

from __future__ import annotations

from . import assets

import copy
import difflib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import catalog as catalog_mod
from . import errors, layout
from . import render as render_mod
from . import sessions, state, strict_json
from . import validate as schema_validate


class DevError(errors.ClaudeMultiError, RuntimeError):
    """Raised on any onboarding failure (fail closed)."""


# Draft and review data version: catalog-33 drafts carry v2 model
# lines. The promote journal has no schema and stays version 1.
DRAFT_DATA_VERSION = 2
REVIEW_DATA_VERSION = 2


# A source checkout and the place it keeps the packaged resources
# (checkout-relative): developer commands read and write only there.
REPO_MARKERS = layout.CHECKOUT_MARKERS
RESOURCES = layout.RESOURCES_IN_CHECKOUT

_DUMMY_SECRET = "dummy-onboarding-secret"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dummy_resolver(_name: str) -> str:
    return _DUMMY_SECRET


# ---------------------------------------------------------------- drafts


class DraftStore:
    """Versioned strict-JSON drafts under XDG state, mode 0600."""

    def __init__(self, root: Path | str):
        self.root = state.ensure_private_dir(Path(root))

    def _path(self, name: str) -> Path:
        return self.root / f"{state.check_name(name)}.json"

    def save(self, name: str, draft: dict[str, Any]) -> Path:
        sessions.check_state_marker(self.root.parent)
        path = self._path(name)
        state.atomic_write(path, strict_json.canonical_file_bytes(draft))
        return path

    def load(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        try:
            draft = strict_json.loads(state.read_private(path))
        except state.StateError as exc:
            raise DevError(f"cannot read draft {name!r}: {exc}") from exc
        except strict_json.StrictJSONError as exc:
            raise DevError(f"draft {name!r} is corrupt: {exc}") from exc
        # Before any schema validation: a legacy (v1) draft gets the migrate pointer,
        # not the schema's const noise.
        if isinstance(draft, dict) and draft.get("version") == 1:
            raise DevError(
                f"draft {name} is a version-1 draft; run claude-multi-dev drafts migrate"
            )
        return draft


def _load_draft_schema(catalog_root: Path) -> dict[str, Any]:
    return strict_json.load(catalog_root / "schemas" / "draft.schema.json")


def _load_review_schema(catalog_root: Path) -> dict[str, Any]:
    return strict_json.load(catalog_root / "schemas" / "review.schema.json")


def _validate_draft(draft: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    problems = schema_validate.validate(draft, schema, "$")
    if problems:
        raise DevError("invalid draft: " + "; ".join(problems))
    return draft


def make_model_draft(
    *,
    name: str,
    provider: str,
    entry: dict[str, Any],
    notes: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Draft for a model on an existing provider (transport/auth inherited)."""

    draft: dict[str, Any] = {
        "version": DRAFT_DATA_VERSION,
        "kind": "model",
        "provider": provider,
        "entry": entry,
        "created_at": now or _now(),
    }
    if notes:
        draft["notes"] = notes
    return draft


def make_provider_draft(
    *,
    name: str,
    provider_profile: dict[str, Any],
    model_entry: dict[str, Any],
    contract_claims: list[str],
    notes: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Draft for a new provider; requires a complete model and typed profile."""

    if not model_entry:
        raise DevError("provider draft requires a complete model entry")
    if not contract_claims:
        raise DevError("provider draft requires contract claims")
    draft: dict[str, Any] = {
        "version": DRAFT_DATA_VERSION,
        "kind": "provider",
        "entry": {
            "provider": provider_profile,
            "model": model_entry,
            "contract_claims": contract_claims,
        },
        "created_at": now or _now(),
    }
    if notes:
        draft["notes"] = notes
    return draft


def draft_hash(draft: dict[str, Any]) -> str:
    return strict_json.bundle_digest(draft)


# ------------------------------------------------------- candidate images


def _entry_id(entry: dict[str, Any], what: str) -> str:
    ident = entry.get("id")
    if not ident:
        raise DevError(f"{what} entry requires an 'id' field")
    return str(ident)


_NEW_OFF_REFUSAL = (
    'promoted lines start New · not admitted (status "new"); local admission is an optional '
    "operator badge (claude-multi models admit KEY), not a draft field"
)


def _new_line_entry(
    docs: dict[str, Any], models_doc: dict[str, Any], key: str, draft_entry: dict[str, Any]
) -> dict[str, Any]:
    """The post-image line for ``key``: always ``status: new``.

    A promoted line starts New · not admitted; an explicit other status is refused
    (activation is a reviewed catalog edit). The key must be a fresh line
    key: never containing ``@``, never a retired key, never a live line.
    """

    if "@" in key:
        raise DevError(
            f"model {key!r}: '@' keys are retired generation entries, never promoted lines"
        )
    if not catalog_mod.LINE_KEY.fullmatch(key):
        raise DevError(f"model id {key!r} must match ^[a-z0-9][a-z0-9-]*$")
    retired = docs.get("retired", {"retired": {}})["retired"]
    if key in retired:
        raise DevError(
            f"model {key!r} is a retired catalog key; retired keys are never reused"
        )
    if key in models_doc["models"]:
        raise DevError(f"model {key!r} already exists in the catalog")
    entry = {k: v for k, v in draft_entry.items() if k != "id"}
    entry.setdefault("status", "new")
    if entry["status"] != "new":
        raise DevError(_NEW_OFF_REFUSAL)
    return entry


def build_post_images(
    docs: dict[str, Any], draft: dict[str, Any]
) -> dict[str, bytes]:
    """Exact canonical post-image bytes for every affected trusted file.

    ``docs`` are RAW catalog docs (``catalog.load_raw``): models v2 and the
    retired map. The promoted line always lands ``status: "new"``.
    """

    images: dict[str, bytes] = {}
    if draft["kind"] == "model":
        models_doc = strict_json.loads(
            strict_json.canonical_bytes(docs["models"])
        )
        key = _entry_id(draft["entry"], "model")
        models_doc["models"][key] = _new_line_entry(docs, models_doc, key, draft["entry"])
        images[str(RESOURCES / "catalog" / "models.json")] = (
            strict_json.pretty_file_bytes(models_doc)
        )
    else:
        provider_entry = draft["entry"]["provider"]
        model_entry = draft["entry"]["model"]
        provider_id = _entry_id(provider_entry, "provider")
        model_id = _entry_id(model_entry, "model")
        provider_clean = {k: v for k, v in provider_entry.items() if k != "id"}
        providers_doc = strict_json.loads(
            strict_json.canonical_bytes(docs["providers"])
        )
        models_doc = strict_json.loads(strict_json.canonical_bytes(docs["models"]))
        if provider_id in providers_doc["providers"]:
            raise DevError(f"provider {provider_id!r} already exists")
        model_clean = _new_line_entry(docs, models_doc, model_id, model_entry)
        providers_doc["providers"][provider_id] = provider_clean
        models_doc["models"][model_id] = model_clean
        images[str(RESOURCES / "catalog" / "providers.json")] = (
            strict_json.pretty_file_bytes(providers_doc)
        )
        images[str(RESOURCES / "catalog" / "models.json")] = (
            strict_json.pretty_file_bytes(models_doc)
        )
    return images


def _apply_images_to_docs(
    docs: dict[str, Any], images: dict[str, bytes]
) -> dict[str, Any]:
    applied = {key: strict_json.loads(strict_json.canonical_bytes(value)) for key, value in docs.items()}
    for relative, data in images.items():
        document = strict_json.loads(data)
        if relative.endswith("providers.json"):
            applied["providers"] = document
        elif relative.endswith("models.json"):
            applied["models"] = document
        else:
            raise DevError(f"unsupported post-image target {relative!r}")
    return applied


def _validate_post_image_schemas(repo: Path, applied: dict[str, Any]) -> None:
    """Run each candidate document through its actual trusted schema first."""

    for relative, document_key in (
        ("catalog/providers.json", "providers"),
        ("catalog/models.json", "models"),
    ):
        schema = strict_json.load(layout.checkout_resources(repo) / "schemas" / f"{document_key}.schema.json")
        problems = schema_validate.validate(applied[document_key], schema, "$")
        if problems:
            raise DevError(
                f"candidate {relative} fails its trusted schema: "
                + "; ".join(problems)
            )


def _validate_candidate(repo: Path, docs: dict[str, Any], prompt_bodies: dict[str, bytes]) -> None:
    problems = catalog_mod.validate_catalog(
        {"docs": docs, "prompt_bodies": prompt_bodies}
    )
    if problems:
        raise DevError("candidate bundle invalid: " + "; ".join(problems))


def _render_models(candidate_docs: dict[str, Any]) -> dict[str, Any]:
    """The model map the check/review dummy render consumes: the raw v2
    line map, so a promoted ``status: new`` line's own render is exercised
    (the gateway serves New lines). The dummy render passes
    ``continuity={}``: it never reads the operator's ``continuity.json``.
    """

    return candidate_docs["models"]["models"]


def _check_selector_shape(docs: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    """Catalog-33 selector shape of a promoted line.

    ``validate_catalog`` leaves these to shipped-shape tests and this promote
    gate, because the frozen test fixture violates them on purpose:

    - Anthropic (``cliproxy-oauth-claude-v1``): the selector is the
      canonical wire id, ``wire_model`` + ``[1m]`` iff the line books 1M
      client tokens.
    - OpenAI-compatible client-effort: ``claude-multi-<key>`` (+``[1m]``
      iff 1M), the shape the scaffold derives.
    - Gateway-effort: every ``efforts[<level>].selector`` is
      ``<prefix><key>-<level>`` (+``[1m]`` iff 1M) — key + effort, never
      the generation — with ``gpt-multi-`` for the codex pool
      and ``claude-multi-`` otherwise; its ``proxy_contract`` level equals
      the effort (the contract ends with ``-<level>``).
    """

    provider = docs["providers"]["providers"][entry["provider"]]
    suffix = "[1m]" if entry["context"]["client_tokens"] >= 1_000_000 else ""
    efforts = entry["efforts"]
    if provider["adapter"] == "cliproxy-oauth-claude-v1":
        expected = entry["wire_model"] + suffix
        if entry.get("selector") != expected:
            raise DevError(
                f"new model {key!r}: an Anthropic selector must be the canonical "
                f"wire id {expected!r}, not {entry.get('selector')!r}"
            )
        return
    if isinstance(efforts, list):
        expected = f"claude-multi-{key}{suffix}"
        if entry.get("selector") != expected:
            raise DevError(
                f"new model {key!r}: an OpenAI-compatible selector must be "
                f"{expected!r}, not {entry.get('selector')!r}"
            )
        return
    transport = provider["transport"]
    prefix = (
        "gpt-multi-"
        if transport.get("kind") == "oauth-pool" and transport.get("pool") == "codex"
        else "claude-multi-"
    )
    for level, spec in sorted(efforts.items()):
        expected = f"{prefix}{key}-{level}{suffix}"
        if spec["selector"] != expected:
            raise DevError(
                f"new model {key!r}: effort {level!r} selector must be {expected!r} "
                f"(key + effort, never the generation), not {spec['selector']!r}"
            )
        contract = spec["proxy_contract"]
        if not isinstance(contract, str) or not contract.endswith("-" + level):
            raise DevError(
                f"new model {key!r}: effort {level!r} proxy_contract {contract!r} "
                f"must carry the same level (…-{level})"
            )


def _check_new_entry_policy(docs: dict[str, Any], draft: dict[str, Any]) -> None:
    """New entries are New · not admitted: status new, catalog-33 selector shape,
    never bound in a seed profile.

    ``docs`` are the candidate (post-image) raw docs. The catalog carries no
    legacy composition (``load_raw`` never reads one), so only the
    seed-profile checks run.
    """

    if draft["kind"] == "model":
        keys = [_entry_id(draft["entry"], "model")]
        provider_ids: list[str] = []
    else:
        keys = [_entry_id(draft["entry"]["model"], "model")]
        provider_ids = [_entry_id(draft["entry"]["provider"], "provider")]
    for key in keys:
        entry = docs["models"]["models"][key]
        if entry.get("status") != "new":
            raise DevError(_NEW_OFF_REFUSAL)
        _check_selector_shape(docs, key, entry)
    models = docs["models"]["models"]
    for name in catalog_mod.SEED_PROFILE_NAMES:
        seed = docs.get(f"profiles/{name}")
        if seed is None:
            continue
        bound = [seed["lead"], *seed["agents"].values()]
        bound_models = {binding.get("model") for binding in bound}
        for key in keys:
            if key in bound_models:
                raise DevError(
                    f"new model {key!r} must not be bound in seed profile {name!r}; "
                    "it is New · not admitted"
                )
        bound_providers = {models[m]["provider"] for m in bound_models if m in models}
        bound_providers |= set(seed.get("lead_providers", []))
        if seed.get("primary_provider"):
            bound_providers.add(seed["primary_provider"])
        for provider_id in provider_ids:
            if provider_id in bound_providers:
                raise DevError(
                    f"new provider {provider_id!r} must not be bound in seed profile "
                    f"{name!r}; it is New · not admitted"
                )


# ------------------------------------------------------------ repo verify


def _repo_atomic_write(target: Path, data: bytes) -> None:
    """Atomic write inside a source checkout.

    Same-directory temp file, fsync file and directory, atomic replace. The
    checkout's own permissions apply (source files are group-readable); the
    stricter 0700/0600 `state` primitives guard private state instead.
    """

    parent = target.parent
    info = os.lstat(parent)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DevError(f"unsafe repo directory {parent}")
    if target.is_symlink():
        raise DevError(f"repo target {target} must not be a symlink")
    mode = 0o644
    if target.exists():
        existing = stat.S_IMODE(os.lstat(target).st_mode)
        mode = existing if existing else 0o644
    descriptor, temporary = tempfile.mkstemp(
        dir=parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def verify_repo(repo: Path | str) -> Path:
    """Require an explicit, writable, real source checkout (never the store);
    its resource directories must be real directories inside it."""

    try:
        return layout.verify_checkout(repo)
    except layout.LayoutError as exc:
        raise DevError(str(exc)) from None


def _checkout_target(repo: Path, relative: str) -> Path:
    """A promotion destination proven to stay inside the verified checkout."""

    try:
        return layout.checkout_destination(repo, relative)
    except layout.LayoutError as exc:
        raise DevError(str(exc)) from None


def repo_revision(repo: Path) -> str:
    """Source identity: git HEAD when available, 'nogit' otherwise."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "nogit"
    if completed.returncode != 0:
        return "nogit"
    return completed.stdout.strip() or "nogit"


# ------------------------------------------------------ scratch candidate


def materialize_candidate(
    repo: Path, images: dict[str, bytes], parent: Path | None = None
) -> Path:
    """Copy the verified checkout minus unsafe exclusions, apply post-images."""

    if parent is None:
        root = Path(state_root_default())
        state.ensure_private_dir(root)
        parent = Path(tempfile.mkdtemp(prefix="claude-multi-candidate-", dir=root))
    if parent.exists():
        os.chmod(parent, 0o700)
    state.ensure_private_dir(parent)
    try:
        candidate = parent / "tree"

        def _ignore(directory: str, names: list[str]) -> set[str]:
            skipped: set[str] = set()
            for name in names:
                if name in (".git", ".slim", "result") or name.startswith("result-"):
                    skipped.add(name)
                    continue
                # The repository root holds the development worktrees and
                # session files under .claude/; no build reads them.
                if name == ".claude" and Path(directory) == repo:
                    skipped.add(name)
                    continue
                if (Path(directory) / name).is_symlink():
                    skipped.add(name)
            return skipped

        shutil.copytree(repo, candidate, ignore=_ignore, symlinks=False)
        # The candidate tree is private: no group/other access anywhere.
        for root_dir, dirs, _files in os.walk(candidate):
            os.chmod(root_dir, 0o700)
            for name in dirs:
                os.chmod(Path(root_dir) / name, 0o700)
        for root_dir, _dirs, files in os.walk(candidate):
            for name in files:
                if (Path(root_dir) / name).is_symlink():
                    raise DevError("candidate copy retained a symlink")
        for relative, data in images.items():
            target = candidate / relative
            if not target.parent.is_dir():
                raise DevError(f"post-image target directory missing: {target.parent}")
            state.atomic_write(target, data)
        return candidate
    except BaseException:
        # A partially materialized candidate is removed before re-raising;
        # cleanup is constrained to the exact candidate root.
        _cleanup_candidate(parent)
        raise


def state_root_default() -> str:
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(root) / "claude-multi")


# --------------------------------------------------------------- lifecycle


@dataclass(frozen=True)
class CheckResult:
    draft_hash: str
    images: dict[str, bytes]
    render: render_mod.RenderResult
    bundle_valid: bool
    candidate: Path
    builds: tuple[dict[str, Any], ...]
    # The pinned-registry presence rule's report lines (non-fatal).
    registry: tuple[str, ...] = ()


def registry_presence(
    candidate_docs: dict[str, Any], environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """The pinned-registry presence rule.

    Flags every OAuth-pool line of the candidate catalog (New lines
    included) whose wire is absent from its pool's section of the pinned
    gateway registry (``claude`` / ``codex-pro``). A flag is a report, not
    a refusal: such a wire still routes, but its aliases drop from
    ``/v1/models`` until a registry patch or ``registry_overlay`` lands.
    The registry is the pristine pinned source (before the local gateway
    patches), so a wire a local registry patch adds is still flagged.
    Without package registry data the rule says so and is skipped.
    """

    root = catalog_mod.registry_dir(environ)
    if root is None:
        return (
            "registry: no pinned gateway registry in this build "
            f"({catalog_mod.REGISTRY_DIR_ENV} unset, no registry/ in the resources); "
            "presence rule skipped",
        )
    try:
        registry = catalog_mod.load_pinned_registry(root)
    except catalog_mod.CatalogError as exc:
        return (f"registry: {exc}; presence rule skipped",)
    absent = catalog_mod.registry_absent_wires(
        candidate_docs["models"]["models"],
        candidate_docs["providers"]["providers"],
        registry,
    )
    if not absent:
        return ("registry: every OAuth-pool line wire is in the pinned gateway registry",)
    return tuple(
        f"registry: line {key!r} wire {wire!r} is absent from the pinned gateway "
        f"registry section {section!r} — it routes, but its aliases drop from "
        "/v1/models until a registry patch or registry_overlay lands"
        for key, wire, section in absent
    )


RETARGET_NOTE = "running sessions of {line} retarget at the next gateway reload"


def gateway_effort_moves(
    before: dict[str, Any], after: dict[str, Any], providers: dict[str, Any],
) -> list[str]:
    """Notes for every gateway-effort generation move from ``before`` to
    ``after`` (print-only).

    Historical same-alias moves retarget on reload. Reviewed generation
    renames retain old aliases through continuity until resume instead.
    Mixed moves report both; names are compared as gateway alias bases,
    not client-class suffixes. There is no apply step to ask at.
    """

    notes: list[str] = []
    for key in sorted(set(before) & set(after)):
        old, new = before[key], after[key]
        provider = providers.get(new.get("provider"))
        if provider is None or catalog_mod.effort_mode(provider) != "gateway":
            continue
        if (old.get("wire_model"), old.get("generation")) == (
            new.get("wire_model"), new.get("generation"),
        ):
            continue
        old_aliases = {selector.removesuffix("[1m]") for _l, selector, _c in catalog_mod.line_selectors(old)}
        new_aliases = {selector.removesuffix("[1m]") for _l, selector, _c in catalog_mod.line_selectors(new)}
        move = f" ({old.get('wire_model')} {old.get('generation')} -> {new.get('wire_model')} {new.get('generation')})"
        if old_aliases & new_aliases:
            notes.append(
                RETARGET_NOTE.format(line=key) + move
                + "; shared selector names move at the next request"
            )
        if old_aliases - new_aliases:
            notes.append(
                f"running sessions of {key} keep old-generation aliases through continuity until resume"
                + move + "; retain removed aliases in retired.json"
            )
    return notes


def running_catalog_lines() -> dict[str, Any] | None:
    """The v2 lines of the catalog packaged with this launcher — what the
    installed gateway renders now — or None when it cannot be read."""

    try:
        raw = catalog_mod.load_raw(assets.root())
    except (catalog_mod.CatalogError, OSError, ValueError, KeyError):
        return None
    return raw["docs"]["models"]["models"]


def _default_runner(command: list[str], cwd: Path) -> dict[str, Any]:
    if shutil.which(command[0]) is None:
        return {"cmd": command, "skipped": f"{command[0]} not available"}
    completed = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=1800
    )
    return {
        "cmd": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def _sanitize_output(text: str) -> str:
    """Strip every nonempty value tied to a sensitive env name from output.

    Values of any length are redacted, longest first so overlapping values
    cannot survive partial replacement. Nonsecret diagnostics are retained.
    """

    sensitive: list[str] = []
    for name, value in os.environ.items():
        if not value:
            continue
        if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASS")):
            sensitive.append(value)
    sanitized = text
    for value in sorted(set(sensitive), key=len, reverse=True):
        sanitized = sanitized.replace(value, "***")
    return sanitized


def _cleanup_candidate(parent: Path) -> None:
    """Remove a scratch candidate tree deterministically and only there.

    The parent is the exact directory this module created; nothing outside it
    is ever touched, and no background reaper exists.
    """

    resolved = parent.resolve()
    if not resolved.is_dir() or resolved.is_symlink():
        return
    shutil.rmtree(resolved)


def _gate_builds(builds: tuple[dict[str, Any], ...]) -> None:
    """Candidate builds gate Check: genuine failures are deterministic errors.

    Binary-unavailable skips are a distinct, honestly reported nonfatal
    status; they are never confused with command failure.
    """

    for build in builds:
        target = build["cmd"][-1] if build.get("cmd") else "unknown"
        if "skipped" in build:
            continue
        code = build.get("returncode")
        if code != 0:
            detail = _sanitize_output(
                (build.get("stderr") or build.get("stdout") or "")[-800:]
            )
            raise DevError(
                f"candidate build failed for {target} (exit {code}): {detail}"
            )


def _reject_qualify_markers(draft: dict[str, Any]) -> None:
    """Scaffold QUALIFY markers must be replaced before check.

    A draft still carrying placeholder text in the judgment fields is a
    failed human gate, not a buildable candidate.
    """

    entries: list[dict[str, Any]] = []
    if draft["kind"] == "model":
        entries.append(draft["entry"])
    else:
        entries.append(draft["entry"]["model"])
        provider = draft["entry"].get("provider", {})
        # A provider draft from an operator declaration carries
        # QUALIFY-marked support text and contract claims.
        for field, value in (("support_note", provider.get("support_note")),
                             ("contract_claims", draft["entry"].get("contract_claims"))):
            if "QUALIFY" in str(value or ""):
                raise DevError(
                    f"draft provider still has a QUALIFY marker in {field!r} — "
                    "fill every QUALIFY field first"
                )
    for entry in entries:
        # Efforts (and the default effort a QUALIFY efforts field
        # implies) are judgment fields too.
        for field in ("generation", "display", "routing_note", "efforts", "default_effort"):
            if "QUALIFY" in str(entry.get(field, "")):
                raise DevError(
                    f"draft entry still has a QUALIFY marker in {field!r} — "
                    "fill every QUALIFY field first"
                )
        for field in ("qualification", "ordinary_profile"):
            if "QUALIFY" in str(entry.get("context", {}).get(field, "")):
                raise DevError(
                    f"draft entry still has a QUALIFY marker in {field!r} — "
                    "fill every QUALIFY field first"
                )


def check_draft(
    draft: dict[str, Any],
    *,
    repo: Path,
    runner: Callable[[list[str], Path], dict[str, Any]] | None = None,
    candidate_parent: Path | None = None,
) -> CheckResult:
    """Check: validate candidate bundle, dummy-secret render, scratch builds."""

    _reject_qualify_markers(draft)
    repo = verify_repo(repo)
    raw = catalog_mod.load_raw(layout.checkout_resources(repo))
    images = build_post_images(raw["docs"], draft)
    candidate_docs = _apply_images_to_docs(raw["docs"], images)
    _validate_post_image_schemas(repo, candidate_docs)
    _validate_candidate(repo, candidate_docs, raw["prompt_bodies"])
    _check_new_entry_policy(candidate_docs, draft)

    render_result = render_mod.render_config(
        candidate_docs["gateway"],
        candidate_docs["providers"]["providers"],
        _render_models(candidate_docs),
        home=Path("/home/onboarding"),
        gateway_token=_DUMMY_SECRET,
        resolve_secret=_dummy_resolver,
        continuity={},
    )
    # Deterministic render proof: two renders are byte-identical.
    again = render_mod.render_config(
        candidate_docs["gateway"],
        candidate_docs["providers"]["providers"],
        _render_models(candidate_docs),
        home=Path("/home/onboarding"),
        gateway_token=_DUMMY_SECRET,
        resolve_secret=_dummy_resolver,
        continuity={},
    )
    if again.yaml != render_result.yaml:
        raise DevError("renderer output is not deterministic")

    candidate = materialize_candidate(repo, images, candidate_parent)
    run = runner or _default_runner
    try:
        builds = (
            run(
                [
                    "nix-build",
                    "--no-out-link",
                    str(candidate / "tests" / "default.nix"),
                ],
                candidate,
            ),
            run(
                ["nix-build", "--no-out-link", str(candidate / "nix" / "package.nix")],
                candidate,
            ),
        )
        _gate_builds(builds)
        return CheckResult(
            draft_hash=draft_hash(draft),
            images=images,
            render=render_result,
            bundle_valid=True,
            candidate=candidate,
            builds=builds,
            registry=registry_presence(candidate_docs),
        )
    finally:
        # The scratch tree is transient; all evidence lives in the result.
        _cleanup_candidate(candidate.parent)


def _unified_diff(relative: str, before: bytes | None, after: bytes) -> str:
    old = [] if before is None else before.decode("utf-8").splitlines(keepends=True)
    new = after.decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}"
        )
    )


def review_draft(
    draft: dict[str, Any],
    *,
    draft_name: str,
    repo: Path,
    revision: str | None = None,
    now: str | None = None,
    check_result: "CheckResult | None" = None,
    runner: Callable[[list[str], Path], dict[str, Any]] | None = None,
    candidate_parent: Path | None = None,
) -> dict[str, Any]:
    """Review: exact file set, pre/post hashes, results, unified diff evidence.

    Review is never creatable from a failed check: it accepts a passed
    CheckResult or runs the same offline candidate gates itself first.
    """

    repo = verify_repo(repo)
    if check_result is None:
        check_result = check_draft(
            draft, repo=repo, runner=runner, candidate_parent=candidate_parent
        )
    raw = catalog_mod.load_raw(layout.checkout_resources(repo))
    images = build_post_images(raw["docs"], draft)
    candidate_docs = _apply_images_to_docs(raw["docs"], images)
    _validate_post_image_schemas(repo, candidate_docs)
    _validate_candidate(repo, candidate_docs, raw["prompt_bodies"])
    _check_new_entry_policy(candidate_docs, draft)

    render_result = render_mod.render_config(
        candidate_docs["gateway"],
        candidate_docs["providers"]["providers"],
        _render_models(candidate_docs),
        home=Path("/home/onboarding"),
        gateway_token=_DUMMY_SECRET,
        resolve_secret=_dummy_resolver,
        continuity={},
    )

    files: list[dict[str, Any]] = []
    diffs: list[str] = []
    for relative in sorted(images):
        target = _checkout_target(repo, relative)
        before: bytes | None = None
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise DevError(f"affected path {relative} is not a regular file")
            before = target.read_bytes()
        after = images[relative]
        files.append(
            {
                "path": relative,
                "pre_image_hash": (
                    None if before is None else "sha256:" + strict_json.sha256_hex(before)
                ),
                "post_image_hash": "sha256:" + strict_json.sha256_hex(after),
            }
        )
        diffs.append(_unified_diff(relative, before, after))

    record = {
        "version": REVIEW_DATA_VERSION,
        "draft": draft_name,
        "draft_hash": draft_hash(draft),
        "repo": {
            "path": str(repo),
            "revision": revision if revision is not None else repo_revision(repo),
        },
        "files": files,
        "results": {
            "bundle_valid": True,
            "render_sha256": "sha256:" + strict_json.sha256_hex(
                render_result.yaml.encode("utf-8")
            ),
            "unavailable_providers": list(render_result.unavailable),
            "diff": "\n".join(diffs),
        },
        "created_at": now or _now(),
    }
    problems = schema_validate.validate(
        record, _load_review_schema(layout.checkout_resources(repo)), "$"
    )
    if problems:
        raise DevError("review record invalid: " + "; ".join(problems))
    return record


def _validate_review_record(repo: Path, record: Any) -> dict[str, Any]:
    """Schema-validate a review record before any field access (fail closed)."""

    if not isinstance(record, dict):
        raise DevError("review record is not a JSON object")
    problems = schema_validate.validate(
        record, _load_review_schema(layout.checkout_resources(repo)), "$"
    )
    if problems:
        raise DevError("review record invalid: " + "; ".join(problems))
    return record


def promote_draft(
    draft: dict[str, Any],
    *,
    draft_name: str,
    repo: Path,
    review_record: dict[str, Any],
    drafts_root: Path | None = None,
    now: str | None = None,
    running_lines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Promote: re-verify hashes, apply reviewed post-images atomically.

    Applies only the reviewed JSON post-images with an in-memory journal and
    rollback on failure; never builds/activates/stages/commits/restarts,
    resolves no real secrets, and makes no provider call. The result's
    ``retargets`` are the gateway-effort generation-move notes between
    ``running_lines`` (the installed catalog's v2 lines; default: the repo
    pre-image) and the post-image.
    """

    if drafts_root is not None:
        # Refuse before source promotion too, not after applying post-images
        # when it is too late to write the corresponding state journal.
        sessions.check_state_marker(Path(drafts_root).parent)
    repo = verify_repo(repo)
    record = _validate_review_record(repo, review_record)
    if record["draft"] != draft_name or record["draft_hash"] != draft_hash(draft):
        raise DevError("review record does not match the draft")
    if record["repo"]["path"] != str(repo):
        raise DevError("review record repo path mismatch")

    raw = catalog_mod.load_raw(layout.checkout_resources(repo))
    images = build_post_images(raw["docs"], draft)
    candidate_docs = _apply_images_to_docs(raw["docs"], images)
    _validate_post_image_schemas(repo, candidate_docs)
    _validate_candidate(repo, candidate_docs, raw["prompt_bodies"])
    _check_new_entry_policy(candidate_docs, draft)

    # Hash contract: reviewed files must match recomputed post-images exactly.
    reviewed = {entry["path"]: entry for entry in record["files"]}
    if set(reviewed) != set(images):
        raise DevError("reviewed file set differs from the candidate file set")
    journal: list[dict[str, Any]] = []
    for relative in sorted(images):
        after = images[relative]
        expected_post = reviewed[relative]["post_image_hash"]
        actual_post = "sha256:" + strict_json.sha256_hex(after)
        if actual_post != expected_post:
            raise DevError(f"post-image hash mismatch for {relative}: source drift")
        target = _checkout_target(repo, relative)
        before: bytes | None = None
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise DevError(f"affected path {relative} is not a regular file")
            before = target.read_bytes()
        expected_pre = reviewed[relative]["pre_image_hash"]
        actual_pre = None if before is None else "sha256:" + strict_json.sha256_hex(before)
        if actual_pre != expected_pre:
            raise DevError(f"pre-image hash mismatch for {relative}: source changed after review")
        journal.append({"path": relative, "before": before})

    targets = {entry["path"]: _checkout_target(repo, entry["path"]) for entry in journal}
    applied: list[str] = []
    try:
        for entry in journal:
            _repo_atomic_write(targets[entry["path"]], images[entry["path"]])
            applied.append(entry["path"])
    except BaseException:
        for entry in journal:
            target = targets[entry["path"]]
            if entry["path"] not in applied:
                continue
            if entry["before"] is None:
                target.unlink(missing_ok=True)
            else:
                _repo_atomic_write(target, entry["before"])
        raise DevError("promotion failed and was rolled back") from None

    if drafts_root is not None:
        journal_record = {
            "version": 1,
            "draft": draft_name,
            "applied": applied,
            "post_image_hashes": {
                path: "sha256:" + strict_json.sha256_hex(images[path])
                for path in sorted(applied)
            },
            "created_at": now or _now(),
        }
        sessions.check_state_marker(Path(drafts_root).parent)
        store = DraftStore(drafts_root)
        state.atomic_write(
            store.root / f"{draft_name}.journal.json",
            strict_json.canonical_file_bytes(journal_record),
        )
    retargets = gateway_effort_moves(
        raw["docs"]["models"]["models"] if running_lines is None else running_lines,
        candidate_docs["models"]["models"],
        candidate_docs["providers"]["providers"],
    )
    return {"applied": applied, "draft_hash": record["draft_hash"], "retargets": retargets}


def resolve_promote_mode(repo: Any, patch_output: Any) -> str:
    """Promote forms are mutually exclusive: apply to source XOR emit a patch."""

    if repo is not None and patch_output is not None:
        raise DevError(
            "promote accepts either --repo PATH (apply) or --patch-output FILE "
            "(emit-only), never both"
        )
    if patch_output is not None:
        return "patch"
    if repo is not None:
        return "apply"
    raise DevError("promote requires --repo PATH or --patch-output FILE")


def promote_patch_output(
    draft: dict[str, Any], *, repo: Path, output: Path,
    running_lines: dict[str, Any] | None = None,
) -> tuple[Path, list[str]]:
    """Emit-only candidate patch: no draft/source mutation, mutually exclusive.

    Returns the patch path and the gateway-effort retarget notes (as
    ``promote_draft``).
    """

    repo = verify_repo(repo)
    raw = catalog_mod.load_raw(layout.checkout_resources(repo))
    images = build_post_images(raw["docs"], draft)
    applied_docs = _apply_images_to_docs(raw["docs"], images)
    _validate_post_image_schemas(repo, applied_docs)
    _validate_candidate(repo, applied_docs, raw["prompt_bodies"])
    _check_new_entry_policy(applied_docs, draft)
    diffs = []
    for relative in sorted(images):
        target = _checkout_target(repo, relative)
        before = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        diffs.append(_unified_diff(relative, before, images[relative]))
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DevError(f"patch output directory {parent} is unsafe")
    if output.is_symlink():
        raise DevError(f"patch output {output} must not be a symlink")
    # Private-state primitive: same-directory mode-0600 atomic write.
    state.atomic_write(output, "\n".join(diffs).encode("utf-8"))
    retargets = gateway_effort_moves(
        raw["docs"]["models"]["models"] if running_lines is None else running_lines,
        applied_docs["models"]["models"],
        applied_docs["providers"]["providers"],
    )
    return output, retargets


# ---------------------------------------------------------- drafts migrate
# Tooling only. Running it on the operator's live drafts is an
# activation step (a dry run first; --apply after review).

DRAFTS_ARCHIVE = "archive-2x"
_MIGRATED_NOTE = (
    "migrated from a version-1 draft by claude-multi-dev drafts migrate; "
    "fill every QUALIFY field and review selectors/roles before check"
)
_V1_ENTRY_FIELDS = (
    "provider",
    "display",
    "wire_model",
    "capabilities",
    "context",
    "lead",
    "routing_note",
    "minimum_tested",
)
# The legacy role ids a catalog-32 (v1) draft can name, in the legacy roles.json
# order. The catalog's copy went with the legacy roles view; the
# drafts migration still reads v1 drafts, so it keeps its own.
_V1_DRAFT_ROLE_IDS = ("cm-lead", "cm-analyst", "cm-reviewer", "cm-implementer")


@dataclass(frozen=True)
class DraftMigration:
    """One draft's classification (``drafts migrate``)."""

    name: str
    action: str  # "archive" | "migrate" | "discard" | "keep"
    reason: str
    files: tuple[str, ...]  # file names in the drafts root, draft first
    converted: dict[str, Any] | None = None  # the v2 draft for "migrate"


def v1_draft_entry_to_v2(
    key: str, entry: dict[str, Any], provider: dict[str, Any], role_ids: list[str]
) -> dict[str, Any]:
    """A catalog-32 (lane-shaped) draft entry as a v2 New · not admitted line.

    Mirrors the fixture converter (spec Appendix B,
    ``tests/fixtures/convert_models_v1_to_v2.py``) with the migrate
    overrides: ``generation`` becomes a QUALIFY marker and ``status`` is
    ``new``. Raises DevError when the entry is not convertible.
    """

    try:
        lanes = entry["lanes"]
        default = entry["default_lane"]
        if not isinstance(lanes, dict) or not lanes or default not in lanes:
            raise DevError("lanes/default_lane are not a lane map with its default")
        for lane_id, lane in lanes.items():
            if lane["agent_effort"] != lane_id:
                raise DevError(f"lane {lane_id!r} effort {lane['agent_effort']!r} differs from its id")
        if provider["adapter"] in catalog_mod.CLIENT_EFFORT_ADAPTERS:
            if len(lanes) != 1 or entry["client_selector"] != lanes[default]["client_selector"]:
                raise DevError("a client-effort entry needs exactly one lane on its client selector")
            shape: dict[str, Any] = {"selector": entry["client_selector"], "efforts": [default]}
        else:
            shape = {
                "efforts": {
                    level: {
                        "selector": lane["client_selector"],
                        "proxy_contract": lane["proxy_effort_contract"],
                    }
                    for level, lane in lanes.items()
                }
            }
        non_lead = [role for role in role_ids if role != catalog_mod.LEAD_ROLE]
        roles = [role for role in entry["compatible_roles"] if role != catalog_mod.LEAD_ROLE]
        out: dict[str, Any] = {"id": key}
        out.update({field: copy.deepcopy(entry[field]) for field in _V1_ENTRY_FIELDS})
    except (KeyError, TypeError, AttributeError) as exc:
        raise DevError(f"missing or malformed v1 field {exc}") from None
    out.update(
        shape,
        generation="QUALIFY: generation",
        default_effort=default,
        roles="all" if set(roles) == set(non_lead) else [r for r in non_lead if r in roles],
        status="new",
        registry_overlay=None,
    )
    return out


def _drafts_root() -> Path:
    """The live drafts directory path (never created here)."""

    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        / "claude-multi"
        / "drafts"
    )


def _read_draft_file(path: Path) -> Any:
    return strict_json.loads(state.read_private(path))


def plan_drafts_migration(
    root: Path, raw: dict[str, Any], *, now: str | None = None
) -> tuple[list[DraftMigration], int, list[str]]:
    """Classify every draft under ``root`` (read-only).

    ``raw`` is ``catalog.load_raw`` of the catalog the drafts migrate to.
    Returns ``(migrations, file_count, orphans)``: every ``*.json`` at the
    top level that is neither a review nor a journal is a draft; its
    ``.review.json`` / ``.journal.json`` travel with it. ``file_count``
    counts every top-level ``*.json`` file; ``orphans`` are review/journal
    files whose draft is gone (reported, left in place).

    Classes (spec §6.5): (a) a journal exists -> archive (promoted in
    catalog <= 32); (b) version 1, kind model, convertible -> migrate to a
    v2 draft (status new, generation QUALIFY); (c) anything else ->
    discard (archived; re-draft against catalog 33). A draft already at
    version 2 is kept untouched.
    """

    root = Path(root)
    if not root.is_dir():
        return [], 0, []
    docs = raw["docs"]
    providers = docs["providers"]["providers"]
    # v1 drafts name only the legacy roles: "all" means every legacy agent id
    # (raw roles v2 has ten ids, which a v1 draft can never cover).
    role_ids = list(_V1_DRAFT_ROLE_IDS)
    draft_schema = raw["schemas"]["draft"]
    files = sorted(
        path.name
        for path in root.glob("*.json")
        if path.is_symlink() or path.is_file()
    )
    companions = (".review.json", ".journal.json")
    drafts = [name for name in files if not name.endswith(companions)]
    draft_names = {name[: -len(".json")] for name in drafts}
    orphans = [
        name
        for name in files
        if name.endswith(companions)
        and name[: -len(next(s for s in companions if name.endswith(s)))] not in draft_names
    ]
    plan: list[DraftMigration] = []
    for file_name in drafts:
        name = file_name[: -len(".json")]
        owned = tuple(
            [file_name]
            + [f"{name}{suffix}" for suffix in companions if f"{name}{suffix}" in files]
        )
        try:
            draft = _read_draft_file(root / file_name)
        except (state.StateError, OSError, ValueError, RecursionError) as exc:
            plan.append(DraftMigration(name, "discard", f"unreadable ({exc.__class__.__name__}): re-draft against catalog 33", owned))
            continue
        version = draft.get("version") if isinstance(draft, dict) else None
        if version == DRAFT_DATA_VERSION:
            plan.append(DraftMigration(name, "keep", "already a v2 draft: untouched", owned))
            continue
        if f"{name}.journal.json" in files:
            plan.append(DraftMigration(name, "archive", "promoted in catalog <=32: archive", owned))
            continue
        reason = "discard: re-draft against catalog 33"
        if version == 1 and draft.get("kind") == "model" and isinstance(draft.get("entry"), dict):
            entry = draft["entry"]
            key = entry.get("id")
            provider_id = entry.get("provider", draft.get("provider"))
            if not isinstance(key, str) or not key:
                reason = "discard: the entry has no id; re-draft against catalog 33"
            elif provider_id not in providers:
                reason = (
                    f"discard: provider {provider_id!r} is not in the catalog; "
                    "re-draft against catalog 33"
                )
            else:
                try:
                    converted_entry = v1_draft_entry_to_v2(
                        key, entry, providers[provider_id], role_ids
                    )
                except DevError as exc:
                    reason = f"discard: not convertible ({exc}); re-draft against catalog 33"
                else:
                    notes = draft.get("notes")
                    combined = f"{notes} | {_MIGRATED_NOTE}" if notes else _MIGRATED_NOTE
                    converted: dict[str, Any] = {
                        "version": DRAFT_DATA_VERSION,
                        "kind": "model",
                        "provider": provider_id,
                        "entry": converted_entry,
                        "created_at": now or _now(),
                        "notes": combined if len(combined) <= 1024 else notes,
                    }
                    if "fixtures" in draft:
                        converted["fixtures"] = copy.deepcopy(draft["fixtures"])
                    problems = schema_validate.validate(converted, draft_schema, "$")
                    if problems:
                        reason = (
                            "discard: the converted draft fails the draft schema ("
                            + "; ".join(problems)
                            + "); re-draft against catalog 33"
                        )
                    else:
                        plan.append(
                            DraftMigration(
                                name,
                                "migrate",
                                "migrate to v2 (status new, generation QUALIFY)",
                                owned,
                                converted,
                            )
                        )
                        continue
        plan.append(DraftMigration(name, "discard", reason, owned))
    return plan, len(files), orphans


def apply_drafts_migration(root: Path, plan: list[DraftMigration]) -> list[str]:
    """Carry out a plan: rename into ``archive-2x/`` (0700), never delete.

    (a) and (c) move the draft with its review/journal; (b) moves the
    original draft and its (now stale, v1) review, then writes the converted
    v2 draft under the original name. Refuses under a newer state marker
    and refuses the whole run, before any change, if an archive name is
    already taken.
    """

    root = Path(root)
    sessions.check_state_marker(root.parent)
    moving = [migration for migration in plan if migration.action != "keep"]
    if not moving:
        return []
    archive = root / DRAFTS_ARCHIVE
    taken = [
        file_name
        for migration in moving
        for file_name in migration.files
        if os.path.lexists(archive / file_name)
    ]
    if taken:
        raise DevError(
            f"{DRAFTS_ARCHIVE}/ already holds {', '.join(sorted(taken))}; "
            "move those aside first (nothing was changed)"
        )
    state.ensure_private_dir(root)  # validates the existing root; never creates it here
    state.ensure_private_dir(archive)
    report: list[str] = []
    for migration in moving:
        for file_name in migration.files:
            os.rename(root / file_name, archive / file_name)
        if migration.action == "migrate" and migration.converted is not None:
            state.atomic_write(
                root / f"{migration.name}.json",
                strict_json.canonical_file_bytes(migration.converted),
            )
            report.append(
                f"{migration.name}: migrated to a v2 draft (original in {DRAFTS_ARCHIVE}/)"
            )
        else:
            verb = "archived" if migration.action == "archive" else "discarded (archived)"
            report.append(f"{migration.name}: {verb} {', '.join(migration.files)}")
    for directory in (archive, root):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return report


# -------------------------------------------------------------- smoke test

def smoke_test(model: str, *, allow_provider_call: bool) -> dict[str, Any]:
    """Consent-gated live smoke. Without consent: zero requests, guidance only.

    No provider transport is wired in this workflow, so consenting calls
    fail closed instead of touching a network.
    """

    if not allow_provider_call:
        return {
            "status": "refused",
            "requests": 0,
            "guidance": (
                "smoke-test requires explicit --allow-provider-call consent; "
                "no external request was made"
            ),
        }
    raise DevError("no provider transport is wired in this workflow")


# -------------------------------------------------------------------- CLI


def _parse_flags(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    positionals: list[str] = []
    flags: dict[str, Any] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            key = token[2:]
            if "=" in key:
                key, value = key.split("=", 1)
                flags[key] = value
            elif index + 1 < len(args) and not args[index + 1].startswith("--"):
                flags[key] = args[index + 1]
                index += 1
            else:
                flags[key] = True
        else:
            positionals.append(token)
        index += 1
    return positionals, flags


def _draft_store() -> DraftStore:
    return DraftStore(_drafts_root())


def _drafts_migrate(flags: dict[str, Any], repo: Path | None) -> int:
    """``drafts migrate [--apply] [--repo PATH]``.

    The target catalog is ``--repo``'s (a verified checkout) or, without
    it, the catalog packaged with this launcher. The dry run (default)
    reads only; ``--apply`` renames into ``archive-2x/`` and never deletes.
    """

    unknown = sorted(set(flags) - {"apply", "repo"})
    if unknown:
        raise DevError(f"drafts migrate: unknown option --{unknown[0]}")
    apply = bool(flags.get("apply"))
    if apply and flags.get("apply") is not True:
        raise DevError("drafts migrate: --apply takes no value")
    root = _drafts_root()
    if apply:
        sessions.check_state_marker(root.parent)
    catalog_root = (
        layout.checkout_resources(verify_repo(repo)) if repo is not None else assets.root()
    )
    raw = catalog_mod.load_raw(catalog_root)
    plan, file_count, orphans = plan_drafts_migration(root, raw)
    for migration in plan:
        print(f"{migration.name}: {migration.reason} ({', '.join(migration.files)})")
    for name in orphans:
        print(f"{name}: no matching draft; left in place")
    print(f"drafts: {len(plan)} (files: {file_count})")
    if not apply:
        print("dry run: nothing was changed; rerun with --apply to carry this out")
        return 0
    for line in apply_drafts_migration(root, plan):
        print(line)
    counts = {
        action: sum(1 for migration in plan if migration.action == action)
        for action in ("archive", "migrate", "discard", "keep")
    }
    print(
        f"applied: archived {counts['archive']}, migrated {counts['migrate']}, "
        f"discarded {counts['discard']}, kept {counts['keep']} "
        f"(originals in {root / DRAFTS_ARCHIVE}; nothing deleted)"
    )
    return 0


DEV_HELP = """claude-multi-dev — catalog onboarding pipeline (draft → check → review → promote)

  claude-multi-dev --version   the release identity (launcher, catalog, gateway, Claude Code)

Two tracks:
  Existing-provider model release:
    claude-multi-dev model add --like astra --id NEWID --wire-id WIRE --name DRAFT
      (scaffolds a draft from the like-model with QUALIFY markers at the
       judgment fields — fill those before check)
    or: model add --from-json FILE --name DRAFT (a complete hand-written entry)
    or: hand-edit the checkout's src/claude_multi/data/catalog/models.json
        with the full battery (AGENTS.md §5)
  New provider / provider kind:
    claude-multi-dev provider add --from-json FILE --name DRAFT

  Prefill from observations (review context, never catalog verification):
    claude-multi-dev model add --from-registry CHANNEL:WIRE --id KEY [--provider P] [--name DRAFT]
      (a pinned-registry candidate: claude-multi models --candidates)
    claude-multi-dev model add --from-operator KEY --id CATALOG_KEY [--name DRAFT]
      (an operator declaration; a line on a T2-only provider becomes a provider draft)
    The four sources (--like, --from-json, --from-registry, --from-operator) are
    mutually exclusive.

Then:  check DRAFT → review DRAFT → promote DRAFT --repo PATH
Developer promotion installs a New · not admitted catalog line (status "new"); operator
admission is only an optional local badge (Models, or `claude-multi models
admit KEY`), not permission to use it. Selectors follow the catalog-33 shape: Anthropic =
canonical wire[1m], gateway-effort = <prefix><key>-<effort>[1m].
`check` also reports every OAuth-pool line whose wire is absent from the
pinned gateway registry (package data; a report, never a refusal).
`promote` notes each gateway-effort line whose generation differs from the
installed catalog: shared aliases retarget at reload; renamed aliases retain
the old generation through continuity until resume. Reviewed generation moves
use generation-tagged aliases and retire the old ones. --like refuses a
generation-tagged sibling; use --from-json and review the new entry.

Version 1 drafts:
  claude-multi-dev drafts migrate [--apply] [--repo PATH]
    (dry run by default; --apply archives promoted/unconvertible drafts and
     converts v1 model drafts to v2 under drafts/archive-2x/ — never deletes)
No test pin or golden moves for a shipped line: the behavioural suite and
the goldens run on the frozen test fixture (tests/check_fixture_isolation.py
proves it), and the shipped-shape/evidence tests in tests/test_catalog.py
derive from the loaded catalog (only a minimum_tested.claude_code above the
baseline goes into its locked `floors` map). Removing, renaming or moving a
line (an Anthropic generation move) needs an entry in the checkout's
src/claude_multi/data/catalog/retired.json instead (AGENTS.md §5); the
frozen legacy selector table (FrozenSelectorCoverageTests) must stay covered —
never edit it to pass.

Claude Code re-pin (maintainers, from a source checkout):
  claude-multi-dev repin [--repo PATH] [--manifest-dir DIR]
                         [--settings-keys FILE]
    Finds a Claude Code newer than the pin (the claude on PATH, then the native
    versions directories), verifies it against Anthropic's signed release
    manifest (GnuPG; the download is asked first, or --manifest-dir reads
    <version>/manifest.json(.sig) offline), inspects it, writes the next
    contract into the checkout (--repo; default: the checkout this command runs
    from), runs the evidence suite and restores the checkout on any failure.
    A new pin records the settings keys its build knows, read from the file;
    when they cannot be read it stops, and --settings-keys FILE records a
    reviewed JSON array of key names instead. The new pin reaches users with
    the next release built from the checkout; repin builds and installs nothing.
  python3 tools/pin_claude.py writes the next contract from a verified manifest
  pair without running the battery (see its --help).

Never performed by promote: builds, activations, restarts, secret reads.
`check` runs the sandbox builds in a candidate tree — that is the gate.
Live provider calls need per-call approval (a listing via `claude-multi
discover PROVIDER`, in a terminal outside Claude Code sessions). Promotion
writes trusted repository JSON only.
"""

PREFILL_REVIEW = ("Registry/listing values and operator qualification are review context, "
                  "not catalog verification.")
PREFILL_PROMOTION = ("Promotion installs a New · not admitted catalog line; local admission is an optional "
                     "operator badge, not permission to use it.")
_ROUTE_PREREQUISITE = ("prerequisite: this pool wire needs a reviewed passthrough-route change before "
                       "check can pass")
_NOTES_MAX = 1024


def _fresh_key_check(docs: dict[str, Any], new_id: str) -> None:
    models = docs["models"]["models"]
    retired = docs.get("retired", {"retired": {}})["retired"]
    if not catalog_mod.LINE_KEY.fullmatch(new_id):
        raise DevError(f"model id {new_id!r} must match ^[a-z0-9][a-z0-9-]*$")
    if new_id in models:
        raise DevError(f"model {new_id!r} already exists in the catalog")
    at_bases = {key.split("@", 1)[0] for key in retired if "@" in key}
    if new_id in retired or new_id in at_bases:
        raise DevError(f"model id {new_id!r} is a retired catalog key; retired keys are never reused")


def _selector_collisions(docs: dict[str, Any], entry: dict[str, Any]) -> None:
    models = docs["models"]["models"]
    retired = docs.get("retired", {"retired": {}})["retired"]
    providers = docs["providers"]["providers"]

    def base(selector: str) -> str:
        return selector.removesuffix("[1m]")

    taken = {base(selector) for line in models.values()
             for _level, selector, _contract in catalog_mod.line_selectors(line)}
    taken |= {base(selector) for item in retired.values() for selector in item["selectors"]}
    taken |= {route["name"] for item in providers.values() for route in item["passthrough_routes"]}
    try:
        proposed = {base(selector) for _l, selector, _c in catalog_mod.line_selectors(entry)}
    except (KeyError, TypeError, AttributeError):
        proposed = {base(entry["selector"])} if isinstance(entry.get("selector"), str) else set()
    collision = proposed & (taken - {entry["wire_model"]} if providers[entry["provider"]]["adapter"]
                            == "cliproxy-oauth-claude-v1" else taken)
    if collision:
        raise DevError(f"selector collision with the catalog (live, retired or route): {sorted(collision)}")


def _prefill_entry(
    docs: dict[str, Any], *, provider_id: str, new_id: str, wire: str, declared: int,
    levels: Any, display: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """A T1 model line from observed facts: selectors recomputed by
    the catalog-33 rules (never string surgery), mechanical fields from a
    reviewed sibling on the provider, judgment fields QUALIFY-marked, status
    new. ``levels`` is a {level: contract} map, a level list, or None.
    Returns (entry, prerequisite lines)."""

    providers = docs["providers"]["providers"]
    models = docs["models"]["models"]
    if provider_id not in providers:
        raise DevError(f"provider {provider_id!r} is not in the catalog (have: {', '.join(sorted(providers))})")
    provider = providers[provider_id]
    siblings = sorted(key for key, line in models.items() if line["provider"] == provider_id)
    if not siblings:
        raise DevError(f"no reviewed line on provider {provider_id!r} to take the mechanical fields from — "
                       "write the entry with model add --from-json")
    active = [key for key in siblings if models[key].get("status", "active") == "active"]
    template = models[(active or siblings)[0]]
    client = 1_000_000 if declared > catalog_mod.CUSTOM_VALIDATED_CAP else catalog_mod.CUSTOM_VALIDATED_CAP
    suffix = "[1m]" if client >= 1_000_000 else ""
    prerequisites: list[str] = []
    entry: dict[str, Any] = {
        "id": new_id,
        "provider": provider_id,
        "display": f"QUALIFY: display name for {new_id}" + (f" (observed: {display[:40]})" if display else ""),
        "generation": "QUALIFY: generation",
        "wire_model": wire,
    }
    transport = provider["transport"]
    if provider["adapter"] == "cliproxy-oauth-claude-v1":
        entry["selector"] = wire + suffix
        entry["efforts"] = (list(levels) if isinstance(levels, list) and levels
                            else "QUALIFY: client-effort levels (a list of native efforts)")
        routes = {route["name"] for route in provider["passthrough_routes"]}
        if wire not in routes:
            prerequisites.append(_ROUTE_PREREQUISITE)
    elif catalog_mod.effort_mode(provider) == "client":
        entry["selector"] = f"claude-multi-{new_id}{suffix}"
        entry["efforts"] = (list(levels) if isinstance(levels, list) and levels
                            else "QUALIFY: client-effort levels (a list of native efforts)")
    else:
        prefix = "gpt-multi-" if transport.get("kind") == "oauth-pool" and transport.get("pool") == "codex" \
            else "claude-multi-"
        if isinstance(levels, dict) and levels:
            entry["efforts"] = {level: {"selector": f"{prefix}{new_id}-{level}{suffix}", "proxy_contract": contract}
                                for level, contract in sorted(levels.items())}
        else:
            entry["efforts"] = ("QUALIFY: efforts map {level: {selector: "
                                f"{prefix}{new_id}-<level>{suffix}, proxy_contract: <reviewed -<level> contract>}}}}")
            prerequisites.append(
                f"prerequisite: provider {provider_id} is gateway-effort; the efforts map needs a reviewed "
                "contract per level before check can pass")
    efforts = entry["efforts"]
    if isinstance(efforts, (list, dict)):
        ordered = list(efforts) if isinstance(efforts, list) else sorted(efforts)
        entry["default_effort"] = "high" if "high" in ordered else ordered[-1]
    else:
        entry["default_effort"] = "QUALIFY: default effort (one of the efforts)"
    profile = "large" if client >= 1_000_000 else (
        template["context"].get("ordinary_profile")
        if template["context"].get("client_tokens") == client and template["context"].get("ordinary_profile")
        else "QUALIFY: ordinary profile of this client class")
    entry.update({
        "lead": copy.deepcopy(template["lead"]),
        "capabilities": list(template["capabilities"]),
        "roles": copy.deepcopy(template["roles"]),
        "context": {
            "client_tokens": client,
            "provider_tokens": min(declared, client),
            "scalar_tokens": None,
            "ordinary_profile": profile,
            "declared_tokens": declared,
            "validated_tokens": min(declared, catalog_mod.CUSTOM_VALIDATED_CAP),
            "qualification": ("QUALIFY: context-bound evidence (provider doc or benchmark); "
                              "unverified until an approval-gated live acceptance call."),
        },
        "routing_note": "QUALIFY: routing guidance (when to prefer this model)",
        "minimum_tested": {**template["minimum_tested"],
                           "cliproxyapi": docs["gateway"]["gateway"]["cliproxyapi_baseline"]},
        "status": "new",
        "registry_overlay": None,
    })
    if isinstance(entry["efforts"], (list, dict)):
        _selector_collisions(docs, entry)
    return entry, prerequisites


def _registry_prefill(
    docs: dict[str, Any], registry: catalog_mod.PinnedRegistry, source: str, new_id: str,
    provider_flag: str | None,
) -> tuple[str, dict[str, Any], list[str], str]:
    """``--from-registry CHANNEL:WIRE``: (provider, entry, prerequisites, notes)."""

    from . import account_pools
    from . import discovery as discovery_mod

    channel, separator, wire = source.partition(":")
    if not separator or not channel or not wire:
        raise DevError("--from-registry takes CHANNEL:WIRE (see claude-multi models --candidates)")
    if channel not in registry.sections:
        raise DevError(f"registry channel {channel!r} is not in the pinned registry "
                       f"(have: {', '.join(sorted(registry.sections))})")
    if wire not in registry.sections[channel]:
        raise DevError(f"{wire!r} is not in pinned registry channel {channel!r}")
    providers = docs["providers"]["providers"]
    if provider_flag is not None:
        provider_id = provider_flag
        if provider_id not in providers:
            raise DevError(f"--provider {provider_id!r} is not a catalog provider")
    else:
        hint = discovery_mod.section_provider(channel)
        pools = account_pools.section_pools(channel)
        pool = pools[0] if len(pools) == 1 else None
        hinted = providers.get(hint) if hint else None
        if hinted is None or hinted["transport"].get("pool") != pool:
            raise DevError(f"registry channel {channel!r} does not identify one reviewed catalog provider — "
                           "name it with --provider PROVIDER")
        provider_id = hint
    model = registry.meta.get(channel, {}).get(wire) or catalog_mod.RegistryModel()
    if model.context_length is None:
        raise DevError(f"the pinned registry states no context for {channel}:{wire} — "
                       "write the entry with model add --from-json")
    provider = providers[provider_id]
    levels: Any = None
    if catalog_mod.effort_mode(provider) != "client" and model.thinking_levels:
        contracts = [c for c in provider["payload_contracts"]]
        levels = {level: next(c for c in sorted(contracts) if c.endswith(f"-{level}"))
                  for level in model.thinking_levels if any(c.endswith(f"-{level}") for c in contracts)}
    entry, prerequisites = _prefill_entry(docs, provider_id=provider_id, new_id=new_id, wire=wire,
                                          declared=model.context_length, levels=levels,
                                          display=model.display_name)
    facts = [f"registry {channel}:{wire}", f"registry-stated context {model.context_length}"]
    if model.max_completion_tokens is not None:
        facts.append(f"max output {model.max_completion_tokens}")
    if model.thinking_levels:
        facts.append(f"thinking levels {','.join(model.thinking_levels)}")
    if model.created is not None:
        facts.append(f"created {datetime.fromtimestamp(model.created, tz=timezone.utc).strftime('%Y-%m-%d')}")
    notes = ("prefilled from " + "; ".join(facts) + ". " + PREFILL_REVIEW + " Fill every QUALIFY field.")
    return provider_id, entry, prerequisites, notes[:_NOTES_MAX]


def _evidence_summary(evidence: Any, key: str, digest: str) -> str:
    """Bounded, verdict-only qualification context (never the store)."""

    record = evidence.lines.get(key) if evidence is not None else None
    if not isinstance(record, dict):
        return "operator qualification: none recorded"
    current = "current" if record.get("digest") == digest else "stale (other definition)"
    parts: list[str] = []
    for name, check in sorted((record.get("checks") or {}).items()):
        if isinstance(check, dict) and isinstance(check.get("result"), str):
            at = check.get("at") if isinstance(check.get("at"), str) else "?"
            parts.append(f"{name} {check['result']} {at[:10]}")
    return f"operator qualification ({current}): " + (", ".join(parts) or "no checks")


def _operator_prefill(
    docs: dict[str, Any], environ: dict[str, str], asset_root: Path, key: str, new_id: str,
) -> tuple[str, dict[str, Any], list[str], str]:
    """``--from-operator KEY``: (kind, draft payload, prerequisites, notes).

    A line on a catalog (T1) provider becomes a model draft; a line on a
    T2-only provider becomes a provider draft (its T1 projection plus the
    one model and QUALIFY-marked contract claims). Never copies admission,
    route approval, secrets, evidence or ledger state.
    """

    from . import custom as custom_mod
    from . import operator as operator_mod

    snapshot = operator_mod.load_snapshot(environ, docs, asset_root=asset_root,
                                          legacy=custom_mod.load_registry(environ))
    line = snapshot.layer.lines.get(key)
    if line is None:
        raise DevError(f"--from-operator {key}: no valid operator declaration (claude-multi providers validate)")
    core = line.core_entry
    efforts = core["efforts"]
    wire = core["wire_model"]
    declared = int(core["context"]["declared_tokens"])
    try:
        schemas = snapshot.schemas or operator_mod.load_schemas(asset_root)
        evidence = operator_mod.load_evidence(environ, schemas)
    except (operator_mod.OperatorError, OSError):
        evidence = None
    summary = _evidence_summary(evidence, key, line.definition_digest)
    catalog_providers = docs["providers"]["providers"]
    if line.provider_id in catalog_providers:
        provider = catalog_providers[line.provider_id]
        prerequisites: list[str] = []
        if isinstance(efforts, list):
            gateway_effort = catalog_mod.effort_mode(provider) != "client"
            levels: Any = None if gateway_effort else list(efforts)
            if gateway_effort:
                prerequisites.append(
                    f"prerequisite: {key} is list-shaped but {line.provider_id} is a claude-compatible "
                    "gateway-effort provider; the catalog line needs an efforts {level: {selector, "
                    "proxy_contract}} map before check can pass")
        else:
            levels = {level: spec["proxy_contract"] for level, spec in efforts.items()}
        entry, more = _prefill_entry(docs, provider_id=line.provider_id, new_id=new_id, wire=wire,
                                     declared=declared, levels=levels, display=core["display"])
        prerequisites.extend(p for p in more if p not in prerequisites)
        notes = f"prefilled from operator {key} ({line.source}). {summary}. {PREFILL_REVIEW}"
        return "model", {"provider": line.provider_id, "entry": entry}, prerequisites, notes[:_NOTES_MAX]
    resolved = snapshot.layer.providers.get(line.provider_id)
    if resolved is None:
        raise DevError(f"--from-operator {key}: provider {line.provider_id} does not resolve")
    projection = {k: copy.deepcopy(v) for k, v in resolved.entry.items() if k != "origin"}
    projection["id"] = line.provider_id
    projection["support"] = "locally-validated-experimental"
    projection["support_note"] = (f"QUALIFY: reviewed support note (operator-declared in {resolved.file}; "
                                  "not yet reviewed)")
    transport = dict(projection["transport"])
    if transport.get("kind") == "direct-openai" and not catalog_mod.is_keyed_compat(projection):
        # Keyless LAN only: a keyed compat route keeps its bearer auth and
        # secret reference (never downgraded to keyless).
        transport["auth"] = {"kind": "none"}
    projection["transport"] = transport
    schema = strict_json.load(asset_root / "schemas" / "providers.schema.json")
    problems = schema_validate.validate(
        {"version": 1, "providers": {line.provider_id: {k: v for k, v in projection.items() if k != "id"}}},
        schema, "$")
    prerequisites = []
    if problems:
        prerequisites.append("prerequisite: the provider's T1 projection needs review: " + "; ".join(problems)[:400])
    if resolved.headers:
        prerequisites.append("prerequisite: its static headers are operator-only and not part of the catalog "
                             "provider shape; review whether the route needs them")
    # The model on a new catalog provider: selectors by the catalog-33 rules.
    shadow = copy.deepcopy(docs)
    shadow["providers"] = {**docs["providers"], "providers": {
        **docs["providers"]["providers"], line.provider_id: {k: v for k, v in projection.items() if k != "id"}}}
    shadow["models"] = {**docs["models"], "models": {**docs["models"]["models"], **{
        f"{new_id}-template": {**copy.deepcopy(core), "status": "new"}}}}
    levels = list(efforts) if isinstance(efforts, list) else {
        level: spec["proxy_contract"] for level, spec in efforts.items()}
    entry, more = _prefill_entry(shadow, provider_id=line.provider_id, new_id=new_id, wire=wire,
                                 declared=declared, levels=levels, display=core["display"])
    prerequisites.extend(more)
    contracts = sorted({spec["proxy_contract"] for spec in efforts.values()}) if isinstance(efforts, dict) else []
    claims = contracts or ["QUALIFY: reviewed contract claims (payload contracts this model needs)"]
    notes = (f"prefilled from operator {key} ({line.source}); provider draft for T2-only provider "
             f"{line.provider_id}. {summary}. {PREFILL_REVIEW}")
    return "provider", {"provider": projection, "model": entry, "contract_claims": claims}, prerequisites, \
        notes[:_NOTES_MAX]


def _prefill_command(flags: dict[str, Any], repo: Path | None) -> int:
    """``model add --from-registry`` / ``--from-operator``."""

    if "id" not in flags or flags["id"] is True:
        raise DevError("model add --from-registry/--from-operator requires --id KEY")
    new_id = state.check_name(str(flags["id"]))
    repo_root = repo if repo is not None else Path(".")
    raw = catalog_mod.load_raw(layout.checkout_resources(repo_root))
    docs = raw["docs"]
    _fresh_key_check(docs, new_id)
    name = state.check_name(str(flags.get("name", "draft")))
    drafts = _draft_store()
    if "from-registry" in flags:
        root = catalog_mod.registry_dir()
        if root is None:
            raise DevError("no pinned gateway registry in this build "
                           f"({catalog_mod.REGISTRY_DIR_ENV} unset, no registry/ in the resources)")
        registry = catalog_mod.load_pinned_registry(root)
        provider = flags.get("provider")
        provider_id, entry, prerequisites, notes = _registry_prefill(
            docs, registry, str(flags["from-registry"]), new_id,
            str(provider) if isinstance(provider, str) else None)
        draft = make_model_draft(name=name, provider=provider_id, entry=entry, notes=notes)
        source = f"registry {flags['from-registry']}"
        kind_note = None
    else:
        if "provider" in flags:
            raise DevError("--provider applies to --from-registry only")
        kind, payload, prerequisites, notes = _operator_prefill(
            docs, dict(os.environ), layout.checkout_resources(repo_root).resolve(), str(flags["from-operator"]), new_id)
        if kind == "model":
            draft = make_model_draft(name=name, provider=payload["provider"], entry=payload["entry"], notes=notes)
            kind_note = None
        else:
            draft = make_provider_draft(name=name, provider_profile=payload["provider"],
                                        model_entry=payload["model"], contract_claims=payload["contract_claims"],
                                        notes=notes)
            kind_note = (f"kind: provider draft — {payload['provider']['id']} is operator-declared (T2-only); "
                         "the draft carries its T1 projection, one model and explicit contract claims")
        source = f"operator {flags['from-operator']}"
    saved = drafts.save(name, _validate_draft(draft, _load_draft_schema(layout.checkout_resources(repo_root))))
    print(f"draft: {saved}")
    print(f"source: {source}")
    if kind_note:
        print(kind_note)
    for line in prerequisites:
        print(line)
    print(PREFILL_REVIEW)
    print("Fill every QUALIFY field, then:")
    print(f"  claude-multi-dev check {name}")
    print(PREFILL_PROMOTION)
    return 0


def _scaffold_model_entry(
    docs: dict[str, Any], *, like_id: str, new_id: str, wire_model: str
) -> dict[str, Any]:
    """Draft a v2 model line from a same-provider sibling (a scaffold).

    ``docs`` are RAW catalog docs (``catalog.load_raw``): models v2 and the
    retired map. Mechanical fields are inherited (effort shape and
    contracts, lead block, capabilities, roles, minimum_tested.claude_code);
    ``minimum_tested.cliproxyapi`` is the catalog's gateway baseline — a new
    line is first tested on the current gateway, not the sibling's.
    Selectors are derived from the documented shapes only — never by
    substring replacement: a gateway-effort level selector must read
    ``{gpt-multi-|claude-multi-}<like>-<level>[1m]?`` and becomes
    ``<prefix><new>-<level>`` with the same suffix; an OpenAI-compatible
    client selector must read ``claude-multi-<like>[1m]?``. Anything else is
    refused (write the entry with --from-json). Anthropic lines are refused:
    their canonical wire must be a passthrough route of the pool, which a
    model draft cannot add. Judgment fields become
    explicit QUALIFY markers the author fills before check: generation,
    display, qualification evidence, routing_note; validated_tokens is
    capped at a conservative bound and any user-attested bound is stripped.
    The line lands ``status: "new"`` (New · not admitted) with ``roles`` inherited
    unchanged — review them.
    """

    models = docs["models"]["models"]
    retired = docs.get("retired", {"retired": {}})["retired"]
    providers = docs["providers"]["providers"]
    if like_id not in models:
        raise DevError(
            f"--like model {like_id!r} is not in the catalog "
            f"(have: {', '.join(sorted(models))})"
        )
    if not catalog_mod.LINE_KEY.fullmatch(new_id):
        raise DevError(f"model id {new_id!r} must match ^[a-z0-9][a-z0-9-]*$")
    if new_id in models:
        raise DevError(f"model {new_id!r} already exists in the catalog")
    at_bases = {key.split("@", 1)[0] for key in retired if "@" in key}
    if new_id in retired or new_id in at_bases:
        raise DevError(
            f"model id {new_id!r} is a retired catalog key; retired keys are never reused"
        )
    like = models[like_id]
    provider = providers[like["provider"]]
    if provider["adapter"] == "cliproxy-oauth-claude-v1":
        raise DevError(
            "Anthropic generations need a route edit; use a provider-kind change "
            "or a direct catalog edit per AGENTS §5"
        )
    entry = copy.deepcopy(like)
    entry["id"] = new_id  # drafts key on entry.id; promote strips it
    entry["wire_model"] = wire_model

    def _cannot(selector: str) -> DevError:
        return DevError(
            f"cannot derive selectors: {selector!r} does not have the documented "
            f"shape for --like {like_id!r} — write the entry by hand with "
            "model add --from-json"
        )

    if isinstance(like["efforts"], list):
        match = re.fullmatch(
            "claude-multi-" + re.escape(like_id) + r"(\[1m\])?", like["selector"]
        )
        if match is None:
            raise _cannot(like["selector"])
        entry["selector"] = f"claude-multi-{new_id}{match.group(1) or ''}"
    else:
        for level, spec in entry["efforts"].items():
            match = re.fullmatch(
                r"(gpt-multi-|claude-multi-)"
                + re.escape(like_id)
                + "-"
                + re.escape(level)
                + r"(\[1m\])?",
                spec["selector"],
            )
            if match is None:
                raise _cannot(spec["selector"])
            spec["selector"] = f"{match.group(1)}{new_id}-{level}{match.group(2) or ''}"

    def _base(selector: str) -> str:
        return selector.removesuffix("[1m]")

    taken = {
        _base(selector)
        for line in models.values()
        for _level, selector, _contract in catalog_mod.line_selectors(line)
    }
    taken |= {_base(selector) for item in retired.values() for selector in item["selectors"]}
    taken |= {
        route["name"]
        for item in providers.values()
        for route in item["passthrough_routes"]
    }
    proposed = {_base(selector) for _l, selector, _c in catalog_mod.line_selectors(entry)}
    collision = proposed & taken
    if collision:
        raise DevError(
            f"selector collision with the catalog (live, retired or route): {sorted(collision)}"
        )
    entry["minimum_tested"] = {
        **like["minimum_tested"],
        "cliproxyapi": docs["gateway"]["gateway"]["cliproxyapi_baseline"],
    }
    entry["generation"] = "QUALIFY: generation"
    entry["display"] = f"QUALIFY: display name for {new_id}"
    entry["routing_note"] = "QUALIFY: routing guidance (when to prefer this model)"
    entry["status"] = "new"
    entry["registry_overlay"] = None
    entry.pop("output", None)  # a sibling's output provenance is not evidence for the new wire
    context = entry["context"]
    context.pop("user_reported_tokens", None)
    context["validated_tokens"] = min(
        context.get("validated_tokens", 0), catalog_mod.CUSTOM_VALIDATED_CAP
    )
    context["qualification"] = (
        "QUALIFY: context-bound evidence (provider doc or benchmark); "
        "unverified until an approval-gated live acceptance call."
    )
    return entry


def main(argv: list[str]) -> int:
    if not argv:
        print(DEV_HELP, end="")
        return 2
    if argv[0] in ("-h", "--help", "help"):
        print(DEV_HELP, end="")
        return 0
    if argv[0] in ("-v", "--version"):
        from . import identity

        print(identity.version_line("claude-multi-dev"))
        return 0
    command, rest = argv[0], argv[1:]
    if command == "repin":
        # Lazy: the re-pin flow loads the CLI's consent helpers.
        from .cli.commands import repin as repin_cmd

        positionals, flags = _parse_flags(rest)
        try:
            if positionals:
                raise DevError(repin_cmd.USAGE)
            return repin_cmd.repin(flags)
        except (errors.ClaudeMultiError, OSError) as exc:
            print(f"claude-multi-dev: {exc}", file=os.sys.stderr)
            return 2
    if command == "probe":
        # Lazy: a tracked module must not hard-import the dev-only probe
        # harness at top level.
        from . import probe as probe_mod

        head = rest[: rest.index("--")] if "--" in rest else rest
        tail = rest[rest.index("--") + 1:] if "--" in rest else []
        positionals, flags = _parse_flags(head)
        return probe_mod.probe_cli(positionals, flags, tail)
    positionals, flags = _parse_flags(rest)
    repo = Path(flags.get("repo", ".")) if "repo" in flags else None
    try:
        if command in ("review", "promote") or (
            command in ("model", "provider") and positionals[:1] == ["add"]
        ):
            sessions.check_state_marker(sessions.state_root())
        if command in ("model", "provider") and positionals[:1] == ["add"]:
            sources = [name for name in ("like", "from-json", "from-registry", "from-operator") if name in flags]
            if len(sources) > 1:
                raise DevError("choose one source: --like, --from-json, --from-registry or --from-operator")
            if command == "model" and sources and sources[0] in ("from-registry", "from-operator"):
                if flags[sources[0]] is True:
                    raise DevError(f"--{sources[0]} needs a value")
                return _prefill_command(flags, repo)
            drafts = _draft_store()
            kind = command
            if kind == "model" and "like" in flags:
                # Scaffold from a same-provider sibling: mechanical fields
                # inherited, judgment fields become QUALIFY markers.
                if "id" not in flags or "wire-id" not in flags:
                    raise DevError(
                        "model add --like requires --id NEWID and --wire-id WIRE"
                    )
                new_id = state.check_name(str(flags["id"]))
                repo_root = repo if repo is not None else Path(".")
                docs = catalog_mod.load_raw(layout.checkout_resources(repo_root))["docs"]
                entry = _scaffold_model_entry(
                    docs,
                    like_id=str(flags["like"]),
                    new_id=new_id,
                    wire_model=str(flags["wire-id"]),
                )
                name = state.check_name(str(flags.get("name", "draft")))
                draft = make_model_draft(
                    name=name,
                    provider=entry["provider"],
                    entry=entry,
                    notes=str(
                        flags.get(
                            "notes",
                            f"scaffolded from {flags['like']}; fill every QUALIFY field",
                        )
                    ),
                )
                saved = drafts.save(
                    name, _validate_draft(draft, _load_draft_schema(layout.checkout_resources(repo_root)))
                )
                print(f"draft: {saved}")
                print("fill every QUALIFY field, then:")
                print(f"  claude-multi-dev check {name}")
                return 0
            if "from-json" not in flags:
                raise DevError(f"{kind} add requires --from-json FILE")
            spec = strict_json.load(Path(flags["from-json"]))
            if not isinstance(spec, dict):
                raise DevError("spec file must contain a JSON object")
            required = ("provider", "entry") if kind == "model" else (
                "provider",
                "model",
                "contract_claims",
            )
            for field in required:
                if field not in spec:
                    raise DevError(f"spec file is missing required field {field!r}")
            name = state.check_name(str(flags.get("name", "draft")))
            if kind == "model":
                draft = make_model_draft(
                    name=name,
                    provider=spec["provider"],
                    entry=spec["entry"],
                    notes=spec.get("notes"),
                )
            else:
                draft = make_provider_draft(
                    name=name,
                    provider_profile=spec["provider"],
                    model_entry=spec["model"],
                    contract_claims=spec["contract_claims"],
                    notes=spec.get("notes"),
                )
            path = drafts.save(name, _validate_draft(draft, _load_draft_schema(
                layout.checkout_resources((repo or Path(".")).resolve())
            )))
            print(f"draft saved: {path}")
            return 0
        if command == "drafts":
            if positionals != ["migrate"]:
                raise DevError("usage: claude-multi-dev drafts migrate [--apply] [--repo PATH]")
            return _drafts_migrate(flags, repo)
        if command == "check":
            drafts = _draft_store()
            if not positionals:
                raise DevError("check requires a DRAFT name")
            draft = _validate_draft(drafts.load(positionals[0]), _load_draft_schema(
                layout.checkout_resources(verify_repo(repo or Path(".")))
            ))
            result = check_draft(draft, repo=verify_repo(repo or Path(".")))
            print(f"check ok: draft {result.draft_hash}")
            for build in result.builds:
                print(f"  build: {build.get('cmd', ['?'])[-1]} -> {build.get('returncode', build.get('skipped'))}")
            for line in result.registry:
                print(f"  {line}")
            return 0
        if command == "review":
            drafts = _draft_store()
            if not positionals:
                raise DevError("review requires a DRAFT name")
            verified = verify_repo(repo or Path("."))
            draft = _validate_draft(
                drafts.load(positionals[0]), _load_draft_schema(layout.checkout_resources(verified))
            )
            record = review_draft(draft, draft_name=positionals[0], repo=verified)
            out = drafts.root / f"{positionals[0]}.review.json"
            sessions.check_state_marker(drafts.root.parent)
            state.atomic_write(out, strict_json.canonical_file_bytes(record))
            print(f"review recorded: {out}")
            print(record["results"]["diff"])
            return 0
        if command == "promote":
            drafts = _draft_store()
            if not positionals:
                raise DevError("promote requires a DRAFT name")
            mode = resolve_promote_mode(
                repo if "repo" in flags else None, flags.get("patch-output")
            )
            if mode == "patch":
                verified = verify_repo(repo or Path("."))
                draft = _validate_draft(
                    drafts.load(positionals[0]), _load_draft_schema(layout.checkout_resources(verified))
                )
                path, retargets = promote_patch_output(
                    draft, repo=verified, output=Path(flags["patch-output"]),
                    running_lines=running_catalog_lines(),
                )
                print(f"patch written: {path}")
                for note in retargets:
                    print(f"note: {note}")
                return 0
            verified = verify_repo(repo)
            draft = _validate_draft(
                drafts.load(positionals[0]), _load_draft_schema(layout.checkout_resources(verified))
            )
            record = strict_json.loads(
                state.read_private(drafts.root / f"{positionals[0]}.review.json"),
                limits=strict_json.JSONLimits(max_string=4 * 1024 * 1024),
            )
            result = promote_draft(
                draft,
                draft_name=positionals[0],
                repo=verified,
                review_record=record,
                drafts_root=drafts.root,
                running_lines=running_catalog_lines(),
            )
            print(f"promoted: {result['applied']}")
            for note in result["retargets"]:
                print(f"note: {note}")
            print("remaining runbook (promotion writes trusted JSON only):")
            print("  1. a removed/renamed line or Anthropic generation move has its retired.json entry")
            print("  2. full host suite + sandbox green; no golden or test pin moved")
            print("  3. activate: install a build of the checkout through your channel (it reloads the gateway;")
            print("     a gateway binary or gateway.json change restarts it)")
            print("     or: claude-multi-proxy init (verifies reload; prints restart command if needed)")
            print("  4. verify /v1/models serves it; one consent-gated live call")
            return 0
        if command == "smoke-test":
            if not positionals:
                raise DevError("smoke-test requires a MODEL name")
            outcome = smoke_test(
                positionals[0], allow_provider_call=bool(flags.get("allow-provider-call"))
            )
            print(outcome.get("guidance", outcome["status"]))
            return 2 if outcome["status"] == "refused" else 0
        raise DevError(f"unknown command {command!r}")
    except (
        errors.ClaudeMultiError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        OSError,
    ) as exc:
        print(f"claude-multi-dev: {exc}", file=os.sys.stderr)
        return 2
