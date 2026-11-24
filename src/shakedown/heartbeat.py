"""The scheduler's heartbeat, and the record of the stale-scheduler notice
(§spec:schedules).

About once a minute `shakedown schedule` replaces one heartbeat record: the time, its
release, whether it holds a valid schedule, and the next tick it plans. `serve` reads
it to say whether the scheduler is running, and records, once per episode, that it
sent the failure notice for a stale scheduler. Both are one row, replaced, never a
history.

Writing takes no writer lock and never migrates, like a rescheduled-sync entry
(§spec:rescheduled-syncs): it waits on SQLite the bounded time any writer waits, and a
write that cannot happen is logged and skipped, never raised. Unlike that entry, these
tables sit outside schema versioning: a writer finding an older database creates its
own table and leaves `schema_version` alone. Gating on the version would leave the
heartbeat unwritable after an upgrade until the first scheduled sync migrated the
database, days later, and the dashboard would report a running scheduler as stopped
all that time.

Times are stored as UTC with their offset: the scheduler and `serve` run in separate
containers, which may not share a `TZ`.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shakedown import db

log = logging.getLogger(__name__)

# A heartbeat older than this means the scheduler has stopped (§spec:schedules).
STALE_AFTER = timedelta(minutes=3)


@dataclass(frozen=True)
class Heartbeat:
    at: datetime
    release: str
    schedule_valid: bool
    next_tick_at: datetime | None


@dataclass(frozen=True)
class Notice:
    """The stale-scheduler notice serve sent: the episode is the heartbeat time the
    scheduler went stale on, so a later stop is a new episode."""
    episode_last_seen: datetime
    sent_at: datetime
    error: str | None


def _stored(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _parsed(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _write(state_db: Path, table_sql: str, insert: str, values: tuple[Any, ...], *,
           create: bool, what: str) -> bool:
    """Replace one record without the writer lock and without migrating. Returns
    whether it was written; never raises."""
    try:
        if not state_db.exists():
            if not create:
                return False
            # A fresh database at the current schema: nothing exists to migrate.
            db.connect(state_db).close()
        with closing(db.open_unmigrated(state_db)) as conn, db.transaction(conn):
            conn.execute(table_sql)
            conn.execute(insert, values)
    except sqlite3.Error as e:
        if e.sqlite_errorcode == sqlite3.SQLITE_BUSY:
            why = f"the state database stayed busy for more than {db.BUSY_TIMEOUT_MS / 1000:g}s"
        else:
            why = f"the state database could not be written ({e})"
        log.warning("%s not recorded: %s", what, why)
        return False
    return True


def record_heartbeat(state_db: Path, beat: Heartbeat, *, create: bool) -> bool:
    """Replace the heartbeat. ``create`` makes a missing database (the scheduler on a
    new install, before any run has written one)."""
    return _write(
        state_db,
        db.SCHEDULER_HEARTBEAT_TABLE,
        "INSERT OR REPLACE INTO scheduler_heartbeat "
        "(id, at, release, schedule_valid, next_tick_at) VALUES (1, ?, ?, ?, ?)",
        (_stored(beat.at), beat.release, int(beat.schedule_valid),
         _stored(beat.next_tick_at)),
        create=create,
        what="scheduler heartbeat",
    )


def record_notice(state_db: Path, notice: Notice) -> bool:
    """Replace the stale-scheduler notice record. Never creates the database."""
    return _write(
        state_db,
        db.SCHEDULER_NOTICE_TABLE,
        "INSERT OR REPLACE INTO scheduler_notice "
        "(id, episode_last_seen, sent_at, error) VALUES (1, ?, ?, ?)",
        (_stored(notice.episode_last_seen), _stored(notice.sent_at), notice.error),
        create=False,
        what="stale-scheduler notice",
    )


def _row(conn: sqlite3.Connection, sql: str) -> tuple[Any, ...] | None:
    try:
        return conn.execute(sql).fetchone()
    except sqlite3.OperationalError:
        return None  # an older database without the table: nothing recorded


def read_heartbeat(conn: sqlite3.Connection) -> Heartbeat | None:
    row = _row(conn, "SELECT at, release, schedule_valid, next_tick_at "
                     "FROM scheduler_heartbeat WHERE id = 1")
    if row is None:
        return None
    at = _parsed(row[0])
    assert at is not None  # NOT NULL column
    return Heartbeat(at=at, release=row[1], schedule_valid=bool(row[2]),
                     next_tick_at=_parsed(row[3]))


def read_notice(conn: sqlite3.Connection) -> Notice | None:
    row = _row(conn, "SELECT episode_last_seen, sent_at, error "
                     "FROM scheduler_notice WHERE id = 1")
    if row is None:
        return None
    episode, sent = _parsed(row[0]), _parsed(row[1])
    assert episode is not None and sent is not None  # NOT NULL columns
    return Notice(episode_last_seen=episode, sent_at=sent, error=row[2])


def state_of(beat: Heartbeat | None, now: datetime) -> str:
    """`running`, `stale` (heartbeat older than three minutes), `no_schedule` (running,
    holding no valid schedule), or `never` (no heartbeat recorded)."""
    if beat is None:
        return "never"
    if now - beat.at > STALE_AFTER:
        return "stale"
    return "running" if beat.schedule_valid else "no_schedule"


def scheduler_state(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """`scheduler: {state, last_seen, release}` for `GET /status` and
    `GET /api/health`. ``now`` is an aware datetime."""
    beat = read_heartbeat(conn)
    return {
        "state": state_of(beat, now),
        # Formatted like every `/api/*` time (dashboard.iso): the reader's local
        # offset. Not imported from there: the scheduler runs without serve's extras.
        "last_seen": beat.at.astimezone().isoformat() if beat else None,
        "release": beat.release if beat else None,
    }
