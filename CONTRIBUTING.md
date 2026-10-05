# Contributing to claude-multi

Thanks for helping. This guide covers setting up a checkout, running the
tests, building the gateway and writing commits. The architecture, the
invariants every change must keep and the workflow for common changes are
in [`AGENTS.md`](AGENTS.md); read it before changing code.

## What you need

- Python 3.11 or newer. The launcher uses the standard library only.
- git.
- For the gateway build: nothing more. `tools/build.py` fetches the pinned
  upstream source and the official Go toolchain for your host and checks
  every download against the hash in `gateway/UPSTREAM.json`.
- For the gateway's race gates (Linux): a C compiler, `gcc` or `clang`
  (the Go race detector needs cgo). Without one, run the portable gates
  only (`--gates portable`).
- Optional: Nix. The flake builds the same package and gateway and runs the
  sandbox checks; nothing requires it.

## Set up a checkout

```bash
git clone https://github.com/nkoturovic/claude-multi
cd claude-multi
python3 -m venv ~/.venvs/claude-multi
~/.venvs/claude-multi/bin/pip install -e .
~/.venvs/claude-multi/bin/claude-multi --version
```

Keep the virtual environment outside the checkout, as above: the
credential check reads every file of the working tree, ignored files
included, and the packages a virtual environment holds are not this
repository's to vet. (The naming checks read only the repository's own
files — what git tracks or would add — so build output and an ignored
`.venv/` never trip them.)

The editable install puts `claude-multi`, `claude-multi-proxy` and
`claude-multi-dev` in the virtual environment's
`bin/`; they run the code in `src/` and name the `source` channel, as the
launchers in `bin/` do (`bin/claude-multi` works without any install).
`claude-multi-dev`, the maintainers' catalog tool, runs only from a source
checkout (a wheel installs it too); release bundles and the Nix package
install `claude-multi` and `claude-multi-proxy`.
The tests need no install: `python3 tools/test.py` runs them with any
Python 3.11 or newer, the environment's own included.
The build backend is setuptools, pinned in `pyproject.toml`
(`[build-system]`). Offline, install that version first and add
`--no-build-isolation`.

Layout:

| Path | What |
| --- | --- |
| `src/claude_multi/` | the launcher package (standard library only) |
| `src/claude_multi/data/` | its runtime resources: `version.json`, the catalog, schemas, presets, examples, the service spec, the generated gateway contract and model registry |
| `bin/` | the source-checkout launchers |
| `docs/` | the user documents (installed with the package) |
| `gateway/` | the gateway recipe (`UPSTREAM.json`), its ordered patches, licences and SBOMs |
| `packaging/` | the installers, the bundled-runtime pins, their licence inventory and texts (`licenses/python/`) and the product description (bundle targets, launchers) |
| `tools/` | `build.py` (gateway and release builds), `release.py` (the release steps), `test.py` (the test runner), repository scripts |
| `nix/`, `flake.nix` | the optional Nix package, gateway derivations and development shell |
| `tests/` | the offline test suite, its fixtures and goldens |

## Run the tests

```bash
python3 tools/test.py                    # the whole suite
python3 tools/test.py tests.test_scope   # named modules
python3 tools/test.py --tier fast        # quicker iteration (never a gate)
python3 tools/test.py --claude PATH ...  # the real-client lanes, with the pinned Claude Code
PYTHONPATH=src:tests python3 tests/bless.py --check  # goldens that would change
python3 tools/docs_gen.py --check        # the generated documentation is current
git diff --check                         # no whitespace errors
```

The real-client lanes need the exact Claude Code version the release pins:
`--claude PATH` checks a file against the pin and copies it into the
suite's private HOME. Without it, a lane that needs it is reported as not
runnable and the run fails.

The installer, update and release tests build small fake releases
(`tests/_fake_release.py`: bundles carrying this checkout's package, signed
with throwaway keys) and run the real `packaging/install.sh` and the
installed `claude-multi update` against them in a temporary HOME. The CI
workflows in `.github/workflows/` are written here and checked as text by
`tests/test_release_ci.py`, which also runs the journey script
`.github/scripts/journey.sh` on fake releases. On a real bundle the
journey also needs the pinned Claude Code (`JOURNEY_CLIENT=PATH`, or
`fetch`) for its managed turn against a loopback fixture provider; run it
in a fresh HOME inside a network namespace (for example
`bwrap --unshare-net`), never against your own installation.

`tools/test.py` gives the suite a private temporary HOME and refuses any
connection to a live gateway port, so a run never reads or changes your
own claude-multi or Claude Code state and never calls a provider. With
Nix, the same suite also runs in the sandbox:
`nix build .#checks.x86_64-linux.claude-multi`.

Goldens (expected outputs under `tests/goldens/`) change only through
`PYTHONPATH=src:tests python3 tests/bless.py`; with `--check` it lists what
would change and writes nothing. Review every golden diff: it is a
behaviour change.

Parts of the documentation are generated from the code: the command
reference, the task index, the cheatsheet's tables and the model,
profile and provider lists, each between `<!-- generated: … -->` and
`<!-- end of generated: … -->` markers. Change the code (or
`tools/docs_gen.py`), never the region, then run
`python3 tools/docs_gen.py`; `--check`, which CI runs, fails while a
region is stale.

The hygiene tests also check the tree for credential-looking values,
personal identifiers and references to internal tracking ids
(`tests/test_hygiene.py`); user-visible text (messages, help, packaged data
and goldens) also names no earlier release line or planning document.

