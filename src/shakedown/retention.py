"""Run error retention (§spec:run-error-retention): runs keep their per-show error
lines for `keep_run_errors_days`, then keep only their result, counts, and dates."""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from shakedown.activity import ActivityKind, ActivityRecorder, ActivityRepo
from shakedown.config import Config
from shakedown.db import connect, transaction
from shakedown.history import result_lines
from shakedown.models import OperationStatus, OperationType
from shakedown.state import OperationOutcomeRepo
from shakedown.trigger import announce_opened, announce_started

log = logging.getLogger(__name__)


def trim_run_errors(conn: sqlite3.Connection, now: datetime, keep_days: int) -> int:
    """Drop the error lines of every finished run that started more than
    ``keep_days`` ago, keeping the lines its result is read from, and the error lines
    of finished operation outcomes as old. Each run that lost a line records when.
    Returns how many runs it trimmed."""
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    trimmed = 0
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT id, errors FROM runs
            WHERE started_at < ? AND finished_at IS NOT NULL
              AND detail_trimmed_at IS NULL AND errors != '[]'
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            errors = json.loads(row["errors"])
            kept = result_lines(errors)
            if len(kept) == len(errors):
                continue
            conn.execute(
                "UPDATE runs SET errors=?, detail_trimmed_at=? WHERE id=?",
                (json.dumps(kept), now.isoformat(), row["id"]),
            )
            trimmed += 1
        conn.execute(
            """
            UPDATE operation_outcomes SET errors='[]'
            WHERE started_at < ? AND finished_at IS NOT NULL AND errors != '[]'
            """,
            (cutoff,),
        )
    return trimmed


def run_trim_history(config: Config) -> int:
    """The `trim` run: under the writer lock, apply the retention rule at once, then
    compact the database so the freed space returns to the disk. Exit 1 when another
    run holds the lock or the compaction failed; the trim itself stands either way."""
    from shakedown.lock import run_exclusive  # lock runs this module's trim at its end

    return run_exclusive(config, lambda: _run_trim(config))


def _run_trim(config: Config) -> int:
    db = config.state_db
    assert db is not None  # filled in by Config validation
    with contextlib.closing(connect(db)) as conn, ActivityRecorder(db) as recorder:
        outcomes = OperationOutcomeRepo(conn)
        # A trim belongs to no collection: its records name none.
        progress = recorder.open_entry("", "", ActivityKind.TRIM)
        # A trim has no phases; the dashboard draws none for it (operator, 2026-11-24).
        progress.phase(None)
        outcome = outcomes.start(OperationType.TRIM, "", "", datetime.now())
        # The run history shows this outcome's errors (§spec:run-history).
        progress.outcome_id = outcome.id
        announce_started()
        announce_opened(None, outcome.id, "", "")

        size_before = _db_bytes(db)
        runs_trimmed = trim_run_errors(conn, datetime.now(), config.keep_run_errors_days)
        errors: list[str] = []
        try:
            _vacuum(db)
        except sqlite3.Error as e:
            log.error("could not compact the state database: %s", e)
            errors.append(f"compaction failed: {e}")
        bytes_freed = max(0, size_before - _db_bytes(db)) if not errors else 0
        # No space came back, so the run fails (operator, 2026-11-24).
        status = (OperationStatus.FAILED_BEFORE_COMPLETION if errors
                  else OperationStatus.COMPLETED)
        outcomes.finish(outcome, status, datetime.now(),
                        completed_work={"runs_trimmed": runs_trimmed,
                                        "bytes_freed": bytes_freed},
                        errors=errors)
        if progress.activity_id is not None:
            ActivityRepo(conn).record_trim(progress.activity_id, runs_trimmed, bytes_freed)
        recorder.end(progress, status)
        log.info("trimmed the error lines of %d runs; the database shrank by %d bytes",
                 runs_trimmed, bytes_freed)
        return 0 if not errors else 1


def _db_bytes(db: Path) -> int:
    """The database's size on disk: its file and its write-ahead log."""
    wal = db.with_name(db.name + "-wal")
    return sum(p.stat().st_size for p in (db, wal) if p.exists())


def _vacuum(db: Path) -> None:
    """Rewrite the database into the least space, then fold the write-ahead log back
    into the file and empty it, so the file itself shrinks. Needs about the
    database's size free; SQLite raises SQLITE_FULL when it lacks the room."""
    with contextlib.closing(connect(db)) as conn:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
