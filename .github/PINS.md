# CI pins

Every third-party action, tool and container image the workflows in
`workflows/` use is pinned, and each pin below was checked against its
upstream with read-only metadata: an action's or tool's release tag
resolved with `git ls-remote <repository> refs/tags/<tag>` (and the
release with `gh api repos/<owner>/<repo>/releases/tags/<tag>`), a Python
or Go tool's version on its package index, a container image's index
digest read from its registry. `tests/test_release_ci.py` keeps this file
and the workflows in agreement: a pin that changes in a workflow needs its
row here, re-verified, with the new date.

Each pin is the newest release of its project on the day it was verified
(setuptools excepted: it follows the build backend `pyproject.toml` pins),
and every JavaScript action runs on Node.js 24 (`runs.using: node24` in
the pinned commit's `action.yml`; a composite action's own steps are
pinned by its maintainers). The inputs the workflows pass were checked
against each pinned `action.yml` and the release notes of every major
version in between: none of them changed meaning. Two behaviour changes
apply: `download-artifact` now fails a download whose digest does not
match (the safe default, kept), and `attest-build-provenance` 4 is a thin
wrapper around `actions/attest` (same inputs).

## Actions (pinned by commit)

| Action | Tag | Commit | Runs on | Verified |
| --- | --- | --- | --- | --- |
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 | node24 | 2026-10-03 |
| actions/setup-python | v7.0.0 | 5fda3b95a4ea91299a34e894583c3862153e4b97 | node24 | 2026-10-03 |
| actions/upload-artifact | v7.0.1 | 043fb46d1a93c77aae656e7c1c64a875d1fc6a0a | node24 | 2026-10-03 |
| actions/download-artifact | v8.0.1 | 3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c | node24 | 2026-10-03 |
| actions/attest-build-provenance | v4.2.2 | 4d101475d8b20a2381f78447822ac1eab6504dd8 | composite | 2026-10-03 |
| cachix/install-nix-action | v31.11.1 | 13d8dd58da0234aa297dedd986986ccb8e7f3e24 | composite | 2026-10-03 |

Each tag is a lightweight tag: the commit is the tag's own object
(`gh api repos/<owner>/<repo>/git/refs/tags/<tag>` names a commit), and
each is the repository's latest release (not a draft or prerelease).

## Tools (pinned by version)

| Tool | Version | Upstream identity | Verified |
| --- | --- | --- | --- |
| actionlint | v1.7.12 | github.com/rhysd/actionlint tag v1.7.12 = 914e7df21a07ef503a81201c76d2b11c789d3fca (Go module proxy origin agrees; latest release) | 2026-10-03 |
| zizmor | 1.30.1 | PyPI zizmor 1.30.1 (latest, not yanked; uploaded 2026-09-09); github.com/zizmorcore/zizmor tag v1.30.1 = 99a054ed9283c90abdd2d5b9fb5101d27dde9783; `--offline` still supported | 2026-10-03 |
| govulncheck | v1.8.0 | golang.org/x/vuln tag v1.8.0 = 709015412431dd2b5b28a53c06c70bc02d49074c (Go module proxy `@latest` and origin agree) | 2026-10-03 |
| Pester | 6.2.0 | PowerShell Gallery Pester 6.2.0 (latest, not a prerelease); github.com/pester/Pester tag 6.2.0 = 98e56d65da1642a7158992b50ded240c42a3ac18 | 2026-10-03 |
| setuptools | 82.0.1 | PyPI setuptools 82.0.1 (not yanked; uploaded 2026-03-09; requires Python >=3.9; 84.0.0 is newer, but the build backend in `pyproject.toml` pins 82.0.1) | 2026-10-03 |

## Tool artifacts (pinned by sha256)

| Tool | Artifact | sha256 | Verified |
| --- | --- | --- | --- |
| zizmor | zizmor-1.30.1-py3-none-manylinux_2_28_x86_64.whl | eee12266b793cb87ad4a7e3af2e72404f8a63e3de5eb099b80bf7b1cfd232a8e | 2026-10-03 |
| setuptools | setuptools-82.0.1-py3-none-any.whl | a59e362652f08dcd477c78bb6e7bd9d80a7995bc73ce773050228a348ce2e5bb | 2026-10-03 |

