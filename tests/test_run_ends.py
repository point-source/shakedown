"""Ends are never missed (§spec:run-activity, §spec:failure-behavior "Run killed or
crashed outside serve"): a run that stops reporting reads as ended unexpectedly, and
the next run that takes the writer lock closes its record."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shakedown.activity import (
    ABANDONED_ERROR,
    ActivityKind,
    ActivityRepo,
    close_abandoned,
)
from shakedown.db import connect
from shakedown.models import OperationStatus, OperationType
from shakedown.restage import run_restage
from shakedown.state import OperationOutcomeRepo, RunRepo
from shakedown.status import print_status
from tests.conftest import make_config


def _abandoned_run(db: Path, heartbeat_age_s: float) -> tuple[int, int, int, datetime]:
    """A sync left open by a killed process: its run record, outcome, and activity
    entry, last heard from ``heartbeat_age_s`` ago."""
    now = datetime.now()
    started = now - timedelta(minutes=5)
    heartbeat = now - timedelta(seconds=heartbeat_age_s)
    with closing(connect(db)) as conn:
        run = RunRepo(conn).start("fake-src", "coll1", started)
        outcome = OperationOutcomeRepo(conn).start(
            OperationType.SYNC, "fake-src", "coll1", started, phase="discover")
        activity = ActivityRepo(conn)
        activity_id = activity.create("fake-src", "coll1", ActivityKind.SYNC,
                                      run_id=run.id, started_by_serve=False, at=started)
        conn.execute("UPDATE run_activity SET last_heartbeat_at=? WHERE id=?",
                     (heartbeat.isoformat(), activity_id))
    assert run.id is not None and outcome.id is not None
    return run.id, outcome.id, activity_id, heartbeat


def _status_json(config, capsys: pytest.CaptureFixture[str]) -> dict:
    print_status(config, as_json=True)
    [summary] = json.loads(capsys.readouterr().out)
    return summary


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]):
    return make_config(*tmp_roots)


def test_close_abandoned_closes_records_at_their_last_heartbeat(config) -> None:
    db = config.state_db
    run_id, outcome_id, activity_id, heartbeat = _abandoned_run(db, 60)
    with closing(connect(db)) as conn:
        fresh = ActivityRepo(conn).create("fake-src", "coll1", ActivityKind.VERIFY,
                                          run_id=None, started_by_serve=False,
                                          at=datetime.now())
        close_abandoned(conn, datetime.now())

        run = RunRepo(conn).latest("fake-src", "coll1")
        assert run is not None and run.id == run_id
        assert run.finished_at == heartbeat
        assert run.errors == [ABANDONED_ERROR]
        assert ABANDONED_ERROR == "run ended unexpectedly (stopped reporting)"
        outcome = OperationOutcomeRepo(conn).get(outcome_id)
        assert outcome is not None
        assert outcome.status == OperationStatus.FAILED_BEFORE_COMPLETION
        assert ABANDONED_ERROR in outcome.errors
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM run_activity")}
    ended = rows[activity_id]
    assert (ended["state"], ended["result"], ended["end_reason"]) == (
        "ended", "failed_before_completion", "stopped_reporting")
    assert ended["finished_at"] == heartbeat.isoformat()
    # A lock-free verify still reporting is alive: left running.
    assert rows[fresh]["state"] == "running"


def test_an_open_record_without_an_entry_closes_at_its_start(config) -> None:
    """A record left open before this release has no heartbeat: it closes at the
    time it started."""
    started = datetime(2026, 10, 1, 3, 0)
    with closing(connect(config.state_db)) as conn:
        RunRepo(conn).start("fake-src", "coll1", started)
        close_abandoned(conn, datetime.now())
        run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.finished_at == started
    assert run.errors == [ABANDONED_ERROR]


def test_status_reports_a_stale_open_run_as_ended_unexpectedly(
    config, capsys: pytest.CaptureFixture[str]
) -> None:
    _abandoned_run(config.state_db, 30)
    summary = _status_json(config, capsys)
    assert summary["last_run"]["finished_at"] is None
    assert summary["last_run"]["end_reason"] == "stopped_reporting"

    print_status(config, as_json=False)
    assert "ended unexpectedly (stopped reporting)" in capsys.readouterr().out


def test_status_shows_a_reporting_run_as_running(
    config, capsys: pytest.CaptureFixture[str]
) -> None:
    _abandoned_run(config.state_db, 5)
    summary = _status_json(config, capsys)
    assert summary["last_run"]["end_reason"] is None
    print_status(config, as_json=False)
    assert "ended unexpectedly" not in capsys.readouterr().out


def test_status_on_a_pre_v10_database_reports_no_end_reason(
    config, capsys: pytest.CaptureFixture[str]
) -> None:
    with closing(connect(config.state_db)) as conn:
        RunRepo(conn).start("fake-src", "coll1", datetime.now() - timedelta(hours=1))
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    summary = _status_json(config, capsys)
    assert summary["last_run"]["end_reason"] is None
    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_the_next_locked_run_closes_an_abandoned_record(config) -> None:
    run_id, _outcome_id, _activity_id, heartbeat = _abandoned_run(config.state_db, 60)
    assert run_restage(config) == 0
    with closing(connect(config.state_db)) as conn:
        row = conn.execute("SELECT finished_at, errors FROM runs WHERE id=?",
                           (run_id,)).fetchone()
    assert row["finished_at"] == heartbeat.isoformat()
    assert json.loads(row["errors"]) == [ABANDONED_ERROR]


def test_a_closer_failure_never_stops_the_locked_run(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shakedown.activity as activity

    def boom(conn, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(activity, "close_abandoned", boom)
    assert run_restage(config) == 0


@pytest.mark.parametrize(
    ("operation", "command"),
    [
        (OperationType.RESTAGE, "restage"),
        (OperationType.RECONCILE, "reconcile"),
        (OperationType.REFETCH, "item refetch"),
        (OperationType.RECONFORM, "verify --deep --reconform"),
        (OperationType.SYNC, "sync"),
    ],
)
def test_a_closed_outcome_names_the_command_that_reruns_it(
    config, operation: OperationType, command: str
) -> None:
    """A killed restage is not fixed by rerunning sync: the closed outcome's next step
    names its own command."""
    with closing(connect(config.state_db)) as conn:
        outcome = OperationOutcomeRepo(conn).start(
            operation, "fake-src", "coll1", datetime.now() - timedelta(minutes=1))
        close_abandoned(conn, datetime.now())
        assert outcome.id is not None
        closed = OperationOutcomeRepo(conn).get(outcome.id)
    assert closed is not None
    assert closed.status == OperationStatus.FAILED_BEFORE_COMPLETION
    assert closed.safe_next_action == f"check the logged error and rerun {command}"


def test_status_still_reports_a_killed_run_once_it_is_closed(
    config, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the next locked run closes it, status and the run list still agree."""
    _abandoned_run(config.state_db, 60)
    assert run_restage(config) == 0
    summary = _status_json(config, capsys)
    assert summary["last_run"]["finished_at"] is not None
    assert summary["last_run"]["end_reason"] == "stopped_reporting"
