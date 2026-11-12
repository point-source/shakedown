"""Syncs skipped because another run holds the writer lock (§spec:rescheduled-syncs).

A `sync --collection` that finds the lock held records each (source, collection) it
covers, once, keeping the time of its oldest skip, so `status` shows it until a sync
finishes that collection.

Recording takes no writer lock and never migrates the database: it waits on SQLite
the bounded time any writer waits, and writes only into a database already at the
schema version that has the record. A database still at an older version means a run
of the previous release is writing; migrating under it would make that run refuse
the database the next time it opens it. Whenever the entry cannot be written, nothing
is recorded, the log says why, and the skipped sync exits 1 exactly as before.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime

from shakedown import db
from shakedown.config import Config
from shakedown.state import RescheduledRepo

log = logging.getLogger(__name__)


def record_skipped(config: Config, scope: Sequence[tuple[str, str]]) -> None:
    """Record each (source, collection) of a sync refused by the writer lock. Never
    raises: a skip that cannot be recorded is logged and left out."""
    assert config.state_db is not None  # filled in by Config validation
    names = ", ".join(f"{source}/{collection}" for source, collection in scope)
    try:
        with closing(db.open_unmigrated(config.state_db)) as conn, db.transaction(conn):
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            if version < db.RESCHEDULED_SINCE_VERSION:
                log.warning(
                    "%s: not rescheduled: the state database is at schema v%d "
                    "because a run of the previous release is writing; "
                    "nothing recorded",
                    names, version,
                )
                return
            repo = RescheduledRepo(conn)
            now = datetime.now()
            recorded = [(s, c, repo.record(s, c, now)) for s, c in scope]
    except sqlite3.Error as e:
        if e.sqlite_errorcode == sqlite3.SQLITE_BUSY:
            why = f"the state database stayed busy for more than {db.BUSY_TIMEOUT_MS / 1000:g}s"
        else:
            why = f"the state database could not be written ({e})"
        log.warning("%s: not rescheduled: %s; nothing recorded", names, why)
        return
    for source, collection, since in recorded:
        log.warning(
            "%s/%s: sync rescheduled: another run holds the writer lock; "
            "oldest skip %s; it runs when that run finishes, no rerun needed",
            source, collection, since.isoformat(),
        )

