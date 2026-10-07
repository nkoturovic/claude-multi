"""Regenerate every checked-in golden file under ``tests/goldens/``.

Usage (from the repository root)::

    PYTHONPATH=src:tests python3 tests/bless.py          # write + prune
    PYTHONPATH=src:tests python3 tests/bless.py --check  # dry run, writes nothing

``--check`` generates the same bytes, writes and deletes
nothing, lists every golden that would be written (``would change`` /
``would add``) or pruned (``would prune``), and exits 1 if there is any such
file, 0 when the tree is clean.

The 2.x ``tests/goldens/default/**`` goldens (durable argv, environment,
lead appendix and scope tree of the deleted 2.x compile path) and their
writer were removed with that path; running bless prunes any
leftover copy. ``tests/goldens/v2/{managed,direct}/scope/`` are the 3.0 v2 scope
trees compiled by ``scope.compile_lineup_scope`` from the fixture seeds
``balanced`` and ``direct``: ``settings.json``, the agent files,
``lineup.md``, ``lineup.gen``, ``lead-set.json`` and the ``/cm`` skill;
``tests/goldens/v2/{managed,direct}/{argv-fresh,argv-resume,env}.json`` and
``lead-appendix.md`` are the matching ``compiler.compile_lineup_launch``
argv, process environment and lead appendix.
``tests/goldens/shim/claude-multi-hook`` (the 2.x lifecycle hook shim,
frozen) and ``tests/goldens/shim/claude-multi-hook-3`` (the protocol-3 shim)
are the two shim texts for the state root ``/state`` and the fixed
launcher ``/nix/store/fixture/bin/claude-multi``.
``tests/goldens/v2/managed/notice/{startup,resume,fork,prompt}.json``
are the exact protocol-3 hook stdout of the lineup notice
(``hooks.notice_text``) over the managed golden scope files.
``tests/goldens/v4/migrate/*.json`` are ``migrate.convert_record``
records of the synthetic 2.x inputs in ``tests/test_migrate.py``,
``tests/goldens/v4/migrate/dry-run.{txt,json}`` the ``migrate --dry-run``
report over a temp state holding them (state root shown as ``/state``), and
``tests/goldens/v4/restore/*.json`` the ``sessions.restore_overlay`` of a
scripted migrate -> resume -> epoch bump -> relink sequence.
``tests/goldens/v4/records/fresh-{balanced,direct}.json`` are the
v4 records a fresh launch of the fixture ``balanced`` / ad-hoc direct ``sol``
commits (launch seam, fixed UUID and clock, cwd ``/project``), and
``resume-follow-diff.txt`` the stderr diff of a following session
resumed after a local profile edit.
``tests/goldens/v4/lineup/*.txt`` are the exact stdout of
``claude-multi lineup --session <id> '<request>'`` on a fixture ``balanced``
session (fixed UUID and clock): ``show``, the live ``set``/``unset``/
``profile`` applies, the pending relaunch of ``profile direct`` and
``direct sol``, and the skill-mode refusal of a split argument.
``tests/goldens/v4/profile-migrate/{dry-run,refused,apply,rerun}.txt``
are the exact ``claude-multi profile migrate`` reports over the synthetic
preset directory of ``tests/test_migrate_profiles.py`` (test-local tables,
config root shown as ``/config``): the dry run and the refused ``--apply``
over the full directory, then ``--apply`` and its idempotent rerun without
the two blocking presets.
``tests/goldens/tui/{models,providers,settings}-80x24.txt`` are one
80x24 ``MONO_PALETTE`` frame of each 3.0 screen over a hermetic fixture
runtime (``tests/_tui_render.GOLDENS``: fixed clock, injected seams, no
pinned registry, no temp path drawn).
``tests/goldens/v4/help/*.txt`` are the three public command help
texts at 100 columns with color disabled and doc paths shown by role.
``tests/goldens/render/gateway-default.yaml`` is the deterministic gateway
YAML, rendered with exactly the inputs ``tests/test_render.py`` uses (its
``_render`` helper is the single source of those inputs);
``tests/goldens/render/gateway-operator-compat.yaml`` is the keyed
openai-compatible render of ``tests/test_openai_compat_keyed.py``'s
``keyed_render_golden_bytes`` (fixture docs, audit opened in memory, dummy
values); ``tests/goldens/render/continuity-seed.json`` is the fixture's continuity
seed set (``continuity.seed_only``, canonical pretty bytes, ``state_root``
null — what a render without a state root persists). Every file is a
pure function of the frozen fixture asset root (``tests/fixtures/assets``)
and the fixed session UUID below — no timestamps, no randomness, no machine
paths — so regeneration is stable across checkouts.

Bless owns the whole ``tests/goldens/`` tree: after writing, it deletes
every file there that it did not write in this run and prints each one as
``pruned <path>``, so a renamed artifact never leaves a stale
golden behind. It never touches anything outside ``tests/goldens/``.

Run this after any intentional compiler/scope/render change and review the
diff (including any pruned paths) before committing; the golden tests in
``tests/test_compiler_v2.py``, ``tests/test_scope_v2.py``,
``tests/test_render.py`` and the v4 modules assert these exact bytes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from claude_multi import (
    catalog,
    compiler,
    continuity,
    hooks,
    profile,
    scope,
    settings,
    strict_json,
)

from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _layout import REPO_ROOT
from test_render import GOLDEN as RENDER_GOLDEN
from test_render import _render
from test_migrate import dry_run_golden_files, migrate_golden_files, restore_golden_files
from test_launch_v4 import fresh_record_golden_files, resume_follow_diff_golden
from test_lineup import lineup_golden_files
from test_migrate_profiles import profile_migrate_golden_files
from _tui_render import tui_golden_files
from test_openai_compat_keyed import KEYED_RENDER_GOLDEN, keyed_render_golden_bytes
from test_help import help_golden_files
from test_openrouter_discovery import listing_golden


# Goldens are generated from the frozen fixture asset root, never from
# the shipped catalog: a shipped model change must not move golden bytes.
CATALOG_ROOT = FIXTURE_ROOT
FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
SCOPE_DIR = Path("/state") / "scopes" / FIXED_SESSION
CONTINUITY_SEED_GOLDEN = GOLDENS_ROOT / "render" / "continuity-seed.json"

# The v2 compile path: fixture seeds ``balanced`` (managed) and ``direct``,
# generation 1, epoch 0, the protocol-3 hook shim and the token helper under
# the state root ``/state``.
V2_GOLDENS = GOLDENS_ROOT / "v2"
V2_SEEDS = {"managed": "balanced", "direct": "direct"}
V2_STATE_ROOT = Path("/state")
V2_HOOK_COMMAND = "/state/bin/claude-multi-hook-3"
V2_TOKEN_HELPER = "/state/bin/claude-multi-gateway-token"
V2_GENERATION = 1

# The lineup notice hook stdout over the managed golden scope files.
NOTICE_GOLDENS = V2_GOLDENS / "managed" / "notice"
NOTICE_CASES = {
    "startup": ("SessionStart", "startup"),
    "resume": ("SessionStart", "resume"),
    "fork": ("SessionStart", "fork"),
    "prompt": ("UserPromptSubmit", None),
}

# The hook shims, written for the state root ``/state`` and a fixed
# fake store path (the shim is the one file that names the launcher).
SHIM_GOLDENS = GOLDENS_ROOT / "shim"

# Migrated v4 records, the migrate report, and restore-2x overlays
# (fixture catalog, fixed UUIDs, clock 2026-09-25T00:00:00Z, state /state).
V4_GOLDENS = GOLDENS_ROOT / "v4"
SHIM_COMMAND = "/nix/store/fixture/bin/claude-multi"

# The 3.0 TUI screens (tests/_tui_render.py).
TUI_GOLDENS = GOLDENS_ROOT / "tui"


def v2_inputs(root: Path = CATALOG_ROOT):
    """``(bundle, LineupCatalog, Effective)`` of the golden v2 compiles."""

    bundle = catalog.load_catalog(root)
    lcat = profile.LineupCatalog.from_docs(bundle.docs)
    eff = settings.effective(
        {"version": settings.SETTINGS_DATA_VERSION},
        provider_ids=lcat.providers,
        line_keys=lcat.lines,
    )
    return bundle, lcat, eff


def v2_scope_plan(kind: str) -> scope.ScopePlan:
    """The golden v2 scope plan of ``kind`` (``managed`` or ``direct``)."""

    bundle, lcat, eff = v2_inputs()
    lineup = profile.resolve(bundle.seed_profiles[V2_SEEDS[kind]], lcat, effective=eff)
    return scope.compile_lineup_scope(
        lineup,
        lcat,
        eff,
        bundle.prompt_bodies,
        scope.catalog_meta_v2(bundle.docs),
        lineup_generation=V2_GENERATION,
        managed_id=FIXED_SESSION,
        hook_command=V2_HOOK_COMMAND,
        launch_epoch=0,
        token_helper_command=V2_TOKEN_HELPER,
    )


V2_PASSTHROUGH = ["--verbose"]


def v2_launch(kind: str, action=None) -> compiler.CompileResult:
    """The golden v2 launch plan of ``kind`` (fresh unless ``action`` is given)."""

    bundle, lcat, eff = v2_inputs()
    lineup = profile.resolve(bundle.seed_profiles[V2_SEEDS[kind]], lcat, effective=eff)
    return compiler.compile_lineup_launch(
        docs=bundle.docs,
        prompt_bodies=bundle.prompt_bodies,
        lineup=lineup,
        effective=eff,
        session_action=action or compiler.build_fresh(FIXED_SESSION),
        lineup_generation=V2_GENERATION,
        state_root=V2_STATE_ROOT,
        scope_dir=SCOPE_DIR,
        hook_command=V2_HOOK_COMMAND,
        token_helper_command=V2_TOKEN_HELPER,
        launch_epoch=0,
        passthrough=V2_PASSTHROUGH,
    )


def v2_appendix(result: compiler.CompileResult) -> str:
    """The generated appendix of a v2 lead prompt (``cm-lead`` body + "\\n" + appendix)."""

    body = catalog.load_catalog(CATALOG_ROOT).prompt_bodies["cm-lead"].decode("utf-8") + "\n"
    if not result.lead_prompt.startswith(body):
        raise SystemExit("bless: the v2 lead prompt does not start with the cm-lead body")
    return result.lead_prompt[len(body):]


def v2_launch_files(kind: str) -> dict[str, bytes]:
    """The golden v2 launch files of ``kind``: name -> bytes."""

    fresh = v2_launch(kind)
    resume = v2_launch(kind, compiler.build_resume(FIXED_SESSION))
    appendix = v2_appendix(fresh)
    return {
        "argv-fresh.json": strict_json.canonical_file_bytes(fresh.argv),
        "argv-resume.json": strict_json.canonical_file_bytes(resume.argv),
        "env.json": strict_json.canonical_file_bytes(
            {"set": fresh.env_set, "unset": list(fresh.env_unset)}
        ),
        "lead-appendix.md": appendix.encode("utf-8"),
    }


def v2_scope_files(plan: scope.ScopePlan) -> dict[str, bytes]:
    """Every golden file of a v2 scope plan: relpath -> bytes."""

    files = {"settings.json": strict_json.canonical_file_bytes(plan.settings)}
    files.update(plan.agent_files)
    files.update(plan.other_files)
    return files


def shim_files() -> dict[str, bytes]:
    """The golden hook shim texts: file name -> bytes."""

    return {
        scope.HOOK_SHIM_RELATIVE.name: scope.hook_shim_text(SHIM_COMMAND),
        scope.HOOK_SHIM_V3_RELATIVE.name: scope.hook_shim_v3_text(
            V2_STATE_ROOT, SHIM_COMMAND
        ),
    }


def notice_files() -> dict[str, bytes]:
    """The golden notice hook outputs: file name -> exact stdout bytes.

    ``hooks.notice_text`` over the managed golden ``lineup.md``/``lineup.gen``
    with the recorded lead of its ``lead-set.json`` (the resume preface).
    """

    plan = v2_scope_plan("managed")
    lineup_md = plan.other_files[scope.LINEUP_MD].decode("utf-8")
    gen_line = plan.other_files[scope.LINEUP_GEN].decode("ascii").rstrip("\n")
    lead = strict_json.loads(plan.other_files[scope.LEAD_SET_JSON])["lead"]
    files: dict[str, bytes] = {}
    for name, (event, source) in NOTICE_CASES.items():
        text = hooks.notice_text(lineup_md, gen_line, event=event, source=source, lead=lead)
        files[f"{name}.json"] = hooks.notice_response(event, text).encode("utf-8")
    return files


# Every golden this run generates, in generation order: resolved absolute
# path under GOLDENS_ROOT -> bytes. Built by ``plan()``; written by ``apply``
# or compared by ``check``.
_PLAN: dict[Path, bytes] = {}


def _write(path: Path, data: bytes) -> None:
    path = path.resolve()
    if not path.is_relative_to(GOLDENS_ROOT):
        raise SystemExit(f"bless: refusing to write outside {GOLDENS_ROOT}: {path}")
    _PLAN[path] = bytes(data)


def _label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _stale(root: Path, keep: set[Path]) -> tuple[list[Path], list[Path]]:
    """Files and then-empty directories under ``root`` a prune would delete.

    ``keep`` holds the resolved paths this run writes. Symlinks are never
    kept. Directories are listed deepest first.
    """

    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_symlink() or not path.is_dir()
        if path.is_symlink() or path.resolve() not in keep
    ]
    doomed = set(files)
    directories: list[Path] = []
    for directory in sorted(
        (entry for entry in root.rglob("*") if entry.is_dir() and not entry.is_symlink()),
        key=lambda entry: len(entry.parts),
        reverse=True,
    ):
        if all(child in doomed for child in directory.iterdir()):
            directories.append(directory)
            doomed.add(directory)
    return files, directories


def _prune(keep: set[Path]) -> list[Path]:
    """Delete every golden file this run did not write.

    Scoped strictly to ``GOLDENS_ROOT``; directories left empty are removed
    too. Returns the pruned files so the caller can report them.
    """

    files, directories = _stale(GOLDENS_ROOT, keep)
    for path in files:
        path.unlink()
        print(f"pruned {_label(path)}")
    for directory in directories:
        directory.rmdir()
        print(f"pruned {_label(directory)}/")
    return files


def apply(planned: dict[Path, bytes]) -> list[Path]:
    """Write ``planned`` into ``GOLDENS_ROOT`` and prune; returns the pruned files."""

    for path, data in planned.items():
        if not path.is_relative_to(GOLDENS_ROOT):
            raise SystemExit(f"bless: refusing to write outside {GOLDENS_ROOT}: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {_label(path)}")
    return _prune(set(planned))


def check(planned: dict[Path, bytes], root: Path = GOLDENS_ROOT) -> list[str]:
    """What ``apply`` would do to the goldens tree at ``root``; writes nothing.

    ``planned`` paths are relative to ``GOLDENS_ROOT`` and are compared at the
    same relative path under ``root`` (a copied tree in tests). Returns one
    ``would change|would add|would prune <path>`` line per file (and per
    directory a prune would empty); an empty list means the tree is clean.
    """

    root = Path(root)
    targets = {root / path.relative_to(GOLDENS_ROOT): data for path, data in planned.items()}
    lines: list[str] = []
    for target, data in targets.items():
        if target.is_symlink() or target.is_dir():
            lines.append(f"would change {_label(target)}")
        elif not target.exists():
            lines.append(f"would add {_label(target)}")
        elif target.read_bytes() != data:
            lines.append(f"would change {_label(target)}")
    if root.is_dir():
        keep = {target.resolve() for target in targets if not target.is_symlink()}
        files, directories = _stale(root, keep)
        lines.extend(f"would prune {_label(path)}" for path in files)
        lines.extend(f"would prune {_label(directory)}/" for directory in directories)
    return lines


def _bless_render() -> None:
    """The gateway render golden, from test_render's exact ``_render`` inputs."""

    _write(RENDER_GOLDEN, _render().yaml.encode("utf-8"))
    # The keyed openai-compatible render (fixture docs with the
    # audit opened in memory; tests/test_openai_compat_keyed.py builder).
    _write(KEYED_RENDER_GOLDEN, keyed_render_golden_bytes())
    _write(
        CONTINUITY_SEED_GOLDEN,
        strict_json.pretty_file_bytes(
            continuity.seed_only(catalog.load_catalog(CATALOG_ROOT))
        ),
    )


