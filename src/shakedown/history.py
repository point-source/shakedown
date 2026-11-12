"""The run history (§spec:run-history): every run of the last N days, newest first.

Runs that have a run record (sync, catch_up, refetch, reconform) take every figure
from it — the record `status` reports — so the two always match; their activity
entry adds its id and the run's result. Restages, verifies, and reconciles have no
record and come from their activity entries, with the errors of the outcome a
restage or reconcile entry names. A run from before activity
entries existed comes from its record alone, its result read from the record the
way the run itself decided it."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from shakedown.activity import (
    ENDED,
    STOPPED_REPORTING,
    ActivityRepo,
    stopped_reporting,
)
from shakedown.models import ENDED_UNEXPECTEDLY, OperationStatus, Run, RunKind
from shakedown.state import OperationOutcomeRepo, RunRepo


@dataclass(frozen=True)
class RunEnd:
    """How a run ended: its outcome code, `stopped_reporting` or None, and when. All
    None while it runs."""
    result: str | None
    end_reason: str | None
    finished_at: datetime | None


_RUNNING = RunEnd(None, None, None)
_CRASH_PREFIXES = tuple(f"{kind.value} crashed: " for kind in RunKind)


def run_end(run: Run, entry: dict[str, Any] | None, now: datetime) -> RunEnd:
    """How a run record's run ended, from its activity entry (as
    `ActivityRepo.since` reads it) when that has ended, else from the record. Shared
    with the last good sync (§spec:library-stats)."""
    if entry is not None and entry["state"] == ENDED:
        return RunEnd(entry["result"], entry["end_reason"],
                      run.finished_at or entry["finished_at"])
    if entry is not None and run.finished_at is None:
        return _RUNNING  # its entry still reports
    return _record_end(run, now)


def _record_end(run: Run, now: datetime) -> RunEnd:
    """The outcome code a record shows, by the rules the run used to set it: a record
    left open is still running until it stops reporting (its start is its last sign
    of life); a killed run, closed by the next run or by serve, carries an error
    starting ENDED_UNEXPECTEDLY; then a
    failed enumeration, a crash, failed shows, or a clean finish."""
    failed = OperationStatus.FAILED_BEFORE_COMPLETION.value
    if run.finished_at is None:
        if stopped_reporting(run.started_at, now):
            return RunEnd(failed, STOPPED_REPORTING, run.started_at)
        return _RUNNING
    if any(error.startswith(ENDED_UNEXPECTEDLY) for error in run.errors):
        return RunEnd(failed, STOPPED_REPORTING, run.finished_at)
    if run.stale:
        result = OperationStatus.STALE_ENUMERATION.value
    elif any(error.startswith(_CRASH_PREFIXES) for error in run.errors):
        result = failed
    elif run.items_failed > 0:
        result = OperationStatus.COMPLETED_WITH_ITEM_ISSUES.value
    else:
        result = OperationStatus.COMPLETED.value
    return RunEnd(result, None, run.finished_at)


_GOOD = (OperationStatus.COMPLETED.value, OperationStatus.COMPLETED_WITH_ITEM_ISSUES.value)


def last_good_sync_at(
    conn: sqlite3.Connection, source: str, collection: str, now: datetime
) -> datetime | None:
    """When the collection's latest sync or catch-up that ended `completed` or
    `completed_with_item_issues` finished, or None (§spec:run-history "Last good
    sync"); each run's result read as the run list reads it."""
    activity = ActivityRepo(conn)
    for run in RunRepo(conn).finished(source, collection, (RunKind.SYNC, RunKind.CATCH_UP)):
        assert run.id is not None
        end = run_end(run, activity.for_run(run.id, now), now)
        if end.result in _GOOD:
            return run.finished_at
    return None


def _duration(started_at: datetime, finished_at: datetime | None) -> int | None:
    return int((finished_at - started_at).total_seconds()) if finished_at else None


def _from_record(run: Run, entry: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    end = run_end(run, entry, now)
    return {
        "run_id": run.id,
        "activity_id": entry["activity_id"] if entry else None,
        "kind": run.kind.value,
        "source": run.source_name,
        "collection": run.collection_name,
        "started_at": run.started_at,
        "finished_at": end.finished_at,
        "duration_seconds": _duration(run.started_at, end.finished_at),
        "result": end.result,
        "end_reason": end.end_reason,
        "items_discovered": run.items_discovered,
        "items_new": run.items_new,
        "items_updated": run.items_updated,
        "items_failed": run.items_failed,
        "bytes_downloaded": run.bytes_downloaded,
        "errors": run.errors,
        "failure_notice_error": run.failure_notice_error,
    }


def _from_entry(entry: dict[str, Any], outcomes: OperationOutcomeRepo) -> dict[str, Any]:
    """A run with no record. Its entry counts no new or updated shows. Its errors are
    those of the outcome it names (restage, reconcile); a verify records none. Only
    a sync sends a failure notice."""
    outcome = outcomes.get(entry["outcome_id"]) if entry["outcome_id"] else None
    return {
        "run_id": None,
        "activity_id": entry["activity_id"],
        "kind": entry["kind"],
        "source": entry["source"],
        "collection": entry["collection"],
        "started_at": entry["started_at"],
        "finished_at": entry["finished_at"],
        "duration_seconds": _duration(entry["started_at"], entry["finished_at"]),
        "result": entry["result"],
        "end_reason": entry["end_reason"],
        # A run's shows discovered are the shows its listing found (a restage or
        # verify lists none).
        "items_discovered": entry["items_listed"],
        "items_new": None,
        "items_updated": None,
        "items_failed": entry["items_failed"],
        "bytes_downloaded": entry["bytes_downloaded"],
        "errors": outcome.errors if outcome else None,
        "failure_notice_error": None,
    }


def run_history(conn: sqlite3.Connection, now: datetime, days: int) -> list[dict[str, Any]]:
    """Every run started in the last ``days`` days, newest first, each once. Times
    are datetimes; every other value is as `GET /api/runs` answers it."""
    since = now - timedelta(days=days)
    entries = ActivityRepo(conn).since(since, now)
    by_run = {e["run_id"]: e for e in entries if e["run_id"] is not None}
    rows = [_from_record(run, by_run.get(run.id), now) for run in RunRepo(conn).since(since)]
    outcomes = OperationOutcomeRepo(conn)
    rows += [_from_entry(e, outcomes) for e in entries if e["run_id"] is None]
    rows.sort(key=lambda r: (r["started_at"], r["run_id"] or 0, r["activity_id"] or 0),
              reverse=True)
    return rows
