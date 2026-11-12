"""The progress recorder (§spec:run-activity): hooks, timer writes, honest totals, and
recording that never costs a run anything."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shakedown.activity import (
    ActivityKind,
    ActivityRecorder,
    Phase,
    RunCounts,
    estimate_eta,
)
from shakedown.db import connect
from shakedown.state import RunRepo


class _Time:
    """Wall and monotonic time the test moves by hand."""

    def __init__(self) -> None:
        self.wall = datetime(2026, 11, 12, 9, 0, 0)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def clock(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    connect(path).close()
    return path


def _recorder(db: Path, t: _Time) -> ActivityRecorder:
    return ActivityRecorder(db, now=t.now, clock=t.clock, start_timer=False)


def _row(db: Path, activity_id: int | None) -> sqlite3.Row:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM run_activity WHERE id=?", (activity_id,)).fetchone()
    assert row is not None
    return row


def test_hooks_reach_the_entry_on_the_next_tick(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    row = _row(db, p.activity_id)
    assert (row["state"], row["phase"], row["kind"]) == ("running", "starting", "sync")
    assert row["started_by_serve"] == 0

    p.phase(Phase.DISCOVERING)
    p.listed(5)
    p.to_look_up(5)
    p.looked_up(2)
    p.to_fetch(100)
    p.to_fetch(300)
    p.working("show-a")
    p.working("show-b")
    p.idle("show-a")
    t.advance(3)
    rec.tick()

    row = _row(db, p.activity_id)
    assert row["phase"] == "discovering"
    assert (row["items_listed"], row["items_to_look_up"], row["items_looked_up"]) == (5, 5, 2)
    assert row["current_items"] == '["show-b"]'
    # Honest totals: unknown until enumeration ends.
    assert row["items_to_fetch"] is None
    assert row["bytes_to_fetch"] is None
    assert row["eta_seconds"] is None
    assert (row["items_done"], row["items_failed"], row["bytes_downloaded"]) == (0, 0, 0)

    p.enumeration_done()
    t.advance(3)
    rec.tick()
    row = _row(db, p.activity_id)
    assert (row["items_to_fetch"], row["bytes_to_fetch"]) == (2, 400)
    rec.close()


def test_bytes_to_fetch_stays_null_when_a_size_is_unknown(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    p.to_fetch(100)
    p.to_fetch(None)
    p.enumeration_done()
    rec.tick()
    row = _row(db, p.activity_id)
    assert row["items_to_fetch"] == 2
    assert row["bytes_to_fetch"] is None
    rec.close()


def test_heartbeat_moves_every_tick_progress_only_when_something_moves(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    started = t.now()
    t.advance(3)
    rec.tick()
    row = _row(db, p.activity_id)
    assert row["last_heartbeat_at"] == (started + timedelta(seconds=3)).isoformat()
    assert row["last_progress_at"] == started.isoformat()

    p.looked_up()
    moved = t.now()
    t.advance(3)
    rec.tick()
    row = _row(db, p.activity_id)
    assert row["last_progress_at"] == moved.isoformat()
    assert row["last_heartbeat_at"] == (moved + timedelta(seconds=3)).isoformat()
    rec.close()


def test_in_flight_bytes_measured_from_the_temp_folder_without_kept_files(
    db: Path, tmp_path: Path
) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    tmp_dir = tmp_path / ".tmp-show-a"
    tmp_dir.mkdir()
    (tmp_dir / "kept.flac").write_bytes(b"k" * 500)  # left by an earlier attempt
    with p.fetch_attempt("show-a", tmp_dir):
        (tmp_dir / "new.flac").write_bytes(b"n" * 1000)
        t.advance(5)
        rec.tick()
        assert _row(db, p.activity_id)["bytes_downloaded"] == 1000

        (tmp_dir / "new2.flac").write_bytes(b"n" * 2000)
        t.advance(5)
        rec.tick()
        row = _row(db, p.activity_id)
        assert row["bytes_downloaded"] == 3000
        assert row["speed_bytes_per_second"] == pytest.approx(300.0)
    rec.close()


def test_run_record_figures_move_with_the_entry(db: Path, tmp_path: Path) -> None:
    """A run record's counts move at the entry's cadence, so `status` and the
    dashboard agree mid-run (§spec:run-activity). Its bytes are the bytes the entry
    measured (§spec:network-activity)."""
    t = _Time()
    with closing(connect(db)) as conn:
        run = RunRepo(conn).start("src", "coll", t.now())
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC, run_id=run.id)
    p.run_counts = lambda: RunCounts(10, 3, 1, 2, 0)
    p.done(4)
    p.failed(2)
    tmp_dir = tmp_path / ".tmp-show"
    tmp_dir.mkdir()
    with p.fetch_attempt("show", tmp_dir):
        (tmp_dir / "a.flac").write_bytes(b"a" * 4096)
    rec.tick()

    with closing(connect(db)) as conn:
        latest = RunRepo(conn).latest("src", "coll")
    assert latest is not None and latest.finished_at is None
    assert (latest.items_discovered, latest.items_new, latest.items_updated,
            latest.items_failed, latest.bytes_downloaded) == (10, 3, 1, 2, 4096)
    row = _row(db, p.activity_id)
    assert (row["items_done"], row["items_failed"], row["bytes_downloaded"]) == (4, 2, 4096)
    rec.close()


def test_end_records_the_result_and_clears_live_fields(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    p.working("show-a")
    p.done()
    t.advance(7)
    rec.end(p, "completed_with_item_issues")
    row = _row(db, p.activity_id)
    assert (row["state"], row["result"], row["end_reason"]) == (
        "ended", "completed_with_item_issues", None)
    assert row["finished_at"] == t.now().isoformat()
    assert row["current_items"] == "[]"
    assert row["eta_seconds"] is None and row["speed_bytes_per_second"] is None
    assert row["items_done"] == 1
    rec.close()


def test_restage_reports_no_listing_counters_or_bytes(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.RESTAGE)
    p.phase(Phase.STAGING)
    p.work_total(7)
    p.done(3)
    rec.tick()
    row = _row(db, p.activity_id)
    assert (row["items_listed"], row["items_to_look_up"], row["items_looked_up"]) == (
        None, None, None)
    assert (row["items_to_fetch"], row["items_done"], row["items_failed"]) == (7, 3, 0)
    assert row["bytes_downloaded"] is None and row["speed_bytes_per_second"] is None
    rec.close()


def test_a_busy_database_skips_the_write_without_waiting(db: Path) -> None:
    """A write that cannot land is skipped; the next carries cumulative totals
    (§spec:run-activity "Recording never costs a run anything")."""
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    p.looked_up(3)
    began = time.monotonic()
    rec.tick()  # must neither raise nor wait for the blocker
    assert time.monotonic() - began < 1.0
    blocker.execute("ROLLBACK")
    blocker.close()
    assert _row(db, p.activity_id)["items_looked_up"] == 0

    p.looked_up(1)
    rec.tick()
    assert _row(db, p.activity_id)["items_looked_up"] == 4
    rec.close()


def test_a_pre_v10_database_records_nothing_and_is_not_migrated(db: Path) -> None:
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    t = _Time()
    rec = _recorder(db, t)
    assert not rec.enabled
    p = rec.open_entry("src", "coll", ActivityKind.VERIFY)
    p.done()
    rec.tick()
    rec.end(p, "completed")
    rec.close()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='run_activity'").fetchone() is None


def test_the_timer_writes_heartbeats_on_its_own(db: Path) -> None:
    rec = ActivityRecorder(db, tick_s=0.05)
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    p.looked_up(2)
    deadline = time.monotonic() + 5
    while _row(db, p.activity_id)["items_looked_up"] != 2:
        assert time.monotonic() < deadline, "the timer never wrote"
        time.sleep(0.02)
    rec.close()


def test_started_by_serve_is_recorded(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shakedown.activity as activity

    monkeypatch.setattr(activity, "_started_by_serve", True)
    rec = _recorder(db, _Time())
    p = rec.open_entry("src", "coll", ActivityKind.SYNC)
    assert _row(db, p.activity_id)["started_by_serve"] == 1
    rec.close()


def test_eta_by_bytes_over_the_ten_minute_speed() -> None:
    assert estimate_eta(bytes_to_fetch=1000, bytes_downloaded=400, eta_speed=20.0,
                        current_speed=50.0, items_remaining=3, pace=0.0) == 30


def test_eta_by_shows_when_the_source_lists_no_sizes() -> None:
    assert estimate_eta(bytes_to_fetch=None, bytes_downloaded=400, eta_speed=20.0,
                        current_speed=50.0, items_remaining=6, pace=0.5) == 12


def test_eta_is_null_while_speed_is_zero() -> None:
    assert estimate_eta(bytes_to_fetch=1000, bytes_downloaded=400, eta_speed=20.0,
                        current_speed=0.0, items_remaining=3, pace=0.5) is None
    assert estimate_eta(bytes_to_fetch=None, bytes_downloaded=0, eta_speed=0.0,
                        current_speed=0.0, items_remaining=3, pace=0.0) is None


def test_eta_is_zero_with_nothing_left() -> None:
    assert estimate_eta(bytes_to_fetch=1000, bytes_downloaded=1000, eta_speed=0.0,
                        current_speed=0.0, items_remaining=0, pace=0.0) == 0
