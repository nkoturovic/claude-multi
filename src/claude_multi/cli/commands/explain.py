"""Read-only routing explanation entry point."""
from claude_multi import routing, termtext
import claude_multi.cli.session_facts as session_facts


def command(runtime, args, output_stream):
    with runtime.report_snapshot():
        report = session_facts.explanation(runtime, args.session, args.agent)
    output_stream.write(report.json() if args.json else "\n".join(termtext.visible_text(line) for line in routing.text(report).splitlines()) + "\n")
    return 0