def plan() -> dict[Path, bytes]:
    """Generate every golden in memory (no filesystem writes)."""

    _PLAN.clear()
    # The v2 scope trees (managed = fixture balanced, direct).
    for kind in V2_SEEDS:
        v2_scope = V2_GOLDENS / kind / "scope"
        for relpath, data in sorted(v2_scope_files(v2_scope_plan(kind)).items()):
            _write(v2_scope.joinpath(*relpath.split("/")), data)
        # Argv, process env and the lead appendix of the same seeds.
        for name, data in v2_launch_files(kind).items():
            _write(V2_GOLDENS / kind / name, data)

    # Both hook shim texts (the 2.x one frozen; protocol 3).
    for name, data in shim_files().items():
        _write(SHIM_GOLDENS / name, data)

    # The lineup notice hook outputs (managed scope).
    for name, data in notice_files().items():
        _write(NOTICE_GOLDENS / name, data)

    # v4 record conversions, the migrate report, restore overlays.
    for name, data in migrate_golden_files().items():
        _write(V4_GOLDENS / "migrate" / name, data)
    for name, data in dry_run_golden_files().items():
        _write(V4_GOLDENS / "migrate" / name, data)
    for name, data in restore_golden_files().items():
        _write(V4_GOLDENS / "restore" / name, data)
    # fresh-launch records (launch seam) and the follow diff.
    for name, data in fresh_record_golden_files().items():
        _write(V4_GOLDENS / "records" / name, data)
    _write(V4_GOLDENS / "records" / "resume-follow-diff.txt", resume_follow_diff_golden())
    # The exact stdout of `lineup --session …`.
    for name, data in lineup_golden_files().items():
        _write(V4_GOLDENS / "lineup" / name, data)

    # The `profile migrate` reports (dry run, refused, apply, rerun).
    for name, data in profile_migrate_golden_files().items():
        _write(V4_GOLDENS / "profile-migrate" / name, data)

    # The 3.0 screens, one 80x24 frame each.
    for name, data in tui_golden_files().items():
        _write(TUI_GOLDENS / name, data)

    for name, data in help_golden_files().items():
        _write(V4_GOLDENS / "help" / name, data)

    _write(GOLDENS_ROOT / "discovery/openrouter-listing.txt", listing_golden().encode())

    # The gateway render golden.
    _bless_render()
    return dict(_PLAN)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = args == ["--check"]
    if args and not dry_run:
        print("usage: bless.py [--check]", file=sys.stderr)
        return 2
    planned = plan()
    if dry_run:
        lines = check(planned)
        for line in lines:
            print(line)
        if lines:
            print(
                f"bless --check: {len(lines)} golden path(s) would change or be "
                "pruned; run tests/bless.py and review the diff"
            )
            return 1
        print(f"bless --check: {len(planned)} golden files clean")
        return 0
    pruned = apply(planned)
    print(f"bless: wrote {len(planned)} golden files, pruned {len(pruned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
