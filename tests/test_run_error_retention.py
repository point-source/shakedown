"""Run error retention (§spec:run-error-retention): when a run that holds the writer
lock ends, runs older than `keep_run_errors_days` drop their per-show error lines, and
their operation outcomes drop theirs. Each such run keeps its kind, collection, dates,
result, counts, and bytes, and records that its detail was trimmed."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shakedown import lock, retention
from shakedown.activity import ABANDONED_ERROR
from shakedown.config import Config
from shakedown.db import SCHEMA_VERSION, connect, open_unmigrated
from shakedown.history import last_good_sync_at, run_end, run_history
from shakedown.models import OperationStatus, OperationType, Run, RunKind
from shakedown.state import OperationOutcomeRepo, RunRepo
from shakedown.status import collection_summaries
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

S, C = "fake-src", "coll1"
PER_SHOW = ["gd-1: download failed: 503", "gd-2: checksum mismatch"]


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]) -> Config:
    config = make_config(*tmp_roots)
    config.keep_run_errors_days = 30
    return config


def _run(conn: sqlite3.Connection, age: timedelta, errors: list[str], *,
         items_failed: int = 0, kind: RunKind = RunKind.SYNC) -> Run:
    runs = RunRepo(conn)
    started = datetime.now() - age
    run = runs.start(S, C, started, kind=kind)
    run.finished_at = started + timedelta(minutes=10)
    run.items_discovered, run.items_new, run.items_failed = 12, 3, items_failed
    run.bytes_downloaded = 4096
    run.errors = errors
    runs.finish(run)
    return run


def _get(conn: sqlite3.Connection, run_id: int | None) -> Run:
    [run] = [r for r in RunRepo(conn).since(datetime.min) if r.id == run_id]
    return run


def _outcome(conn: sqlite3.Connection, age: timedelta, errors: list[str]) -> int:
    outcomes = OperationOutcomeRepo(conn)
    started = datetime.now() - age
    outcome = outcomes.start(OperationType.RESTAGE, S, C, started)
    outcomes.finish(outcome, OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
                    started + timedelta(minutes=1), errors=errors)
    assert outcome.id is not None
    return outcome.id


def _figures(run: Run) -> tuple[object, ...]:
    return (run.kind, run.source_name, run.collection_name, run.started_at,
            run.finished_at, run.items_discovered, run.items_new, run.items_updated,
            run.items_failed, run.bytes_downloaded, run.stale, run.failure_notice_error)


def test_the_next_sync_trims_a_run_older_than_the_setting_and_keeps_a_newer_one(
    config: Config,
) -> None:
    """The spec's test path: 30 days set, a failed run from 40 days ago and one from
    20 days ago; after the next sync the old one keeps its result and counts without
    its error lines, and the newer one keeps them."""
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        old = _run(conn, timedelta(days=40), PER_SHOW, items_failed=2)
        recent = _run(conn, timedelta(days=20), PER_SHOW, items_failed=2)
        old_before = _get(conn, old.id)
    FakePlugin.items["gd-9"] = FakeItem(
        identifier="gd-9", files=[FakeFile(name="gd-9.flac", content=b"audio")])

    assert run_sync(config) == 0

    with closing(connect(config.state_db)) as conn:
        now = datetime.now()
        trimmed = _get(conn, old.id)
        assert trimmed.errors == []
        assert trimmed.detail_trimmed_at is not None
        assert _figures(trimmed) == _figures(old_before)
        assert run_end(trimmed, None, now).result == "completed_with_item_issues"
        kept = _get(conn, recent.id)
        assert kept.errors == PER_SHOW and kept.detail_trimmed_at is None


def test_a_crashed_or_killed_run_keeps_its_result_and_last_good_sync_stays(
    config: Config,
) -> None:
    """A run's result is read from its crash or lost-contact line once its activity
    entry is gone (after 30 days), so the trim keeps that line: trimming never
    changes a result or the last good sync."""
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        good = _run(conn, timedelta(days=60), [])
        crashed = _run(conn, timedelta(days=50), [*PER_SHOW, "sync crashed: boom"])
        killed = _run(conn, timedelta(days=40), [*PER_SHOW, ABANDONED_ERROR])
        now = datetime.now()
        before = {r.id: run_end(r, None, now) for r in (crashed, killed)}
        good_before = last_good_sync_at(conn, S, C, now)
        assert good_before == good.finished_at

        assert retention.trim_run_errors(conn, now, 30) == 2

        assert _get(conn, crashed.id).errors == ["sync crashed: boom"]
        assert _get(conn, killed.id).errors == [ABANDONED_ERROR]
        for run_id, end in before.items():
            assert run_end(_get(conn, run_id), None, now) == end
        assert last_good_sync_at(conn, S, C, now) == good_before


def test_operation_outcomes_older_than_the_setting_drop_their_lines(
    config: Config,
) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        old = _outcome(conn, timedelta(days=40), PER_SHOW)
        recent = _outcome(conn, timedelta(days=20), PER_SHOW)
        retention.trim_run_errors(conn, datetime.now(), 30)
        outcomes = OperationOutcomeRepo(conn)
        old_outcome, recent_outcome = outcomes.get(old), outcomes.get(recent)
        assert old_outcome is not None and recent_outcome is not None
        assert old_outcome.errors == []
        assert old_outcome.status == OperationStatus.COMPLETED_WITH_ITEM_ISSUES
        assert recent_outcome.errors == PER_SHOW


def test_only_runs_that_lost_a_line_are_marked_and_a_second_trim_changes_nothing(
    config: Config,
) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        clean = _run(conn, timedelta(days=40), [])
        crash_only = _run(conn, timedelta(days=40), ["sync crashed: boom"])
        noisy = _run(conn, timedelta(days=40), PER_SHOW)
        now = datetime.now()
        assert retention.trim_run_errors(conn, now, 30) == 1
        assert _get(conn, clean.id).detail_trimmed_at is None
        assert _get(conn, crash_only.id).detail_trimmed_at is None
        assert _get(conn, noisy.id).detail_trimmed_at is not None
        assert retention.trim_run_errors(conn, now, 30) == 0


def test_status_shows_a_trimmed_last_run_with_its_figures(config: Config) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        _run(conn, timedelta(days=200), PER_SHOW, items_failed=2)
        retention.trim_run_errors(conn, datetime.now(), 30)
        [summary] = collection_summaries(config, conn)
    last = summary["last_run"]
    assert last["errors"] == [] and last["detail_trimmed"] is True
    assert (last["items_failed"], last["result"]) == (2, "completed_with_item_issues")


def test_the_run_history_says_whether_a_run_was_trimmed(config: Config) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        _run(conn, timedelta(days=1), PER_SHOW)
        conn.execute("UPDATE runs SET detail_trimmed_at=?", (datetime.now().isoformat(),))
        [row] = run_history(conn, datetime.now(), 30)
    assert row["detail_trimmed"] is True


def test_a_failed_trim_never_changes_the_run_s_exit_code(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention, "trim_run_errors", broken)
    assert lock.run_exclusive(config, lambda: 0) == 0


def _to_v13(db: Path) -> None:
    """Turn a current database into one from before v14."""
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN detail_trimmed_at")
        conn.execute("ALTER TABLE run_activity DROP COLUMN runs_trimmed")
        conn.execute("ALTER TABLE run_activity DROP COLUMN bytes_freed")
        conn.execute("UPDATE schema_version SET version = 13")
        conn.commit()


def test_the_v14_migration_adds_the_columns_and_keeps_every_run(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with closing(connect(db)) as conn:
        run = _run(conn, timedelta(days=3), PER_SHOW)
    _to_v13(db)
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        kept = _get(conn, run.id)
        assert kept.errors == PER_SHOW and kept.detail_trimmed_at is None
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(run_activity)")}
        assert {"runs_trimmed", "bytes_freed"} <= columns


def test_the_fresh_schema_matches_the_migrated_one(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    connect(db).close()
    _to_v13(db)
    migrated = connect(db)
    fresh = connect(tmp_path / "fresh.db")

    def columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    for table in ("runs", "run_activity"):
        assert columns(fresh, table) == columns(migrated, table)


def test_a_reader_of_a_v13_database_answers_without_migrating(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with closing(connect(db)) as conn:
        _run(conn, timedelta(days=1), PER_SHOW)
    _to_v13(db)
    with closing(open_unmigrated(db)) as conn:
        [row] = run_history(conn, datetime.now(), 30)
        assert row["errors"] == PER_SHOW and row["detail_trimmed"] is False
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 13
