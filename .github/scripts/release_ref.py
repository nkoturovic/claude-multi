"""The commit a release run builds, and whether it may draft (CI).

  python3 .github/scripts/release_ref.py --event EVENT --ref REF --sha SHA \
      [--input-ref NAME] [--draft true|false]

Run in a full clone (every branch and tag fetched). Prints the step
outputs as NAME=value lines (for $GITHUB_OUTPUT): ``sha`` (the one commit
every job checks out), ``tag`` (the release tag, or empty) and ``draft``
(``true`` or ``false``).

- A tag push (EVENT ``push``, REF ``refs/tags/v...``): the tag's commit,
  which must be SHA (the commit the push event names); it drafts (behind
  the protected environment).
- A manual run (EVENT ``workflow_dispatch``): --input-ref names a tag, a
  branch or a commit; a tag wins over a branch of the same name. It builds
  only, unless --draft true: then --input-ref must name an existing tag
  ``v<major>.<minor>.<patch>`` (exactly that tag, not its commit), whose
  commit is the one resolved. No tag is ever created here.

Anything else exits 1 with the reason, before any expensive job runs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class RefError(Exception):
    pass


def _git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=60,
                            stdin=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else None


def commit_of(ref: str) -> str | None:
    if not ref or ref.startswith("-") or any(ch.isspace() for ch in ref):
        return None
    return _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")


def tag_commit(name: str) -> str | None:
    """The commit of the existing tag ``name`` (annotated or not), or None."""

    if not TAG.fullmatch(name) or _git("show-ref", "--verify", "--quiet", f"refs/tags/{name}") is None:
        return None
    return commit_of(f"refs/tags/{name}")


def resolve(event: str, ref: str, sha: str, input_ref: str, draft: str) -> dict[str, str]:
    if event == "push":
        name = ref.removeprefix("refs/tags/")
        if not ref.startswith("refs/tags/") or not TAG.fullmatch(name):
            raise RefError(f"a release push must be a v<major>.<minor>.<patch> tag, not {ref!r}")
        found = tag_commit(name)
        if found is None:
            raise RefError(f"the tag {name} is not in this clone")
        if found != sha:
            raise RefError(f"the tag {name} names {found}, the push named {sha}")
        return {"sha": found, "tag": name, "draft": "true"}
    if event != "workflow_dispatch":
        raise RefError(f"release runs on a tag push or a manual run, not on {event!r}")
    if draft not in ("true", "false"):
        raise RefError(f"draft must be true or false, not {draft!r}")
    name = input_ref.strip()
    found = None
    for candidate in (f"refs/tags/{name}", f"refs/remotes/origin/{name}", name):
        found = commit_of(candidate)
        if found is not None:
            break
    if found is None or not SHA.fullmatch(found):
        raise RefError(f"ref {name!r} names no tag, branch or commit in this clone")
    if draft == "false":
        return {"sha": found, "tag": "", "draft": "false"}
    tagged = tag_commit(name)
    if tagged is None:
        raise RefError(f"a draft needs ref to name an existing v<major>.<minor>.<patch> tag; {name!r} is not one "
                       "(create and push the tag first, or run without draft)")
    if tagged != found:
        raise RefError(f"the tag {name} names {tagged}, not the resolved commit {found}")
    return {"sha": found, "tag": name, "draft": "true"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_ref.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--input-ref", default="")
    parser.add_argument("--draft", default="false")
    args = parser.parse_args(argv)
    try:
        outputs = resolve(args.event, args.ref, args.sha, args.input_ref, args.draft)
    except RefError as exc:
        print(f"release_ref: {exc}", file=sys.stderr)
        return 1
    for name, value in outputs.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
