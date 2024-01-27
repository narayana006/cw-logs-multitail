"""Where events come from: CloudWatch Logs (read-only) or exported files."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Iterable, Iterator

from .model import Event, Source

_WILDCARD = re.compile(r"[*?\[]")


def has_wildcard(pattern: str) -> bool:
    return _WILDCARD.search(pattern) is not None


def resolve_groups(client, patterns: Iterable[str]) -> list[str]:
    """Expand names and shell-style patterns into log group names, in a stable order.

    A plain name is used as given (no API call). A pattern lists groups with
    DescribeLogGroups, narrowed by the literal text before the first wildcard.
    """
    found: list[str] = []
    for pattern in patterns:
        if not has_wildcard(pattern):
            names = [pattern]
        else:
            prefix = _WILDCARD.split(pattern, maxsplit=1)[0]
            kwargs = {"logGroupNamePrefix": prefix} if prefix else {}
            names = sorted(
                group["logGroupName"]
                for page in client.get_paginator("describe_log_groups").paginate(**kwargs)
                for group in page.get("logGroups", [])
                if fnmatch.fnmatchcase(group["logGroupName"], pattern)
            )
        found.extend(name for name in names if name not in found)
    return found


def fetch_events(client, source: Source, start_ms: int | None = None, end_ms: int | None = None,
                 filter_pattern: str = "") -> list[Event]:
    """Read matching events of one log group with FilterLogEvents, oldest first."""
    kwargs: dict = {"logGroupName": source.group}
    if start_ms is not None:
        kwargs["startTime"] = start_ms
    if end_ms is not None:
        kwargs["endTime"] = end_ms
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern
    events = [
        Event(timestamp=item["timestamp"], message=item.get("message", ""), source=source,
              stream=item.get("logStreamName", ""), event_id=item.get("eventId", ""))
        for page in client.get_paginator("filter_log_events").paginate(**kwargs)
        for item in page.get("events", [])
    ]
    events.sort(key=lambda e: e.timestamp)  # stable: keeps service order within a millisecond
    return events


def _event_from_record(record: dict, default_group: str, where: str) -> Event:
    if not isinstance(record, dict) or "timestamp" not in record or "message" not in record:
        raise ValueError(f"{where}: each event needs \"timestamp\" (epoch ms) and \"message\"")
    timestamp = record["timestamp"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError(f"{where}: \"timestamp\" must be a number of milliseconds since the epoch")
    group = record.get("logGroup") or record.get("logGroupName") or record.get("group") or default_group
    return Event(
        timestamp=int(timestamp), message=str(record["message"]),
        source=Source(group=group, region=record.get("region", ""), profile=record.get("profile", "")),
        stream=record.get("logStreamName") or record.get("stream") or "",
        event_id=record.get("eventId", ""),
    )


def load_file(path: str) -> list[Event]:
    """Load events from JSON Lines, or from `aws logs filter-log-events` JSON output.

    Events without a group field are attributed to a group named after the file.
    """
    text = Path(path).read_text(encoding="utf-8")
    default_group = Path(path).stem
    stripped = text.lstrip()
    records: Iterator[tuple[str, object]]
    if _is_document(stripped):
        document = json.loads(stripped)
        records = ((f"{path}: events[{i}]", r) for i, r in enumerate(document["events"]))
    else:
        def lines() -> Iterator[tuple[str, object]]:
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    yield f"{path}:{number}", json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: not valid JSON ({exc.msg})") from exc
        records = lines()
    events = [_event_from_record(record, default_group, where) for where, record in records]
    events.sort(key=lambda e: e.timestamp)
    return events


def _is_document(text: str) -> bool:
    """True if the whole text is one JSON object with an "events" list."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(document, dict) and isinstance(document.get("events"), list)