Every command, option, `claude-multi-proxy` command, `/cm` verb and TUI
action needs a row in `src/claude_multi/surface_matrix.py` (its surfaces,
or the reason one has none); a value-taking option that only supplies an
input of its command's operation may instead be listed in
`OPTION_ARGUMENTS` with the input it supplies. Each TUI path names a
dispatch test: one that presses the action's key and asserts what it does.
`tests/test_surface_matrix.py` fails until all of this holds.

## Credentials and local files

Keep credentials outside the checkout: claude-multi reads its provider
keys from `~/.config/claude-multi/` (or the key file you point it at),
never from the repository. The tree's credential check scans every file of
the working tree, ignored files included, so a key file left in a checkout
fails the suite; `.gitignore` only keeps one from being committed by
accident. Tests use generated or placeholder values (`dummy`, `fixture`,
`example`).

## Build the gateway

```bash
python3 tools/build.py gateway --help              # the steps and options
python3 tools/build.py gateway --target host       # fetch, vendor, patch, build, inspect for this host
python3 tools/build.py gateway gates               # the Go test gates (race gates need gcc or clang)
python3 tools/build.py gateway gates --gates portable   # the gates that need no C compiler
```

A gateway patch is a file under `gateway/patches/` listed, in order and
with its sha256, in `gateway/UPSTREAM.json`; a patch basename is an
identity and never changes. After a patch change, regenerate the checked-in
contract (`python3 tools/build.py gateway contract`).

## Build a release

```bash
python3 tools/build.py bundle                     # this host's bundle into dist/release
python3 tools/build.py release --out dist/release # every bundle, MANIFEST.json, SHA256SUMS, installers
python3 tools/build.py release --help             # inputs, options, compare and repro
```

The build fetches the pinned Python runtimes (`packaging/python-runtimes.json`)
and the gateway inputs by hash, ships each runtime's library licences from
`packaging/licenses/python/` (regenerate them with
`python3 tools/_build/runtime_licenses.py derive --archives DIR --versions FILE`
when the runtime pin
moves; `check` validates them), builds the gateway (or takes an earlier
build with `--gateway-dist`) and assembles one bundle per target; the
bundle for your own platform is run before it is archived. Two builds of
the same commit give the same bundle contents (`release compare`). A local
build installs like a release:
`sh dist/release/install.sh --from-dir dist/release --allowed-signers FILE`
with `SHA256SUMS` signed by a key of your own. Releasing is
`python3 tools/release.py preflight|build|reproduce|sign|verify-draft|publish`
(`python3 tools/release.py --help`); the release key never leaves its
maintainer.

`--test-build` records `test_build: true` in the checksummed
`MANIFEST.json`, even when production trust and release URLs are present.
Such a build is refused by `sign`, production-trust `verify-draft` and
`publish`; `verify-draft --allowed-signers FILE` remains available for
explicit test-key verification.

## Change the catalog

Catalog edits go to `src/claude_multi/data/catalog/` in a checkout, through
`claude-multi-dev` (draft, check, review, promote) for new models and
providers; `claude-multi-dev --help` shows the tracks. The behavioural tests
run on a frozen fixture catalog, so a catalog change does not move them.
`catalog_version` (in `version.json`) moves once per release line: the
first catalog change after a release bumps it, and later changes on that
line keep the number (1.0.0 keeps 37) and update only the digests and
goldens they move.

## Write a commit

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>

<body>
```

- `type`: one of `feat`, `fix`, `refactor`, `perf`, `test`, `docs`,
  `build`, `ci`, `chore`.
- `scope`: the product area (`gateway`, `setup`, `tui`, `profiles`,
  `build`, `pin`, …).
- The summary is imperative, lower case, at most 72 characters, with no
  trailing period, and says what changed for users or maintainers.
- The body, wrapped at 72 columns, says what is done and why: user-visible
  behaviour, compatibility notes and anything deliberately left out.
- `!` after the scope and a `BREAKING CHANGE:` footer mark a real breaking
  change only.

Code comments explain the reason for the code as it is. They never refer
to issue, plan or review identifiers, or to how the code used to be.

## Report a problem

Open an issue with:

- what you did, what you expected and what happened;
- `claude-multi --version` (the launcher, catalog, gateway and Claude Code
  versions; a missing component says unavailable);
- `claude-multi doctor --json`: one JSON document built for sharing. Its
  environment block is an allowlist: folders are reported as present or
  not, never as paths from your home; the terminal type is generalized;
  custom hosts and proxy values are left out; no secret is printed.

Never attach API keys or the key file, sign-in files under
`~/.local/share/claude-multi/`, the gateway's `config.yaml` or `api-key`,
transcripts, an environment dump, or the gateway's log files: they can
name account files and credential indexes (`claude-multi gateway logs`
shows them redacted). Security problems go through [`SECURITY.md`](SECURITY.md).

## Compatibility window

Each release pins one Claude Code version and one gateway version
(`src/claude_multi/data/catalog/native-contract.json`,
`gateway/UPSTREAM.json`). A newer Claude Code reaches users only through a
claude-multi release that re-pins it (`claude-multi-dev repin` from a
source checkout, with its evidence run); re-pins ship as patch releases.

## Before you open a pull request

- `python3 tools/test.py` passes, and
  `PYTHONPATH=src:tests python3 tests/bless.py --check` lists no golden
  you did not mean to change.
- `python3 tools/docs_gen.py --check` and `git diff --check` are clean.
- New behaviour has a test; a test that needs a real provider or the real
  Claude Code binary is opt-in and says so when it skips.
- User-visible changes are in `docs/` and in `CHANGELOG.md`.

Security problems go through [`SECURITY.md`](SECURITY.md), not an issue.
