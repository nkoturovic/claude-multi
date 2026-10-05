"""Read-only observed client HTTP usage."""
from claude_multi import errors, usage, termtext
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.types as cli_types
import claude_multi.cli.session_facts as session_facts


def command(runtime, args, output_stream):
    now = gateway_facts._doctor_now()
    try:
        start = usage.parse_since(args.since, now)
    except errors.CLIError as exc:
        # An invalid --since value is an invalid command line (exit 2).
        raise cli_types.UsageError(str(exc)) from exc
    with runtime.report_snapshot():
        mid = session_facts.report_session(runtime, args.session)["managed_id"] if args.session else None
        events = gateway_facts.report_events(runtime, since=start.isoformat())
        report = usage.report(events, start, now, mid)
    output_stream.write(report.json() if args.json else "\n".join(termtext.visible_text(line) for line in usage.text(report).splitlines()) + "\n")
    return 0 if events.coverage != "unavailable" else 1