The CI fast lane installs zizmor with `pip install --require-hashes`
against this digest (the one PyPI records for the wheel), so it runs the
same file the local lint below ran; any other file, a rebuilt or
replaced wheel included, fails the install. The battery installs
setuptools the same way, for both of its Python versions (the
distribution tests build with it; a skipped distribution test fails the
job); the digest is the one PyPI records, and the local wheel the
distribution tests ran with has it.

Pester is installed with `-RequiredVersion`, so a new major never arrives
unannounced. The suite in `tests/pwsh` uses the classic `Should -Be`,
`Should -Throw` and `Should -Invoke` assertions and filtered mocks that
cover every call they intercept; it uses none of what Pester 6 removed
(`Assert-MockCalled`, `-Pending`, `-Focus`, empty `-ForEach`, duplicate
setup blocks, the fall-through from an unmatched filtered mock to the real
command).

## Container images (pinned by index digest)

| Image | Index digest | Verified |
| --- | --- | --- |
| ubuntu:22.04 | sha256:b1066385161d28ddf6bc7e7b28a9170eec11484c821d1a5150d176cbde41d7f7 | 2026-10-03 |
| ubuntu:24.04 | sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60 | 2026-10-03 |
| debian:12 | sha256:f37a335e82bca302e955fa39f9dfe28f1be618f016f8a2b56318e5a5111afc26 | 2026-10-03 |
| fedora:44 | sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80 | 2026-10-03 |

The digests are the multi-platform image indexes on Docker Hub
(`library/<name>`), still the current index of each tag on the day
verified; the runner pulls its own platform's image from the index.

## Workflow lint before the first CI run

On 2026-10-03 the two workflow linters ran locally over `.github/` at the
pinned versions, each fetched by hash: actionlint from the release archive
`actionlint_1.7.12_linux_amd64.tar.gz` (sha256
`8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8`, as
listed in the release's `actionlint_1.7.12_checksums.txt` and in GitHub's
asset digest), zizmor from the PyPI wheel
`zizmor-1.30.1-py3-none-manylinux_2_28_x86_64.whl` with
`pip download --require-hashes` (sha256
`eee12266b793cb87ad4a7e3af2e72404f8a63e3de5eb099b80bf7b1cfd232a8e`, the
digest PyPI records), run with `--offline`.

- actionlint, with shellcheck 0.11.0 on the path, reported two shellcheck
  notes, both fixed: the full-suite step passed its module list through an
  unquoted `$(cat …)` (now a bash array), and the Linux journey's
  `su journey -c '…'` keeps `$HOME` in single quotes on purpose (it is the
  journey user's), now stated with a `shellcheck disable=SC2016` comment.
  Its pyflakes rule had nothing to check: no step runs with `shell: python`.
- zizmor's default persona reports nothing. Its pedantic persona also
  asked for a comment on each write permission (added). Exception, kept:
  the pedantic and auditor personas list `anonymous-definition`
  (informational) for the 15 jobs, which carry no `name:`; the job ids
  already say what each job does, and a `name:` would only repeat them and
  change the names the checks are reported under.
- The CI lint step ran zizmor as `python3 -m zizmor`, which fails: the
  wheel ships an executable and no Python module. It now installs the wheel
  into a directory of its own, by the hash above (`--require-hashes`), and
  runs `bin/zizmor` from there.

## Workflow lint of the release-candidate wiring

On 2026-10-03, after the resolve job, the evidence producers, the native
lanes (arm64 Linux, the systemd user manager, Intel macOS behind its
repository variable, WSL 2 through install.ps1), the Nix garbage-collector
root step and the schedule guard were added, the same pinned actionlint
(with shellcheck on the path) and zizmor (`--offline`, default, pedantic
and auditor personas) ran over `.github/` again: actionlint reported
nothing; zizmor reported nothing beyond `anonymous-definition`
(informational), now for the 20 jobs, kept for the reason above. These
changes add no action, tool or image other than the setuptools wheel
above.
