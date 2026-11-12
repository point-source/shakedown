"""Growth snapshots (§spec:library-stats "Growth"): per collection, its complete shows
and archive bytes, recorded at the end of every run that holds the writer lock (the
only runs that change them) and first by the v10 migration. The dashboard reads one
figure per day for the last 30 days; a day with no run carries the previous day's.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from shakedown.db import transaction
from shakedown.models import ItemStatus
from shakedown.state import ItemRepo

# History is kept this long (§spec:state); growth is shown for this many days.
HISTORY_DAYS = 30


class GrowthRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def has_table(self) -> bool:
        """False on a database before schema v10, which records no growth."""
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='growth_snapshots'"
        ).fetchone() is not None

    def record(self, taken_at: datetime, scopes: Iterable[tuple[str, str]] | None) -> None:
        """Snapshot each (source, collection) in ``scopes``, or every collection the
        database holds shows for when None. The figures come from the computation
        `status` uses, so a snapshot never disagrees with it."""
        items = ItemRepo(self.conn)
        if scopes is None:
            scopes = [
                (row["source_name"], row["collection_name"])
                for row in self.conn.execute(
                    "SELECT DISTINCT source_name, collection_name FROM items"
                    " ORDER BY source_name, collection_name"
                )
            ]
        rows = [
            (
                source,
                collection,
                taken_at.isoformat(),
                items.count_by_status(source, collection).get(ItemStatus.COMPLETE, 0),
                items.archived_bytes(source, collection),
            )
            for source, collection in scopes
        ]
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO growth_snapshots"
                " (source_name, collection_name, taken_at, complete, archive_bytes)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def trim(self, now: datetime) -> None:
        """Drop snapshots older than the history window, except each collection's
        newest one before it: that one carries into the window's first days."""
        cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat()
        with transaction(self.conn):
            self.conn.execute(
                """
                DELETE FROM growth_snapshots
                WHERE taken_at < ?
                  AND id NOT IN (
                      SELECT id FROM (
                          SELECT id, ROW_NUMBER() OVER (
                              PARTITION BY source_name, collection_name
                              ORDER BY taken_at DESC, id DESC
                          ) AS n
                          FROM growth_snapshots WHERE taken_at < ?
                      ) WHERE n = 1
                  )
                """,
                (cutoff, cutoff),
            )

    def series(
        self, scopes: Iterable[tuple[str, str]], today: date
    ) -> list[dict[str, Any]]:
        """One row per collection per day, from the window's start (or the
        collection's first snapshot, if later) to ``today``: the day's last snapshot,
        else the previous day's figures."""
        first_day = today - timedelta(days=HISTORY_DAYS - 1)
        out: list[dict[str, Any]] = []
        for source, collection in scopes:
            rows = self.conn.execute(
                """
                SELECT taken_at, complete, archive_bytes FROM growth_snapshots
                WHERE source_name=? AND collection_name=? AND taken_at < ?
                ORDER BY taken_at, id
                """,
                (source, collection, (today + timedelta(days=1)).isoformat()),
            ).fetchall()
            if not rows:
                continue
            by_day: dict[date, tuple[int, int]] = {}
            for row in rows:
                by_day[datetime.fromisoformat(row["taken_at"]).date()] = (
                    row["complete"], row["archive_bytes"])
            days = sorted(by_day)
            carried: tuple[int, int] | None = None
            for day in days:
                if day < first_day:
                    carried = by_day[day]
            day = max(first_day, days[0])
            while day <= today:
                carried = by_day.get(day, carried)
                assert carried is not None
                out.append({
                    "date": day.isoformat(),
                    "source": source,
                    "collection": collection,
                    "complete": carried[0],
                    "archive_bytes": carried[1],
                })
                day += timedelta(days=1)
        return out
