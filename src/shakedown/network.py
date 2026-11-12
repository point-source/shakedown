"""Network activity (§spec:network-activity): per source, what a run measured each
minute — bytes, peak requests in flight, failures by cause, retries, back-off — and
each network event, recorded by the run into the state database (schema v10)."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
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


# The per-minute counter each failure cause adds to.
CAUSE_COUNTERS: dict[EventKind, str] = {
    EventKind.RATE_LIMITED: "rate_limited",
    EventKind.SERVER_ERROR: "server_errors",
    EventKind.TIMEOUT: "timeouts",
    EventKind.CONNECTION_ERROR: "connection_errors",
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
