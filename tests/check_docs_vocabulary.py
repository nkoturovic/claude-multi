#!/usr/bin/env python3
"""Documentation gate: stale vocabulary, secret reads, machine facts, links.

    python3 tests/check_docs_vocabulary.py [--links] [--spans] [--installed DIR] [--self-test] [-v] [PATH[:REGEX]…]

Without ``--links``, ``--self-test`` or a PATH it scans the in-tree set
(every public page, every rendered ``claude-multi`` ``--help``, the proxy/dev
help texts, and ``src`` for the two phrases). ``--links`` resolves the
Markdown links of every public page (``_docs_vocab.public_pages``: the root
pages and every page under ``docs/``; a link leaving the repository, a
missing heading anchor or an installed page linking a root page by a
relative path fails) and lists references to material outside it.
``--spans`` parses every command span of every public page (each form of
it, with the parser that owns it) and prints the coverage counts.
``--installed DIR`` resolves the links of an installed document tree
(``share/claude-multi`` of a wheel, a bundle or the Nix package): every
relative link and heading anchor stays inside it, nested directories
included, and the root pages are reached only by their public URL.
``PATH[:REGEX]`` scans one Markdown file with the full rule set (only lines
matching REGEX when given).

Every rule lives in ``tests/_docs_vocab.py``. The in-tree mode also
enforces ``_docs_vocab.BUDGETS`` (as ``tests/test_docs_vocabulary.py``
does). The checker never opens anything under ``~/.claude/projects``, a
``*.jsonl`` file or a secret: such a PATH exits 2 before any file is read.

Output: ``file:line: rule: text``; ``-v`` also prints intentional and
report-only hits. Exit 0 clean, 1 hits, 2 a path it may not read.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _layout import REPO_ROOT  # noqa: E402  (the one root source)

sys.path.insert(1, str(REPO_ROOT / "src"))

import _docs_vocab as dv  # noqa: E402


def _parse_path(arg: str) -> tuple[Path, str | None]:
    path, sep, regex = arg.partition(":")
    return Path(os.path.expanduser(path)), (regex if sep and regex else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_docs_vocabulary.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--links", action="store_true",
                        help="resolve relative Markdown links in the public docs; list outside references")
    parser.add_argument("--spans", action="store_true",
                        help="parse every command span of the public pages and print the coverage counts")
    parser.add_argument("--installed", type=Path, metavar="DIR",
                        help="resolve the links of an installed document tree (share/claude-multi)")
    parser.add_argument("--self-test", action="store_true", help="run the rule unit cases")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print intentional and report-only hits")
    parser.add_argument("paths", nargs="*", metavar="PATH[:REGEX]")
    args = parser.parse_args(argv)

    requested = [_parse_path(arg) for arg in args.paths]
    for path, _regex in requested:
        reason = dv.forbidden_reason(path)
        if reason:
            print(f"{path}: refused: {reason}", file=sys.stderr)
            return 2

    failing = 0
    if args.self_test:
        problems = dv.self_test()
        for problem in problems:
            print(f"self-test: {problem}")
        failing += len(problems)
        print(f"self-test: {'ok' if not problems else f'{len(problems)} failure(s)'}")

    targets: list[dv.Target] = []
    explicit = dv.EXPLICIT
    for path, regex in requested:
        if not path.is_file():
            print(f"{path}: not a file", file=sys.stderr)
            return 2
        targets.append(dv.Target(str(path), "markdown", frozenset(dv.RULES_USER_DOC), path, line_filter=regex))
    if not (args.links or args.spans or args.self_test or args.installed or requested):
        targets = dv.in_tree_targets()

    if targets:
        hits, hygiene = dv.run_all(targets, explicit=explicit)
        for hit in hits:
            if hit.failing or args.verbose:
                print(hit.format())
        failing += sum(hit.failing for hit in hits)
        if not requested:
            # The in-tree set: allowlist hygiene and the per-file budgets
            # (the same two checks test_docs_vocabulary runs).
            for problem in hygiene:
                print(f"allowlist: {problem}")
            budgets = dv.budget_failures(hits)
            for problem in budgets:
                print(f"budget: {problem}")
            failing += len(hygiene) + len(budgets)
        intentional = sum(bool(hit.intentional) for hit in hits)
        report_only = sum(bool(hit.report_only) and not hit.intentional for hit in hits)
        print(
            f"scanned {len(targets)} target(s): {sum(h.failing for h in hits)} failing, "
            f"{intentional} intentional, {report_only} report-only hit(s)"
        )

    if args.links:
        broken = 0
        for path in dv.link_targets():
            for line, link, problem in dv.link_problems(path):
                print(f"{path.relative_to(dv.REPO_ROOT)}:{line}: link: {link}: {problem}")
                broken += 1
            for line, reference in dv.private_references(path.read_text(encoding="utf-8")):
                print(f"{path.relative_to(dv.REPO_ROOT)}:{line}: outside reference: {reference}")
                broken += 1
        print(f"links: {broken} broken")
        failing += broken

    if args.installed:
        if not (args.installed / dv.MANUALS[0]).is_file():
            print(f"{args.installed}: no installed document tree ({dv.MANUALS[0]} is missing)", file=sys.stderr)
            return 2
        problems = dv.installed_link_problems(args.installed)
        for page, line, link, problem in problems:
            print(f"{page}:{line}: link: {link}: {problem}")
        pages = len(list(args.installed.rglob("*.md")))
        print(f"installed: {pages} pages, {len(problems)} broken link(s)")
        failing += len(problems) + (pages == 0)

    if args.spans:
        pages = {label: dv.doc_path(label).read_text(encoding="utf-8") for label in dv.public_pages()}
        for label, text in pages.items():
            for line, span, _physical in dv.command_spans(text):
                problem = dv.page_span_problem(label, span)
                if problem:
                    print(f"{label}:{line}: span: {span}: {problem}")
        coverage = dv.span_coverage(pages)
        print("spans: " + ", ".join(f"{key} {value}" for key, value in coverage.items()))
        failing += coverage["problems"] + (coverage["spans"] == 0)

    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
