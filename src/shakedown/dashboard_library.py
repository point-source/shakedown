"""`GET /api/library`: what the mirror holds, the disk it uses and has left, how it
grew, what was mirrored most recently, and roughly when the disk fills
(§spec:library-stats)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from shakedown.activity import ENDED, ActivityRepo
from shakedown.models import OperationStatus, RunKind

_GOOD = (OperationStatus.COMPLETED.value, OperationStatus.COMPLETED_WITH_ITEM_ISSUES.value)
_SYNC_KINDS = (RunKind.SYNC.value, RunKind.CATCH_UP.value)
_CRASH_PREFIXES = tuple(f"{kind.value} crashed: " for kind in RunKind)
# Both closers of a killed run — the next locked run and serve — start with this.
_ABANDONED_PREFIX = "run ended unexpectedly"


def last_good_sync_at(conn: sqlite3.Connection, source: str, collection: str) -> datetime | None:
    """When the collection's latest sync or catch-up that ended `completed` or
    `completed_with_item_issues` finished, or None (§spec:run-history "Last good
    sync"). The result is its activity entry's once that has ended; a run from before
    entries existed is read from its record by the rules the run used to set it."""
    with_entries = ActivityRepo(conn).has_table()
    entry_cols = ", a.state AS entry_state, a.result AS entry_result" if with_entries else ""
    join = "LEFT JOIN run_activity a ON a.run_id = r.id" if with_entries else ""
    rows = conn.execute(
        f"""
        SELECT r.finished_at, r.stale, r.errors, r.items_failed {entry_cols}
        FROM runs r {join}
        WHERE r.source_name=? AND r.collection_name=? AND r.finished_at IS NOT NULL
          AND r.kind IN ({", ".join("?" * len(_SYNC_KINDS))})
        ORDER BY r.finished_at DESC, r.id DESC
        """,
        (source, collection, *_SYNC_KINDS),
    )
    for row in rows:
        if with_entries and row["entry_state"] == ENDED:
            result = row["entry_result"]
        else:
            result = _record_result(row)
        if result in _GOOD:
            return datetime.fromisoformat(row["finished_at"])
    return None


def _record_result(row: sqlite3.Row) -> str:
    """A closed run record's outcome code: a killed run, a failed listing, a crash,
    failed shows, or a clean finish (sync's `_finish_outcome`, `_close_crashed`)."""
    errors: list[str] = json.loads(row["errors"])
    if any(e.startswith(_ABANDONED_PREFIX) or e.startswith(_CRASH_PREFIXES) for e in errors):
        return OperationStatus.FAILED_BEFORE_COMPLETION.value
    if row["stale"]:
        return OperationStatus.STALE_ENUMERATION.value
    if row["items_failed"] > 0:
        return OperationStatus.COMPLETED_WITH_ITEM_ISSUES.value
    return OperationStatus.COMPLETED.value
