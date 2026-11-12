"""`GET /api/library`: what the mirror holds, the disk it uses and has left, how it
grew, what was mirrored most recently, and roughly when the disk fills
(§spec:library-stats)."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shakedown.activity import ENDED, ActivityRepo
from shakedown.dashboard import DashboardContext, iso
from shakedown.growth import GrowthRepo
from shakedown.models import ItemStatus, OperationStatus, RunKind
from shakedown.state import ItemRepo
from shakedown.status import library_figures

_GOOD = (OperationStatus.COMPLETED.value, OperationStatus.COMPLETED_WITH_ITEM_ISSUES.value)
_SYNC_KINDS = (RunKind.SYNC.value, RunKind.CATCH_UP.value)
_CRASH_PREFIXES = tuple(f"{kind.value} crashed: " for kind in RunKind)
# How many of the most recently mirrored shows the view lists.
RECENT_SHOWS = 20
# The disk-full estimate needs at least this many days of growth.
_MIN_GROWTH_DAYS = 7
# Both closers of a killed run — the next locked run and serve — start with this.
_ABANDONED_PREFIX = "run ended unexpectedly"


def library_answer(ctx: DashboardContext) -> dict[str, Any]:
    """Per collection and in total, what the mirror holds — the figures `status`
    reports, from the same computation — plus the disk, growth, and the shows
    mirrored most recently."""
    config, conn = ctx.config, ctx.conn
    scopes = [(s.name, c.name) for s in config.sources for c in s.collections]
    items = ItemRepo(conn)
    collections = []
    for source, collection in scopes:
        figures = library_figures(items, source, collection)
        collections.append({
            "source": source,
            "collection": collection,
            "counts": figures["counts"],
            "restricted": len(figures["restricted"]),
            "archive_bytes": figures["bytes_on_disk"],
            "library_bytes": items.library_bytes(source, collection),
            "last_successful_sync_at": iso(last_good_sync_at(conn, source, collection)),
        })
    totals = {
        "counts": {s.value: sum(c["counts"][s.value] for c in collections) for s in ItemStatus},
        "restricted": sum(c["restricted"] for c in collections),
        "archive_bytes": sum(c["archive_bytes"] for c in collections),
        "library_bytes": sum(c["library_bytes"] for c in collections),
    }
    growth_repo = GrowthRepo(conn)
    growth = growth_repo.series(scopes, date.today()) if growth_repo.has_table() else []
    total_bytes, free_bytes = disk_usage(config.archive_root)
    return {
        "disk": {
            "total_bytes": total_bytes,
            "free_bytes": free_bytes,
            "full_in_days": full_in_days(growth, free_bytes),
        },
        "totals": totals,
        "collections": collections,
        "growth": growth,
        "recently_mirrored": [
            {"source": source, "collection": collection, "identifier": identifier,
             "title": title, "downloaded_at": iso(downloaded_at)}
            for source, collection, identifier, downloaded_at, title
            in items.recently_downloaded(scopes, RECENT_SHOWS)
        ],
    }


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


def disk_usage(path: Path) -> tuple[int, int]:
    """Total and free bytes of the filesystem holding ``path``, as `df` reports them
    (free = available to an unprivileged process), read from the OS: no walk."""
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def full_in_days(growth: list[dict[str, Any]], free_bytes: int) -> int | None:
    """Days until free space runs out at the average daily growth of archive bytes
    over the growth rows (all collections summed per day). None with fewer than
    seven days of growth, or no growth."""
    totals: dict[str, int] = defaultdict(int)
    for row in growth:
        totals[row["date"]] += row["archive_bytes"]
    if not totals:
        return None
    first, last = min(totals), max(totals)
    span = (date.fromisoformat(last) - date.fromisoformat(first)).days
    grown = totals[last] - totals[first]
    if span < _MIN_GROWTH_DAYS or grown <= 0:
        return None
    return int(free_bytes * span // grown)
