"""Read many log groups in parallel, once or continuously."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from botocore.exceptions import BotoCoreError, ClientError

from .model import Event, Source
from .sources import fetch_events

MAX_WORKERS = 8
# Events can become visible slightly out of order, so each poll re-reads this
# much before the newest event already shown and drops the ones it has printed.
OVERLAP_MS = 5000


class Reader:
    """Fetches from a fixed set of sources; remembers what follow mode has already shown."""

    def __init__(self, clients: dict[tuple[str, str], object], sources: list[Source],
                 filter_pattern: str = "", on_error: Callable[[Source, Exception], None] | None = None):
        self.clients = clients
        self.sources = sources
        self.filter_pattern = filter_pattern
        self.on_error = on_error or (lambda source, exc: None)
        self.failed: set[Source] = set()
        self._newest: dict[Source, int] = {}
        self._seen: dict[Source, dict[str, int]] = {s: {} for s in sources}

    def _fetch_one(self, source: Source, start_ms: int | None, end_ms: int | None) -> list[Event]:
        client = self.clients[(source.profile, source.region)]
        try:
            events = fetch_events(client, source, start_ms, end_ms, self.filter_pattern)
        except (ClientError, BotoCoreError) as exc:
            if source not in self.failed:  # report each broken group once, not every poll
                self.failed.add(source)
                self.on_error(source, exc)
            return []
        self.failed.discard(source)
        return events

    def read(self, start_ms: int | None, end_ms: int | None = None) -> list[list[Event]]:
        """One pass over every source, in parallel. Returns one oldest-first list per source."""
        if not self.sources:
            return []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(self.sources))) as pool:
            batches = list(pool.map(lambda s: self._fetch_one(s, start_ms, end_ms), self.sources))
        for source, batch in zip(self.sources, batches):
            self._remember(source, batch)
        return batches

    def _remember(self, source: Source, batch: list[Event]) -> None:
        seen = self._seen[source]
        for event in batch:
            seen[event.event_id or f"{event.timestamp}:{event.stream}:{event.message}"] = event.timestamp
        if batch:
            self._newest[source] = max(self._newest.get(source, 0), batch[-1].timestamp)
            horizon = self._newest[source] - OVERLAP_MS
            for key in [k for k, ts in seen.items() if ts < horizon]:
                del seen[key]

    def poll(self, default_start_ms: int) -> list[list[Event]]:
        """Fetch only events that were not returned by an earlier read() or poll()."""
        def fresh(source: Source) -> list[Event]:
            newest = self._newest.get(source)
            start = default_start_ms if newest is None else max(default_start_ms, newest - OVERLAP_MS)
            seen = self._seen[source]
            return [e for e in self._fetch_one(source, start, None)
                    if (e.event_id or f"{e.timestamp}:{e.stream}:{e.message}") not in seen]

        if not self.sources:
            return []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(self.sources))) as pool:
            batches = list(pool.map(fresh, self.sources))
        for source, batch in zip(self.sources, batches):
            self._remember(source, batch)
        return batches


def follow(reader: Reader, start_ms: int, emit: Callable[[list[list[Event]]], None],
           interval: float = 2.0, sleep: Callable[[float], None] = time.sleep,
           max_polls: int | None = None) -> None:
    """Poll forever (or `max_polls` times), handing each round of new events to `emit`."""
    polls = 0
    while max_polls is None or polls < max_polls:
        sleep(interval)
        emit(reader.poll(start_ms))
        polls += 1
