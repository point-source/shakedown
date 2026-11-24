"""Run error retention (§spec:run-error-retention): runs keep their per-show error
lines for `keep_run_errors_days`, then keep only their result, counts, and dates."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from shakedown.db import transaction
from shakedown.history import result_lines


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
