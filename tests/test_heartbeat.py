"""The scheduler's heartbeat and the stale-scheduler notice record (§spec:schedules):
one row each, replaced, written without the writer lock and without migrating."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shakedown import db, heartbeat
from shakedown.heartbeat import Heartbeat, Notice

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)


def _beat(at: datetime = NOW, *, valid: bool = True) -> Heartbeat:
    return Heartbeat(at=at, release="9.9.9", schedule_valid=valid,
                     next_tick_at=at + timedelta(hours=1))


def _older_database(path: Path) -> None:
    """A database as the previous release left it: no scheduler tables, schema v13."""
    with closing(db.connect(path)) as conn:
        conn.execute("DROP TABLE scheduler_heartbeat")
        conn.execute("DROP TABLE scheduler_notice")
        conn.execute("UPDATE schema_version SET version = 13")


def _version(path: Path) -> int:
    with closing(db.open_unmigrated(path)) as conn:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_a_heartbeat_is_one_record_replaced_each_time(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    assert heartbeat.record_heartbeat(path, _beat(), create=True)
    later = _beat(NOW + timedelta(minutes=1), valid=False)
    assert heartbeat.record_heartbeat(path, later, create=True)
    with closing(db.connect_reader(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_heartbeat").fetchone()[0] == 1
        assert heartbeat.read_heartbeat(conn) == later


def test_writing_into_an_older_database_adds_the_table_without_migrating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    _older_database(path)
    assert heartbeat.record_heartbeat(path, _beat(), create=True)
    assert heartbeat.record_notice(path, Notice(NOW, NOW, None))
    assert _version(path) == 13
    with closing(db.open_unmigrated(path)) as conn:
        assert heartbeat.read_heartbeat(conn) == _beat()
        assert heartbeat.read_notice(conn) == Notice(NOW, NOW, None)


def test_the_scheduler_creates_a_missing_database_but_serve_never_does(
    tmp_path: Path,
) -> None:
    serve_side = tmp_path / "serve" / "state.db"
    assert not heartbeat.record_notice(serve_side, Notice(NOW, NOW, None))
    assert not serve_side.exists()
    scheduler_side = tmp_path / "scheduler" / "state.db"
    assert heartbeat.record_heartbeat(scheduler_side, _beat(), create=True)
    assert _version(scheduler_side) == db.SCHEMA_VERSION


def test_a_busy_database_skips_the_beat_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.db"
    db.connect(path).close()
    monkeypatch.setattr("shakedown.db.BUSY_TIMEOUT_MS", 50)
    with closing(db.connect(path)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            assert not heartbeat.record_heartbeat(path, _beat(), create=True)
        holder.execute("ROLLBACK")
    assert "busy" in caplog.text


def test_reading_an_older_database_finds_no_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _older_database(path)
    with closing(db.open_unmigrated(path)) as conn:
        assert heartbeat.read_heartbeat(conn) is None
        assert heartbeat.read_notice(conn) is None


@pytest.mark.parametrize("age, valid, state", [
    (timedelta(seconds=30), True, "running"),
    (timedelta(minutes=3), True, "running"),
    (timedelta(minutes=3, seconds=1), True, "stale"),
    (timedelta(seconds=30), False, "no_schedule"),
    (timedelta(minutes=10), False, "stale"),
])
def test_scheduler_state(tmp_path: Path, age: timedelta, valid: bool, state: str) -> None:
    path = tmp_path / "state.db"
    heartbeat.record_heartbeat(path, _beat(NOW - age, valid=valid), create=True)
    with closing(db.connect_reader(path)) as conn:
        answer = heartbeat.scheduler_state(conn, NOW)
    assert answer["state"] == state
    assert answer["release"] == "9.9.9"
    assert datetime.fromisoformat(answer["last_seen"]) == NOW - age


def test_scheduler_state_never(tmp_path: Path) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        assert heartbeat.scheduler_state(conn, NOW) == {
            "state": "never", "last_seen": None, "release": None,
        }
