"""Live run progress (§spec:run-activity): each run records, per collection it works
on, an activity entry — its kind, phase, counters, heartbeat, and result — into the
state database, so the dashboard shows it whether or not `serve` was running when the
run started."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from shakedown.db import transaction

# History is kept this long; trimming never touches item, run, or placement records
# (§spec:state).
HISTORY_DAYS = 30


class ActivityKind(StrEnum):
    SYNC = "sync"
    CATCH_UP = "catch_up"
    REFETCH = "refetch"
    RECONFORM = "reconform"
    RESTAGE = "restage"
    VERIFY = "verify"
    RECONCILE = "reconcile"


class Phase(StrEnum):
    STARTING = "starting"
    LISTING = "listing"
    DISCOVERING = "discovering"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    STAGING = "staging"
    NOTIFYING = "notifying"
    VERIFYING = "verifying"
    FINISHING = "finishing"


RUNNING = "running"
ENDED = "ended"


@dataclass(frozen=True)
class RunCounts:
    """A run record's figures, written mid-run at the activity entry's cadence so
    `status` and the dashboard agree (§spec:run-activity)."""
    items_discovered: int
    items_new: int
    items_updated: int
    items_failed: int
    bytes_downloaded: int


@dataclass
class Snapshot:
    """An entry's progress at one moment: cumulative, so a skipped write loses
    nothing the next one does not carry."""
    phase: str | None = Phase.STARTING
    last_progress_at: datetime | None = None
    items_listed: int | None = None
    items_to_look_up: int | None = None
    items_looked_up: int | None = None
    items_to_fetch: int | None = None
    items_done: int | None = None
    items_failed: int | None = None
    current_items: list[str] = field(default_factory=list)
    bytes_downloaded: int | None = None
    bytes_to_fetch: int | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: int | None = None
    run_counts: RunCounts | None = None


_PROGRESS_COLUMNS = (
    "phase", "items_listed", "items_to_look_up", "items_looked_up", "items_to_fetch",
    "items_done", "items_failed", "bytes_downloaded", "bytes_to_fetch",
    "speed_bytes_per_second", "eta_seconds",
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class ActivityRepo:
    """The `run_activity` table (schema v10). A reader of an older schema has no
    table and sees no entries."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def has_table(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_activity'"
        ).fetchone() is not None

    def create(
        self,
        source: str,
        collection: str,
        kind: ActivityKind,
        *,
        run_id: int | None,
        started_by_serve: bool,
        at: datetime,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO run_activity (
                source_name, collection_name, kind, started_by_serve, run_id,
                started_at, last_heartbeat_at, last_progress_at, state, phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, collection, kind.value, int(started_by_serve), run_id,
             _iso(at), _iso(at), _iso(at), RUNNING, Phase.STARTING.value),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def restart(self, activity_id: int, at: datetime) -> None:
        """Move an entry opened ahead of time to when its work actually began."""
        self.conn.execute(
            "UPDATE run_activity SET started_at=? WHERE id=?", (_iso(at), activity_id)
        )

    def write(self, activity_id: int, snap: Snapshot, heartbeat_at: datetime,
              run_id: int | None = None) -> None:
        """Record a running entry's progress and heartbeat; with ``run_id``, move the
        open run record's figures to the same moment."""
        values = [getattr(snap, column) for column in _PROGRESS_COLUMNS]
        self.conn.execute(
            f"""
            UPDATE run_activity SET
                {", ".join(f"{column}=?" for column in _PROGRESS_COLUMNS)},
                current_items=?, last_heartbeat_at=?,
                last_progress_at=COALESCE(?, last_progress_at)
            WHERE id=? AND state=?
            """,
            (*values, json.dumps(snap.current_items), _iso(heartbeat_at),
             snap.last_progress_at and _iso(snap.last_progress_at), activity_id, RUNNING),
        )
        if run_id is not None and snap.run_counts is not None:
            counts = snap.run_counts
            self.conn.execute(
                """
                UPDATE runs SET items_discovered=?, items_new=?, items_updated=?,
                    items_failed=?, bytes_downloaded=?
                WHERE id=? AND finished_at IS NULL
                """,
                (counts.items_discovered, counts.items_new, counts.items_updated,
                 counts.items_failed, counts.bytes_downloaded, run_id),
            )

    def end(
        self,
        activity_id: int,
        result: str,
        finished_at: datetime,
        snap: Snapshot | None = None,
        *,
        end_reason: str | None = None,
    ) -> None:
        """Record an entry's result. Its last progress stays; nothing is in flight
        any more, so speed, time left, and current items clear."""
        if snap is not None:
            self.write(activity_id, snap, finished_at)
        self.conn.execute(
            """
            UPDATE run_activity SET state=?, result=?, end_reason=?, finished_at=?,
                last_heartbeat_at=?, current_items='[]', speed_bytes_per_second=NULL,
                eta_seconds=NULL
            WHERE id=? AND state=?
            """,
            (ENDED, result, end_reason, _iso(finished_at), _iso(finished_at),
             activity_id, RUNNING),
        )

    def trim(self, now: datetime) -> None:
        """Drop ended entries older than the history window."""
        cutoff = now - timedelta(days=HISTORY_DAYS)
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM run_activity WHERE state=? AND finished_at < ?",
                (ENDED, _iso(cutoff)),
            )
