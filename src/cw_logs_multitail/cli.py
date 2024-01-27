"""Command-line interface for cw-logs-multitail."""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from datetime import datetime, timezone

from . import __version__
from .localfilter import UnsupportedPattern, compile_pattern
from .model import Event, Source, parse_time
from .render import Printer, merge, use_colour
from .sources import load_file, resolve_groups
from .tail import Reader, follow


def _split(values: list[str] | None) -> list[str]:
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cw-logs-multitail",
        description="Tail several CloudWatch Logs groups as one merged, time-ordered, colour-coded stream. "
                    "Read-only: it only lists log groups and reads log events.",
    )
    parser.add_argument("groups", nargs="*", metavar="GROUP",
                        help="log group names or shell-style patterns, e.g. '/aws/lambda/orders-*'")
    parser.add_argument("--since", metavar="TIME", help="start: 15m, 2h, 1d ago, or an ISO timestamp "
                        "(default: 10m; with --from-file: no lower bound)")
    parser.add_argument("--until", metavar="TIME", help="end: same formats as --since (default: now)")
    parser.add_argument("--filter", metavar="PATTERN", default="",
                        help="CloudWatch Logs filter pattern, applied by the service")
    parser.add_argument("--grep", metavar="REGEX", help="also keep only messages matching this regular expression")
    parser.add_argument("-f", "--follow", action="store_true", help="keep polling for new events")
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECONDS",
                        help="polling interval for --follow (default: 2)")
    parser.add_argument("--fields", metavar="A,B.C", action="append",
                        help="for JSON log lines, print only these fields as field=value (dots reach nested keys)")
    parser.add_argument("--region", action="append", metavar="REGION",
                        help="region to read; repeat or comma-separate for several (default: profile's region)")
    parser.add_argument("--profile", action="append", metavar="NAME",
                        help="AWS profile; repeat or comma-separate to read several accounts")
    parser.add_argument("--from-file", action="append", metavar="FILE",
                        help="read exported events from a file instead of AWS (repeatable)")
    parser.add_argument("--output", choices=["text", "jsonl"], default="text",
                        help="jsonl prints one JSON object per event, readable again with --from-file")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="colour the group label (default: auto, honours NO_COLOR)")
    parser.add_argument("--full-names", action="store_true",
                        help="show full log group names instead of stripping their common prefix")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _warn(text: str) -> None:
    print(f"cw-logs-multitail: {text}", file=sys.stderr)


def _ordered_sources(events: list[Event]) -> list[Source]:
    sources: list[Source] = []
    for event in events:
        if event.source not in sources:
            sources.append(event.source)
    return sorted(sources, key=lambda s: (s.profile, s.region, s.group))


def _run_files(args, start_ms, end_ms, grep, out) -> int:
    matches = compile_pattern(args.filter)
    events = [event for path in args.from_file for event in load_file(path)]
    events = [
        e for e in events
        if (not args.groups or any(fnmatch.fnmatchcase(e.source.group, p) for p in args.groups))
        and (start_ms is None or e.timestamp >= start_ms)
        and (end_ms is None or e.timestamp <= end_ms)
        and matches(e.message) and (grep is None or grep.search(e.message))
    ]
    sources = _ordered_sources(events)
    printer = Printer(sources, out, use_colour(args.color, out), _split(args.fields),
                      args.full_names, args.output == "jsonl")
    printer.emit(merge([[e for e in events if e.source == source] for source in sources]))
    return 0


def _run_live(args, start_ms, end_ms, grep, out, session_factory, sleep) -> int:
    if session_factory is None:
        import boto3
        session_factory = lambda profile, region: boto3.Session(  # noqa: E731
            profile_name=profile or None, region_name=region or None)

    clients: dict[tuple[str, str], object] = {}
    sources: list[Source] = []
    for profile in _split(args.profile) or [""]:
        for region in _split(args.region) or [""]:
            session = session_factory(profile, region)
            actual_region = region or session.region_name or ""
            if not actual_region:
                _warn("no region: pass --region or configure a default region")
                return 2
            client = session.client("logs", region_name=actual_region)
            clients[(profile, actual_region)] = client
            for group in resolve_groups(client, args.groups):
                sources.append(Source(group=group, region=actual_region, profile=profile))
    if not sources:
        _warn(f"no log groups match {', '.join(args.groups)}")
        return 2

    reader = Reader(clients, sources, args.filter,
                    on_error=lambda source, exc: _warn(f"{source.group} ({source.region}): {exc}"))
    printer = Printer(sources, out, use_colour(args.color, out), _split(args.fields),
                      args.full_names, args.output == "jsonl")

    def emit(batches: list[list[Event]]) -> None:
        printer.emit(e for e in merge(batches) if grep is None or grep.search(e.message))

    emit(reader.read(start_ms, end_ms))
    if len(reader.failed) == len(sources):
        return 1
    if args.follow:
        try:
            follow(reader, start_ms, emit, args.interval, sleep, getattr(args, "max_polls", None))
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None, session_factory=None, out=None, now: datetime | None = None,
         sleep=None, max_polls: int | None = None) -> int:
    """Entry point. The keyword arguments exist so tests can inject fakes."""
    import time

    parser = build_parser()
    args = parser.parse_args(argv)
    args.max_polls = max_polls
    out = out or sys.stdout
    now = now or datetime.now(timezone.utc)

    if not args.groups and not args.from_file:
        parser.error("give at least one log group name or pattern (or --from-file)")
    if args.from_file and (args.follow or args.region or args.profile):
        parser.error("--from-file cannot be combined with --follow, --region or --profile")
    if args.follow and args.until:
        parser.error("--follow cannot be combined with --until")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    try:
        since = args.since or (None if args.from_file else "10m")
        start_ms = parse_time(since, now) if since else None
        end_ms = parse_time(args.until, now) if args.until else None
        grep = re.compile(args.grep) if args.grep else None
    except (ValueError, re.error) as exc:
        parser.error(str(exc))
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        parser.error("--since is later than --until")

    try:
        if args.from_file:
            return _run_files(args, start_ms, end_ms, grep, out)
        return _run_live(args, start_ms, end_ms, grep, out, session_factory, sleep or time.sleep)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, UnsupportedPattern) as exc:
        _warn(str(exc))
        return 2
    except Exception as exc:  # credential, endpoint and permission problems from botocore
        if type(exc).__module__.startswith("botocore"):
            _warn(str(exc))
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
