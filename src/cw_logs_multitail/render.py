"""Merge events by time and format them for a terminal or as JSON Lines."""

from __future__ import annotations

import heapq
import json
import os
from typing import Iterable, Iterator

from .model import Event, Source, format_time

# bright, readable on dark and light backgrounds; cycled when there are more groups
_COLOURS = ("36", "33", "35", "32", "34", "31", "96", "93", "95", "92", "94", "91")
_RESET = "\033[0m"


def merge(batches: Iterable[list[Event]]) -> Iterator[Event]:
    """Merge per-group lists (each already oldest-first) into one time-ordered stream.

    Events with the same millisecond keep the order of the groups as given.
    """
    keyed = (
        [(event.timestamp, index, position, event) for position, event in enumerate(batch)]
        for index, batch in enumerate(batches)
    )
    for _, _, _, event in heapq.merge(*keyed):
        yield event


def use_colour(mode: str, stream) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def make_labels(sources: list[Source], full_names: bool = False) -> dict[Source, str]:
    """Short display name per source; region/profile are added only when they differ."""
    names = [s.group for s in sources]
    cut = 0
    if not full_names and len(set(names)) > 1:
        common = os.path.commonprefix(names)
        cut = common.rfind("/") + 1  # only strip whole path segments
        if cut <= 1:  # nothing shared beyond the leading slash
            cut = 0
    many_regions = len({s.region for s in sources}) > 1
    many_profiles = len({s.profile for s in sources}) > 1
    labels = {}
    for source in sources:
        label = source.group[cut:] or source.group
        if many_regions and source.region:
            label += f"@{source.region}"
        if many_profiles and source.profile:
            label = f"{source.profile}:{label}"
        labels[source] = label
    return labels


def _lookup(document: dict, path: str):
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _show(value) -> str:
    if isinstance(value, str):
        return value if value and not any(c.isspace() for c in value) else json.dumps(value)
    return json.dumps(value, separators=(",", ":"))


def format_message(message: str, fields: list[str] | None) -> str:
    """With --fields, show JSON object lines as `field=value` pairs; leave other lines alone."""
    text = message.rstrip("\n")
    if not fields or not text.lstrip().startswith("{"):
        return text
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(document, dict):
        return text
    pairs = [(field, _lookup(document, field)) for field in fields]
    shown = [f"{field}={_show(value)}" for field, value in pairs if value is not None]
    return " ".join(shown) if shown else text


class Printer:
    def __init__(self, sources: list[Source], out, colour: bool = False, fields: list[str] | None = None,
                 full_names: bool = False, as_json: bool = False):
        self.out = out
        self.colour = colour
        self.fields = fields
        self.as_json = as_json
        self.labels = make_labels(sources, full_names)
        self.width = max((len(label) for label in self.labels.values()), default=0)
        self.colours = {source: _COLOURS[i % len(_COLOURS)] for i, source in enumerate(sources)}

    def line(self, event: Event) -> str:
        if self.as_json:
            record = {"timestamp": event.timestamp, "logGroup": event.source.group}
            if event.source.region:
                record["region"] = event.source.region
            if event.source.profile:
                record["profile"] = event.source.profile
            if event.stream:
                record["logStreamName"] = event.stream
            record["message"] = event.message.rstrip("\n")
            return json.dumps(record, ensure_ascii=False)
        label = self.labels.get(event.source, event.source.group).ljust(self.width)
        if self.colour:
            label = f"\033[{self.colours.get(event.source, '0')}m{label}{_RESET}"
        return f"{format_time(event.timestamp)}  {label}  {format_message(event.message, self.fields)}"

    def emit(self, events: Iterable[Event]) -> int:
        count = 0
        for event in events:
            print(self.line(event), file=self.out, flush=True)
            count += 1
        return count
