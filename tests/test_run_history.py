"""The run history (§spec:run-history): every run of the last N days, newest first.
Runs with a run record take every figure from it; restages, verifies, and reconciles
come from their activity entries; a run with both appears once."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shakedown.activity import (
    ABANDONED_ERROR,
    ActivityKind,
    ActivityRepo,
    Snapshot,
    close_abandoned,
)
from shakedown.db import connect
from shakedown.history import run_history
from shakedown.models import OperationStatus, OperationType, Run, RunKind
from shakedown.state import OperationOutcomeRepo, RunRepo


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    connect(path).close()
    return path


def _record(conn: sqlite3.Connection, started: datetime, *,
            finished: datetime | None = None, kind: RunKind = RunKind.SYNC,
            **figures: object) -> Run:
    runs = RunRepo(conn)
    run = runs.start("fake-src", "coll1", started, kind=kind)
    if finished is not None:
        run.finished_at = finished
        for key, value in figures.items():
            setattr(run, key, value)
        runs.finish(run)
    return run


def _entry(conn: sqlite3.Connection, kind: ActivityKind, started: datetime, *,
           run_id: int | None = None, ended: datetime | None = None,
           result: str = "completed", heartbeat: datetime | None = None,
           snap: Snapshot | None = None) -> int:
    repo = ActivityRepo(conn)
    activity_id = repo.create("fake-src", "coll1", kind, run_id=run_id,
                              started_by_serve=False, at=started)
    if snap is not None:
        repo.write(activity_id, snap, heartbeat or started)
    if ended is not None:
        repo.end(activity_id, result, ended)
    elif heartbeat is not None:
        conn.execute("UPDATE run_activity SET last_heartbeat_at=? WHERE id=?",
                     (heartbeat.isoformat(), activity_id))
    return activity_id


def _history(db: Path, days: int = 30, now: datetime | None = None) -> list[dict]:
    with closing(connect(db)) as conn:
        return run_history(conn, now or datetime.now(), days)


def test_runs_with_and_without_records_form_one_list_newest_first(db: Path) -> None:
    now = datetime.now()
    with closing(connect(db)) as conn:
        older = now - timedelta(hours=5)
        sync = _record(conn, older, finished=older + timedelta(minutes=30),
                       items_discovered=12, items_new=3, items_updated=1,
                       items_failed=0, bytes_downloaded=4096)
        sync_entry = _entry(conn, ActivityKind.SYNC, older, run_id=sync.id,
                            ended=older + timedelta(minutes=30))
        verify_at = now - timedelta(hours=2)
        verify = _entry(conn, ActivityKind.VERIFY, verify_at,
                        ended=verify_at + timedelta(minutes=4),
                        result="completed_with_item_issues",
                        snap=Snapshot(items_to_fetch=10, items_done=9, items_failed=1))
    history = _history(db)
    assert [(r["kind"], r["run_id"], r["activity_id"]) for r in history] == [
        ("verify", None, verify),
        ("sync", sync.id, sync_entry),
    ]
    verify_row, sync_row = history
    assert sync_row == {
        "run_id": sync.id, "activity_id": sync_entry, "kind": "sync",
        "source": "fake-src", "collection": "coll1",
        "started_at": older, "finished_at": older + timedelta(minutes=30),
        "duration_seconds": 1800, "result": "completed", "end_reason": None,
        "items_discovered": 12, "items_new": 3, "items_updated": 1, "items_failed": 0,
        "bytes_downloaded": 4096, "errors": [], "failure_notice_error": None,
    }
    assert verify_row["result"] == "completed_with_item_issues"
    assert verify_row["duration_seconds"] == 240
    # An activity entry counts no new or updated shows; only a sync sends a failure
    # notice; a verify records no outcome, so it has no errors.
    assert verify_row["items_failed"] == 1
    assert verify_row["items_new"] is None and verify_row["items_updated"] is None
    assert verify_row["errors"] is None and verify_row["failure_notice_error"] is None


@pytest.mark.parametrize("kind, operation", [
    (ActivityKind.RESTAGE, OperationType.RESTAGE),
    (ActivityKind.RECONCILE, OperationType.RECONCILE),
])
def test_a_restage_or_reconcile_shows_the_errors_its_outcome_records(
    db: Path, kind: ActivityKind, operation: OperationType,
) -> None:
    at = datetime.now() - timedelta(minutes=10)
    with closing(connect(db)) as conn:
        outcomes = OperationOutcomeRepo(conn)
        outcome = outcomes.start(operation, "fake-src", "coll1", at)
        outcomes.finish(outcome, OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
                        at + timedelta(minutes=1), errors=["gd-a: staging failed"])
        _entry(conn, kind, at, ended=at + timedelta(minutes=1),
               result="completed_with_item_issues",
               snap=Snapshot(outcome_id=outcome.id))
        unlinked = at + timedelta(minutes=5)  # recorded before entries named outcomes
        _entry(conn, kind, unlinked, ended=unlinked + timedelta(minutes=1))
    newer, linked = _history(db)
    assert linked["errors"] == ["gd-a: staging failed"]
    assert linked["failure_notice_error"] is None
    assert newer["errors"] is None


def test_a_reconcile_reports_its_listing_as_shows_discovered(db: Path) -> None:
    at = datetime.now() - timedelta(minutes=10)
    with closing(connect(db)) as conn:
        _entry(conn, ActivityKind.RECONCILE, at, ended=at + timedelta(minutes=1),
               snap=Snapshot(items_listed=40))
    [row] = _history(db)
    assert row["items_discovered"] == 40
    assert row["bytes_downloaded"] is None


def test_the_window_keeps_runs_started_in_the_last_n_days(db: Path) -> None:
    now = datetime.now()
    with closing(connect(db)) as conn:
        two_days = now - timedelta(days=2)
        old = _record(conn, two_days, finished=two_days + timedelta(minutes=1))
        _entry(conn, ActivityKind.SYNC, two_days, run_id=old.id,
               ended=two_days + timedelta(minutes=1))
        _entry(conn, ActivityKind.RESTAGE, two_days, ended=two_days + timedelta(minutes=1))
        recent = now - timedelta(hours=1)
        new = _record(conn, recent, finished=recent + timedelta(minutes=1))
    assert [r["run_id"] for r in _history(db, days=1)] == [new.id]
    assert len(_history(db, days=3)) == 3


def test_a_running_run_has_no_end_yet(db: Path) -> None:
    now = datetime.now()
    with closing(connect(db)) as conn:
        run = _record(conn, now - timedelta(minutes=3))
        _entry(conn, ActivityKind.SYNC, now - timedelta(minutes=3), run_id=run.id,
               heartbeat=now - timedelta(seconds=2))
        _entry(conn, ActivityKind.VERIFY, now - timedelta(minutes=1),
               heartbeat=now - timedelta(seconds=1))
    for row in _history(db):
        assert row["finished_at"] is None and row["duration_seconds"] is None
        assert row["result"] is None and row["end_reason"] is None


def test_a_killed_run_reads_as_stopped_reporting_at_its_last_heartbeat(db: Path) -> None:
    now = datetime.now()
    started = now - timedelta(minutes=5)
    heartbeat = now - timedelta(minutes=1)
    with closing(connect(db)) as conn:
        run = _record(conn, started)
        _entry(conn, ActivityKind.SYNC, started, run_id=run.id, heartbeat=heartbeat)
        verify = _entry(conn, ActivityKind.VERIFY, started - timedelta(minutes=1),
                        heartbeat=heartbeat)
    sync_row, verify_row = _history(db, now=now)
    for row in (sync_row, verify_row):
        assert row["result"] == "failed_before_completion"
        assert row["end_reason"] == "stopped_reporting"
        assert row["finished_at"] == heartbeat
    assert sync_row["run_id"] == run.id and verify_row["activity_id"] == verify
    assert sync_row["duration_seconds"] == 240


def test_a_killed_run_closed_by_the_next_run_still_reads_stopped_reporting(
    db: Path,
) -> None:
    now = datetime.now()
    started = now - timedelta(minutes=5)
    heartbeat = now - timedelta(minutes=1)
    with closing(connect(db)) as conn:
        run = _record(conn, started)
        _entry(conn, ActivityKind.SYNC, started, run_id=run.id, heartbeat=heartbeat)
        close_abandoned(conn, now)
    [row] = _history(db, now=now)
    assert (row["result"], row["end_reason"]) == ("failed_before_completion",
                                                  "stopped_reporting")
    assert row["finished_at"] == heartbeat
    assert row["errors"] == [ABANDONED_ERROR]


def test_pre_release_records_appear_alone_with_their_result_from_the_record(
    db: Path,
) -> None:
    now = datetime.now()
    at = [now - timedelta(hours=h) for h in range(6, 0, -1)]
    with closing(connect(db)) as conn:
        clean = _record(conn, at[0], finished=at[0] + timedelta(minutes=1))
        issues = _record(conn, at[1], finished=at[1] + timedelta(minutes=1),
                         items_failed=2, kind=RunKind.REFETCH)
        stale = _record(conn, at[2], finished=at[2] + timedelta(minutes=1), stale=True,
                        kind=RunKind.CATCH_UP)
        crashed = _record(conn, at[3], finished=at[3] + timedelta(minutes=1),
                          items_failed=1, errors=["x: 404", "sync crashed: boom"],
                          kind=RunKind.CATCH_UP)
        abandoned = _record(conn, at[4])
        close_abandoned(conn, now)
        open_long_ago = _record(conn, at[5])
    results = {r["run_id"]: (r["result"], r["end_reason"], r["activity_id"])
               for r in _history(db, now=now)}
    assert results == {
        clean.id: ("completed", None, None),
        issues.id: ("completed_with_item_issues", None, None),
        stale.id: ("stale_enumeration", None, None),
        crashed.id: ("failed_before_completion", None, None),
        abandoned.id: ("failed_before_completion", "stopped_reporting", None),
        open_long_ago.id: ("failed_before_completion", "stopped_reporting", None),
    }


def test_a_pre_v10_database_lists_its_run_records(db: Path) -> None:
    at = datetime.now() - timedelta(hours=1)
    with closing(connect(db)) as conn:
        run = _record(conn, at, finished=at + timedelta(minutes=2), items_new=5)
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        [row] = run_history(conn, datetime.now(), 30)
    assert (row["run_id"], row["activity_id"], row["items_new"]) == (run.id, None, 5)
    assert row["result"] == "completed"


def test_a_closed_record_whose_entry_has_not_ended_yet_reads_from_the_record(
    db: Path,
) -> None:
    """The moment between closing the record and ending the entry."""
    now = datetime.now()
    started = now - timedelta(minutes=2)
    with closing(connect(db)) as conn:
        run = _record(conn, started, finished=now, items_failed=1)
        _entry(conn, ActivityKind.SYNC, started, run_id=run.id, heartbeat=now)
    [row] = _history(db, now=now)
    assert row["finished_at"] == now
    assert row["result"] == "completed_with_item_issues"
