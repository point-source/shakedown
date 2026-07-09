from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shakedown.db import connect
from shakedown.models import (
    Item,
    ItemStatus,
    Manifest,
    ManifestFile,
    OperationStatus,
    OperationType,
    Run,
)
from shakedown.state import DriftRepo, ItemRepo, OperationOutcomeRepo, RunRepo


def _make_item(identifier: str = "x") -> Item:
    m = Manifest(files=(ManifestFile("a.flac", 100, "d41d"),))
    return Item(
        source_name="s", collection_name="c", identifier=identifier,
        status=ItemStatus.COMPLETE, archive_path=Path("/data/archive/s/c/x"),
        discovered_at=datetime(2026, 4, 26), downloaded_at=datetime(2026, 4, 26),
        recorded_manifest=m, change_signal="2024-05-01T00:00:00Z|12345",
    )


def test_item_round_trip(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    repo = ItemRepo(conn)
    item = _make_item()
    repo.upsert(item)
    fetched = repo.get("s", "c", "x")
    assert fetched is not None
    assert fetched.identifier == "x"
    assert fetched.recorded_manifest == item.recorded_manifest
    assert fetched.change_signal == "2024-05-01T00:00:00Z|12345"


def test_item_upsert_preserves_change_signal(tmp_path: Path) -> None:
    """Re-upserting with change_signal=None must not clobber the prior signal."""
    conn = connect(tmp_path / "s.db")
    repo = ItemRepo(conn)
    item = _make_item()
    repo.upsert(item)
    item2 = _make_item()
    item2.change_signal = None
    repo.upsert(item2)
    fetched = repo.get("s", "c", "x")
    assert fetched is not None
    assert fetched.change_signal == item.change_signal


def test_item_upsert_preserves_unchanged_timestamps(tmp_path: Path) -> None:
    """Re-upserting with downloaded_at=None must not clobber the prior timestamp."""
    conn = connect(tmp_path / "s.db")
    repo = ItemRepo(conn)
    item = _make_item()
    repo.upsert(item)
    item2 = _make_item()
    item2.downloaded_at = None
    item2.discovered_at = None
    repo.upsert(item2)
    fetched = repo.get("s", "c", "x")
    assert fetched is not None
    assert fetched.downloaded_at == item.downloaded_at
    assert fetched.discovered_at == item.discovered_at


def test_count_by_status(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    repo = ItemRepo(conn)
    for n in range(3):
        i = _make_item(f"x{n}")
        repo.upsert(i)
    bad = _make_item("bad")
    bad.status = ItemStatus.FAILED
    repo.upsert(bad)
    counts = repo.count_by_status("s", "c")
    assert counts[ItemStatus.COMPLETE] == 3
    assert counts[ItemStatus.FAILED] == 1


def test_run_lifecycle(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    runs = RunRepo(conn)
    run = runs.start("s", "c", datetime(2026, 4, 26, 12, 0))
    assert isinstance(run, Run) and run.id is not None
    run.items_new = 5
    run.bytes_downloaded = 99
    run.finished_at = datetime(2026, 4, 26, 12, 30)
    runs.finish(run)
    latest = runs.latest("s", "c")
    assert latest is not None
    assert latest.items_new == 5
    assert latest.bytes_downloaded == 99
    assert latest.finished_at == datetime(2026, 4, 26, 12, 30)


def test_run_stale_round_trips(tmp_path: Path) -> None:
    """A run flagged stale (source enumeration failed) survives a save/load cycle."""
    conn = connect(tmp_path / "s.db")
    runs = RunRepo(conn)
    run = runs.start("s", "c", datetime(2026, 4, 26, 12, 0))
    run.stale = True
    run.errors = ["source enumeration failed: unreachable"]
    run.finished_at = datetime(2026, 4, 26, 12, 1)
    runs.finish(run)
    latest = runs.latest("s", "c")
    assert latest is not None
    assert latest.stale is True
    assert latest.errors == ["source enumeration failed: unreachable"]


def test_operation_outcome_round_trip_and_latest_actionable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    repo = OperationOutcomeRepo(conn)
    started = datetime(2026, 4, 26, 12, 0)
    finished = datetime(2026, 4, 26, 12, 3)

    sync = repo.start(OperationType.SYNC, "s", "c", started, phase="discover")
    repo.finish(
        sync,
        OperationStatus.STALE_ENUMERATION,
        finished,
        phase="discover",
        affected_item="x",
        completed_work={"items_discovered": 1, "items_fetched": 1},
        preservation_context="local recordings retained; no pruning performed",
        deletion_context="no deletion requested",
        safe_next_action="restore source access and rerun sync",
        errors=["source enumeration failed: unreachable"],
    )

    restage = repo.start(OperationType.RESTAGE, "s", "c", finished, phase="stage")
    repo.finish(restage, OperationStatus.COMPLETED, finished, phase="stage")

    latest = repo.latest_actionable("s", "c")
    assert latest is not None
    assert latest.operation == OperationType.SYNC
    assert latest.status == OperationStatus.STALE_ENUMERATION
    assert latest.phase == "discover"
    assert latest.affected_item == "x"
    assert latest.completed_work == {"items_discovered": 1, "items_fetched": 1}
    assert latest.preservation_context == "local recordings retained; no pruning performed"
    assert latest.deletion_context == "no deletion requested"
    assert latest.safe_next_action == "restore source access and rerun sync"
    assert latest.errors == ["source enumeration failed: unreachable"]


def test_operation_outcome_resolution_is_scoped(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    repo = OperationOutcomeRepo(conn)
    started = datetime(2026, 4, 26, 12, 0)
    finished = datetime(2026, 4, 26, 12, 1)

    sync = repo.start(OperationType.SYNC, "s", "c", started, phase="discover")
    repo.finish(
        sync,
        OperationStatus.STALE_ENUMERATION,
        finished,
        phase="discover",
        safe_next_action="rerun sync",
    )
    restage = repo.start(OperationType.RESTAGE, "s", "c", started, phase="stage")
    repo.finish(
        restage,
        OperationStatus.FAILED_BEFORE_COMPLETION,
        finished,
        phase="stage",
        safe_next_action="rerun restage",
    )

    repo.resolve_actionable(
        OperationType.SYNC, "s", "c", datetime(2026, 4, 26, 12, 2)
    )

    latest = repo.latest_actionable("s", "c")
    assert latest is not None
    assert latest.operation == OperationType.RESTAGE
    assert latest.safe_next_action == "rerun restage"


def test_v1_db_migrates_to_current_schema(tmp_path: Path) -> None:
    """An existing v1 database (no runs.stale column) upgrades in place; the added
    column defaults to non-stale so historical runs read back cleanly."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version (version) VALUES (1);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            items_discovered INTEGER NOT NULL DEFAULT 0,
            items_new INTEGER NOT NULL DEFAULT 0,
            items_updated INTEGER NOT NULL DEFAULT 0,
            items_failed INTEGER NOT NULL DEFAULT 0,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO runs (source_name, collection_name, started_at)
            VALUES ('s', 'c', '2026-01-01T00:00:00');
        """
    )
    legacy.commit()
    legacy.close()

    conn = connect(db_path)  # triggers migration
    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == 5
    latest = RunRepo(conn).latest("s", "c")
    assert latest is not None and latest.stale is False
    # The collision columns added in later migrations default cleanly for old runs.
    assert latest.collisions_dropped == 0
    assert latest.collision_paths == []
    tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()]
    assert "operation_outcomes" in tables


def test_v2_db_migrates_change_signal_column(tmp_path: Path) -> None:
    """An existing v2 database (items table without change_signal) upgrades in place:
    the column is added and subsequent upserts can store/read a change signal."""
    import sqlite3

    db_path = tmp_path / "legacy_v2.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version (version) VALUES (2);
        CREATE TABLE items (
            source_name        TEXT NOT NULL,
            collection_name    TEXT NOT NULL,
            identifier         TEXT NOT NULL,
            status             TEXT NOT NULL,
            archive_path       TEXT,
            discovered_at      TEXT,
            downloaded_at      TEXT,
            last_verified_at   TEXT,
            restriction_reason TEXT,
            source_metadata    TEXT NOT NULL DEFAULT '{}',
            recorded_manifest  TEXT,
            PRIMARY KEY (source_name, collection_name, identifier)
        );
        INSERT INTO items (source_name, collection_name, identifier, status)
            VALUES ('s', 'c', 'x', 'complete');
        """
    )
    legacy.commit()
    legacy.close()

    conn = connect(db_path)  # triggers migration
    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == 5
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
    assert "change_signal" in cols
    run_cols = [row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    assert "collisions_dropped" in run_cols
    assert "collision_paths" in run_cols
    tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()]
    assert "operation_outcomes" in tables

    repo = ItemRepo(conn)
    item = repo.get("s", "c", "x")
    assert item is not None
    item.change_signal = "2024-06-01T00:00:00Z|999"
    repo.upsert(item)
    fetched = repo.get("s", "c", "x")
    assert fetched is not None
    assert fetched.change_signal == "2024-06-01T00:00:00Z|999"


def test_v4_db_migrates_operation_outcomes_table(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy_v4.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version (version) VALUES (4);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            items_discovered INTEGER NOT NULL DEFAULT 0,
            items_new INTEGER NOT NULL DEFAULT 0,
            items_updated INTEGER NOT NULL DEFAULT 0,
            items_failed INTEGER NOT NULL DEFAULT 0,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]',
            stale INTEGER NOT NULL DEFAULT 0,
            collisions_dropped INTEGER NOT NULL DEFAULT 0,
            collision_paths TEXT NOT NULL DEFAULT '[]'
        );
        """
    )
    legacy.commit()
    legacy.close()

    conn = connect(db_path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == 5
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "operation_outcomes" in tables

    repo = OperationOutcomeRepo(conn)
    outcome = repo.start(
        OperationType.VALIDATE,
        "s",
        "c",
        datetime(2026, 4, 26, 12, 0),
        phase="paths",
    )
    assert outcome.id is not None


def test_drift_repo(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    drift = DriftRepo(conn)
    now = datetime.now()
    drift.record("s", "c", "x", "a.flac", "newhash", "oldhash", now)
    drift.record("s", "c", "x", "b.flac", "newhash2", "oldhash2", now)
    assert drift.count("s", "c") == 2
    drift.clear("s", "c", "x", "a.flac")
    assert drift.count("s", "c") == 1
