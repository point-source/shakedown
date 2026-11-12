"""Network activity (§spec:network-activity): per source, what a run measured each
minute — bytes, peak requests in flight, failures by cause, retries, back-off — and
each network event, recorded by the run into the state database (schema v10)."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum


class EventKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    REFUSED = "refused"
    RETRY = "retry"
    BACKOFF = "backoff"


@dataclass
class MinuteTotals:
    """One source's totals for one minute."""
    bytes: int = 0
    peak_requests_in_flight: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    timeouts: int = 0
    connection_errors: int = 0
    retries: int = 0
    backoff_seconds: float = 0.0

    def merge(self, other: MinuteTotals) -> None:
        """Add ``other`` in: sums add up, the peak is the higher of the two."""
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            value = max(mine, theirs) if f.name == "peak_requests_in_flight" else mine + theirs
            setattr(self, f.name, value)

    def any(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))


# The per-minute counter each kind of event adds one to; a back-off adds its seconds,
# and a refusal has no counter.
_COUNTERS: dict[EventKind, str] = {
    EventKind.RATE_LIMITED: "rate_limited",
    EventKind.SERVER_ERROR: "server_errors",
    EventKind.TIMEOUT: "timeouts",
    EventKind.CONNECTION_ERROR: "connection_errors",
    EventKind.RETRY: "retries",
}

_TOTAL_COLUMNS = tuple(f.name for f in fields(MinuteTotals))


@dataclass(frozen=True)
class NetworkEvent:
    at: datetime
    kind: EventKind
    http_status: int | None = None
    collection: str | None = None
    identifier: str | None = None
    wait_seconds: float | None = None
    detail: str | None = None


def _minute_key(minute: datetime) -> str:
    return minute.replace(second=0, microsecond=0).isoformat()


def _event(row: sqlite3.Row) -> NetworkEvent:
    return NetworkEvent(
        at=datetime.fromisoformat(row["at"]),
        kind=EventKind(row["kind"]),
        http_status=row["http_status"],
        collection=row["collection_name"],
        identifier=row["identifier"],
        wait_seconds=row["wait_seconds"],
        detail=row["detail"],
    )


class NetworkRepo:
    """The `network_minutes` and `network_events` tables. A reader of an older schema
    has no tables and sees no history."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def has_tables(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' "
            "AND name IN ('network_minutes', 'network_events')"
        ).fetchone()
        return row["n"] == 2

    def add_minutes(self, source: str, minutes: Mapping[datetime, MinuteTotals]) -> None:
        """Add what happened since the last write: sums add to the minute's row, its
        peak keeps the higher value."""
        updates = ", ".join(
            f"{c}=MAX({c}, excluded.{c})" if c == "peak_requests_in_flight"
            else f"{c}={c} + excluded.{c}"
            for c in _TOTAL_COLUMNS
        )
        self.conn.executemany(
            f"""
            INSERT INTO network_minutes (source_name, minute, {", ".join(_TOTAL_COLUMNS)})
            VALUES (?, ?, {", ".join("?" for _ in _TOTAL_COLUMNS)})
            ON CONFLICT (source_name, minute) DO UPDATE SET {updates}
            """,
            [
                (source, _minute_key(minute), *(getattr(t, c) for c in _TOTAL_COLUMNS))
                for minute, t in minutes.items()
            ],
        )

    def add_events(self, source: str, events: Sequence[NetworkEvent]) -> None:
        self.conn.executemany(
            """
            INSERT INTO network_events (source_name, at, kind, http_status,
                collection_name, identifier, wait_seconds, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (source, e.at.isoformat(), e.kind.value, e.http_status, e.collection,
                 e.identifier, e.wait_seconds, e.detail)
                for e in events
            ],
        )

    def trim(self, cutoff: datetime) -> None:
        """Drop history older than ``cutoff``."""
        self.conn.execute("DELETE FROM network_minutes WHERE minute < ?", (_minute_key(cutoff),))
        self.conn.execute("DELETE FROM network_events WHERE at < ?", (cutoff.isoformat(),))

    def minutes(self, source: str, since: datetime) -> list[tuple[datetime, MinuteTotals]]:
        rows = self.conn.execute(
            f"SELECT minute, {', '.join(_TOTAL_COLUMNS)} FROM network_minutes "
            "WHERE source_name=? AND minute >= ? ORDER BY minute",
            (source, _minute_key(since)),
        ).fetchall()
        return [
            (datetime.fromisoformat(row["minute"]),
             MinuteTotals(**{c: row[c] for c in _TOTAL_COLUMNS}))
            for row in rows
        ]

    def events(self, source: str, since: datetime, limit: int) -> list[NetworkEvent]:
        """The newest ``limit`` events at or after ``since``, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM network_events WHERE source_name=? AND at >= ? "
            "ORDER BY at DESC, id DESC LIMIT ?",
            (source, since.isoformat(), limit),
        ).fetchall()
        return [_event(row) for row in rows]

    def events_total(self, source: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM network_events WHERE source_name=? AND at >= ?",
            (source, since.isoformat()),
        ).fetchone()
        return row["n"]

    def last_event(self, source: str, since: datetime) -> NetworkEvent | None:
        events = self.events(source, since, limit=1)
        return events[0] if events else None


class NetworkMeter:
    """One source's network activity in this process, as the run's hooks report it:
    per-minute totals and events not yet written. Collections of the same source share
    one meter, so their requests and bytes add up to the source's. Every method is an
    in-memory update under a lock and never raises; the recorder takes what is pending
    in its few-second writes and gives it back if the write cannot land."""

    def __init__(self, source: str, now: Callable[[], datetime] = datetime.now) -> None:
        self.source = source
        self._now = now
        self._lock = threading.Lock()
        self._in_flight = 0
        self._minutes: dict[datetime, MinuteTotals] = {}
        self._events: list[NetworkEvent] = []

    def _minute(self, at: datetime) -> MinuteTotals:
        return self._minutes.setdefault(at.replace(second=0, microsecond=0), MinuteTotals())

    def _note_peak(self, at: datetime) -> None:
        if self._in_flight > 0:
            totals = self._minute(at)
            totals.peak_requests_in_flight = max(totals.peak_requests_in_flight, self._in_flight)

    def add_bytes(self, n: int) -> None:
        with self._lock:
            now = self._now()
            self._minute(now).bytes += n
            self._note_peak(now)

    def request_started(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._note_peak(self._now())

    def request_ended(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def event(self, event: NetworkEvent) -> None:
        with self._lock:
            self._events.append(event)
            totals = self._minute(event.at)
            counter = _COUNTERS.get(event.kind)
            if counter is not None:
                setattr(totals, counter, getattr(totals, counter) + 1)
            elif event.kind is EventKind.BACKOFF:
                totals.backoff_seconds += event.wait_seconds or 0.0

    def take(self) -> tuple[dict[datetime, MinuteTotals], list[NetworkEvent]]:
        """What is pending, now cleared; the current minute's peak counts requests still
        in flight."""
        with self._lock:
            self._note_peak(self._now())
            minutes = {m: t for m, t in self._minutes.items() if t.any()}
            events = self._events
            self._minutes, self._events = {}, []
            return minutes, events

    def give_back(self, minutes: dict[datetime, MinuteTotals], events: list[NetworkEvent]) -> None:
        """Return what a failed write took, so the next write carries it."""
        with self._lock:
            for minute, totals in minutes.items():
                self._minute(minute).merge(totals)
            self._events[:0] = events
