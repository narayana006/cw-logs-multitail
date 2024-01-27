"""Shared data types and small parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass(frozen=True)
class Source:
    """One log group in one region, read with one profile."""
    group: str
    region: str = ""
    profile: str = ""


@dataclass(frozen=True)
class Event:
    timestamp: int  # milliseconds since the epoch, as CloudWatch Logs reports it
    message: str
    source: Source
    stream: str = ""
    event_id: str = ""


def parse_time(text: str, now: datetime) -> int:
    """Turn '15m', '2h', '1d' (ago) or an ISO-8601 timestamp into epoch milliseconds."""
    text = text.strip()
    match = re.fullmatch(r"(\d+)\s*([smhdw])", text)
    if match:
        moment = now - timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
    else:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"cannot read time {text!r}: use a duration such as 15m, 2h, 1d "
                "or an ISO timestamp such as 2026-03-01T09:30:00Z") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def format_time(timestamp_ms: int) -> str:
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{timestamp_ms % 1000:03d}Z"
