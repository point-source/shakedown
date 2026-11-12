from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from shakedown.db import SCHEMA_VERSION, connect
from shakedown.models import (
    Item,
    ItemStatus,
    Manifest,
    ManifestFile,
    OperationStatus,
    OperationType,
    Run,
    RunKind,
)
from shakedown.state import (
    DriftRepo,
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    PlacementRepo,
    RescheduledRepo,
    RunRepo,
    TagRecord,
    TagRepo,
)


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


def test_run_kind_round_trips_and_latest_filters_by_kind(tmp_path: Path) -> None:
    """A single-show run is recorded with its kind (§spec:single-show-runs); a run
    opened without one is a sync."""
    conn = connect(tmp_path / "s.db")
    runs = RunRepo(conn)
    sync = runs.start("s", "c", datetime(2026, 4, 26, 12, 0))
    refetch = runs.start("s", "c", datetime(2026, 4, 26, 13, 0), kind=RunKind.REFETCH)
    assert sync.kind == RunKind.SYNC and refetch.kind == RunKind.REFETCH

    latest = runs.latest("s", "c")
    assert latest is not None and latest.id == refetch.id
    assert latest.kind == RunKind.REFETCH
    latest_sync = runs.latest("s", "c", kind=RunKind.SYNC)
    assert latest_sync is not None and latest_sync.id == sync.id
    assert latest_sync.kind == RunKind.SYNC


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


def test_operation_outcome_latest_is_the_newest_of_one_operation(tmp_path: Path) -> None:
    """The dashboard's last validation per collection (§spec:dashboard-health): the
    newest outcome of that operation, resolved or not, ignoring other operations."""
    conn = connect(tmp_path / "s.db")
    repo = OperationOutcomeRepo(conn)
    assert repo.latest(OperationType.VALIDATE, "s", "c") is None

    first = datetime(2026, 11, 1, 9, 0)
    failed = repo.start(OperationType.VALIDATE, "s", "c", first)
    repo.finish(failed, OperationStatus.FAILED_BEFORE_COMPLETION, first)
    second = datetime(2026, 11, 2, 9, 0)
    passed = repo.start(OperationType.VALIDATE, "s", "c", second)
    repo.finish(passed, OperationStatus.COMPLETED, second)
    repo.resolve_actionable(OperationType.VALIDATE, "s", "c", second)
    later_sync = repo.start(OperationType.SYNC, "s", "c", datetime(2026, 11, 3))
    repo.finish(later_sync, OperationStatus.COMPLETED, datetime(2026, 11, 3))
    other = repo.start(OperationType.VALIDATE, "s", "other", datetime(2026, 11, 4))
    repo.finish(other, OperationStatus.FAILED_BEFORE_COMPLETION, datetime(2026, 11, 4))

    latest = repo.latest(OperationType.VALIDATE, "s", "c")
    assert latest is not None
    assert latest.id == passed.id
    assert latest.status == OperationStatus.COMPLETED
    assert latest.finished_at == second


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
    assert version == SCHEMA_VERSION
    latest = RunRepo(conn).latest("s", "c")
    assert latest is not None and latest.stale is False
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
    assert version == SCHEMA_VERSION
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
    assert "change_signal" in cols
    run_cols = [row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    # Added in v4 for skip-the-loser, dropped in v6 with clash separation.
    assert "collisions_dropped" not in run_cols
    assert "collision_paths" not in run_cols
    assert "failure_notice_error" in run_cols
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
    assert version == SCHEMA_VERSION
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


_V5_RUNS = """
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


_V5_ITEMS = """
        CREATE TABLE items (
            source_name TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            identifier TEXT NOT NULL,
            status TEXT NOT NULL,
            archive_path TEXT,
            discovered_at TEXT,
            downloaded_at TEXT,
            last_verified_at TEXT,
            restriction_reason TEXT,
            source_metadata TEXT NOT NULL DEFAULT '{}',
            recorded_manifest TEXT,
            change_signal TEXT,
            PRIMARY KEY (source_name, collection_name, identifier)
        );
"""


def _tables(conn) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _legacy_v5(db_path: Path, extra_sql: str = "") -> None:
    import sqlite3

    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);"
        "INSERT INTO schema_version (version) VALUES (5);" + _V5_RUNS + extra_sql
    )
    legacy.commit()
    legacy.close()


def test_v5_db_migrates_to_placement_record(tmp_path: Path) -> None:
    """§spec:state: the increment moves the schema forward by one migration. A v5
    database gains the library placement record, keeps its runs, and loses the
    skip-the-loser collision columns (§spec:layout-collision-safety, superseded)."""
    db_path = tmp_path / "legacy_v5.db"
    _legacy_v5(
        db_path,
        "INSERT INTO runs (source_name, collection_name, started_at, collisions_dropped)"
        " VALUES ('s', 'c', '2026-01-01T00:00:00', 2);",
    )

    conn = connect(db_path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    assert {
        "library_placements", "library_folders", "library_unplaced", "library_adoption",
        "handoff_pending", "tag_files", "tag_shows",
    } <= _tables(conn)
    run_cols = _columns(conn, "runs")
    assert "collisions_dropped" not in run_cols
    assert "collision_paths" not in run_cols
    assert "failure_notice_error" in run_cols
    assert "kind" in run_cols
    latest = RunRepo(conn).latest("s", "c")
    assert latest is not None and latest.kind == RunKind.SYNC


def test_v5_db_gains_the_not_playable_reason_and_reselects_every_show(tmp_path: Path) -> None:
    """The quality rule (§spec:quality-selection) adds a show's not-playable reason
    to the increment's one migration, and clears every stored change signal once so
    the first sync after the upgrade looks each show up and applies the new rule,
    even on an incremental collection (§spec:incremental-discovery)."""
    db_path = tmp_path / "legacy_v5.db"
    _legacy_v5(
        db_path,
        _V5_ITEMS
        + "INSERT INTO items (source_name, collection_name, identifier, status,"
        " recorded_manifest, change_signal) VALUES ('s', 'c', 'x', 'complete',"
        " '[{\"name\": \"a.shn\", \"size\": 1, \"md5\": null}]', 'sig-1');",
    )

    conn = connect(db_path)
    assert "not_playable_reason" in _columns(conn, "items")
    item = ItemRepo(conn).get("s", "c", "x")
    assert item is not None
    assert item.change_signal is None
    assert item.not_playable_reason is None
    assert item.recorded_manifest is not None
    assert [f.library for f in item.recorded_manifest.files] == [True]


def test_v5_db_keeps_every_show_and_dates_no_earlier_takedown(tmp_path: Path) -> None:
    """The increment's one migration adds the date a show was first found missing
    (§spec:item-lifecycle). A show already missing before the upgrade gets none:
    `last_verified_at` is rewritten by every reconcile, so it is not that date."""
    db_path = tmp_path / "legacy_v5.db"
    _legacy_v5(
        db_path,
        _V5_ITEMS
        + "INSERT INTO items (source_name, collection_name, identifier, status,"
        " last_verified_at) VALUES"
        " ('s', 'c', 'gone', 'disappeared', '2026-03-04T05:06:07'),"
        " ('s', 'c', 'here', 'complete', '2026-03-04T05:06:07');",
    )

    conn = connect(db_path)
    assert "disappeared_at" in _columns(conn, "items")
    repo = ItemRepo(conn)
    assert [i.identifier for i in repo.list_for_collection("s", "c")] == ["gone", "here"]
    gone = repo.get("s", "c", "gone")
    assert gone is not None and gone.status == ItemStatus.DISAPPEARED
    assert gone.disappeared_at is None


def test_not_playable_reason_round_trips(tmp_path: Path) -> None:
    repo = ItemRepo(connect(tmp_path / "s.db"))
    item = _make_item()
    item.not_playable_reason = "only in SHN; archive.org offers no playable copy"
    repo.upsert(item)
    got = repo.get("s", "c", "x")
    assert got is not None
    assert got.not_playable_reason == item.not_playable_reason
    planned = next(repo.list_for_planning("s", "c"))
    assert planned.not_playable_reason == item.not_playable_reason
    item.not_playable_reason = None
    repo.upsert(item)
    got = repo.get("s", "c", "x")
    assert got is not None and got.not_playable_reason is None


def test_fresh_db_matches_migrated_schema(tmp_path: Path) -> None:
    fresh = connect(tmp_path / "fresh.db")
    db_path = tmp_path / "legacy_v5.db"
    _legacy_v5(db_path, _V5_ITEMS)
    migrated = connect(db_path)
    for table in ("items", "runs", "library_placements", "library_folders",
                  "library_unplaced", "library_adoption", "handoff_pending",
                  "tag_files", "tag_shows"):
        assert _columns(fresh, table) == _columns(migrated, table), table


def test_placement_repo_round_trip(tmp_path: Path) -> None:
    repo = PlacementRepo(connect(tmp_path / "s.db"))
    repo.add_placement("s", "c", "show-a", "A/a.flac", 1)
    repo.add_placement("s", "c", "show-a", "A/a.flac", 2)  # same owner: updates the inode
    repo.add_placement("s", "c", "show-a", "A/metadata.json", 3)
    repo.add_placement("s", "c", "show-b", "B/b.flac", 4)
    repo.add_placement("s", "other", "show-a", "A/a.flac", 5)  # other collection

    assert set(repo.placed_inodes("s", "c", "show-a")) == {"A/a.flac", "A/metadata.json"}
    assert repo.owner_of("s", "c", "B/b.flac") == "show-b"
    assert repo.owner_of("s", "c", "nope") is None
    assert repo.placed_inodes("s", "c", "show-a")["A/a.flac"] == 2  # last recorded inode
    assert repo.placed_identifiers("s", "c") == {"show-a", "show-b"}
    repo.remove_placement("s", "c", "A/a.flac")
    assert set(repo.placed_inodes("s", "c", "show-a")) == {"A/metadata.json"}


def test_placement_repo_one_owner_per_path(tmp_path: Path) -> None:
    """The record is the sole authority for who owns a library path
    (§spec:library-ownership): a second show cannot claim a recorded path."""
    import sqlite3

    import pytest

    repo = PlacementRepo(connect(tmp_path / "s.db"))
    repo.add_placement("s", "c", "show-a", "X/track.flac", 6)
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_placement("s", "c", "show-b", "X/track.flac", 7)


def test_placement_repo_folders_unplaced_adoption(tmp_path: Path) -> None:
    repo = PlacementRepo(connect(tmp_path / "s.db"))
    repo.set_folder("s", "c", "show-b", "1977 [show-b]", separated=True)
    repo.set_folder("s", "c", "show-a", "1977 [show-a]", separated=True)
    repo.set_folder("s", "c", "show-c", "1978", separated=False)
    repo.set_folder("s", "c", "show-c", "1979", separated=False)  # moves
    assert repo.list_separated("s", "c") == [
        ("show-a", "1977 [show-a]"), ("show-b", "1977 [show-b]"),
    ]
    assert repo.folders("s", "c") == {
        "show-a": "1977 [show-a]", "show-b": "1977 [show-b]", "show-c": "1979",
    }
    repo.delete_folder("s", "c", "show-a")
    assert [i for i, _ in repo.list_separated("s", "c")] == ["show-b"]

    repo.add_unplaced("s", "c", "X/mine.flac")
    repo.add_unplaced("s", "c", "X/mine.flac")
    repo.add_unplaced("s", "c", "Y/stray.mp3")
    assert repo.list_unplaced("s", "c") == ["X/mine.flac", "Y/stray.mp3"]
    repo.remove_unplaced("s", "c", "X/mine.flac")
    assert repo.list_unplaced("s", "c") == ["Y/stray.mp3"]

    assert repo.is_adopted("s", "c") is False
    repo.mark_adopted("s", "c", datetime(2026, 11, 10))
    assert repo.is_adopted("s", "c") is True
    assert repo.is_adopted("s", "other") is False


def test_pending_handoff_repo_keeps_first_since_and_latest_reason(tmp_path: Path) -> None:
    """§spec:durable-handoff: per collection, the shows covered by notices not yet
    delivered, since when, and the last reason a notice failed."""
    repo = PendingHandoffRepo(connect(tmp_path / "s.db"))
    first = datetime(2026, 11, 1, 3, 0)
    later = datetime(2026, 11, 8, 3, 0)
    repo.mark_undelivered("s", "c", ["show-a", "show-b"], "webhook answered 503", first)
    repo.mark_undelivered("s", "c", ["show-b", "show-c"], "no answer within 5 minutes", later)
    repo.mark_undelivered("s", "other", ["show-z"], "elsewhere", later)

    pending = repo.list("s", "c")
    assert [p.identifier for p in pending] == ["show-a", "show-b", "show-c"]
    # A show keeps the time it first went undelivered; every show carries the newest reason.
    assert {p.identifier: p.since for p in pending} == {
        "show-a": first, "show-b": first, "show-c": later,
    }
    assert {p.last_reason for p in pending} == {"no answer within 5 minutes"}


def test_pending_handoff_repo_clear_and_drop(tmp_path: Path) -> None:
    repo = PendingHandoffRepo(connect(tmp_path / "s.db"))
    at = datetime(2026, 11, 1)
    repo.mark_undelivered("s", "c", ["show-a", "show-b"], "boom", at)
    repo.mark_undelivered("s", "other", ["show-a"], "boom", at)

    repo.clear("s", "c", ["show-a"])
    assert [p.identifier for p in repo.list("s", "c")] == ["show-b"]
    assert [p.identifier for p in repo.list("s", "other")] == ["show-a"]

    repo.drop("s", "c")
    assert repo.list("s", "c") == []
    assert [p.identifier for p in repo.list("s", "other")] == ["show-a"]


def test_run_failure_notice_error_round_trips(tmp_path: Path) -> None:
    """§spec:durable-handoff: an undelivered failure alert is shown in that run's
    status, so the run keeps its reason."""
    repo = RunRepo(connect(tmp_path / "s.db"))
    run = repo.start("s", "c", datetime(2026, 11, 10, 3, 0))
    assert repo.latest("s", "c").failure_notice_error is None  # type: ignore[union-attr]
    run.failure_notice_error = "webhook answered 502"
    run.finished_at = datetime(2026, 11, 10, 3, 5)
    repo.finish(run)
    assert repo.latest("s", "c").failure_notice_error == "webhook answered 502"  # type: ignore[union-attr]


def test_tag_repo_round_trip(tmp_path: Path) -> None:
    """Per file shakedown tagged: the post-tag checksum and the values it wrote; per
    show: the fingerprint of its computed tags (§spec:tag-filling, §spec:state)."""
    repo = TagRepo(connect(tmp_path / "s.db"))
    repo.put_file("s", "c", "show-a", "a.flac", TagRecord("md5-a", {"album": "X"}))
    repo.put_file("s", "c", "show-a", "b.flac", TagRecord("md5-b", {"artist": "Y"}))
    repo.put_file("s", "c", "show-a", "a.flac", TagRecord("md5-a2", {"album": "Z"}))
    repo.put_file("s", "other", "show-a", "a.flac", TagRecord("md5-o", {}))
    assert repo.file_records("s", "c", "show-a") == {
        "a.flac": TagRecord("md5-a2", {"album": "Z"}),
        "b.flac": TagRecord("md5-b", {"artist": "Y"}),
    }
    repo.drop_files("s", "c", "show-a", ["b.flac"])
    assert set(repo.file_records("s", "c", "show-a")) == {"a.flac"}

    repo.set_fingerprint("s", "c", "show-a", "fp-1")
    repo.set_fingerprint("s", "c", "show-a", "fp-2")
    repo.set_fingerprint("s", "c", "show-b", "fp-b")
    assert repo.fingerprints("s", "c") == {"show-a": "fp-2", "show-b": "fp-b"}
    repo.clear_fingerprint("s", "c", "show-b")
    assert repo.fingerprints("s", "c") == {"show-a": "fp-2"}

    repo.delete_show("s", "c", "show-a")
    assert repo.file_records("s", "c", "show-a") == {}
    assert repo.fingerprints("s", "c") == {}
    assert set(repo.file_records("s", "other", "show-a")) == {"a.flac"}


def test_deleting_an_item_drops_its_tag_records(tmp_path: Path) -> None:
    """A show's tag records go with its row (reconcile drops rows for shows gone from
    disk); nothing else loses them (§spec:state)."""
    conn = connect(tmp_path / "s.db")
    items, tags = ItemRepo(conn), TagRepo(conn)
    items.upsert(_make_item())
    tags.put_file("s", "c", "x", "a.flac", TagRecord("md5", {"album": "A"}))
    tags.set_fingerprint("s", "c", "x", "fp")
    items.delete("s", "c", "x")
    assert tags.file_records("s", "c", "x") == {}
    assert tags.fingerprints("s", "c") == {}


def test_manifest_keeps_track_title_and_number_outside_its_identity(tmp_path: Path) -> None:
    """Per-file title and track are recorded as the source listed them, but a change
    to them is not a change to the show's files (§spec:sync-identity)."""
    titled = Manifest(files=(ManifestFile("a.flac", 1, "m", title="Intro", track="1"),))
    plain = Manifest(files=(ManifestFile("a.flac", 1, "m"),))
    assert titled == plain
    assert titled.to_json() == [
        {"name": "a.flac", "size": 1, "md5": "m", "title": "Intro", "track": "1"}
    ]
    assert plain.to_json() == [{"name": "a.flac", "size": 1, "md5": "m"}]
    assert Manifest.from_json(titled.to_json()).files == titled.files

    repo = ItemRepo(connect(tmp_path / "s.db"))
    item = _make_item()
    item.recorded_manifest = titled
    repo.upsert(item)
    got = repo.get("s", "c", "x")
    assert got is not None and got.recorded_manifest is not None
    assert got.recorded_manifest.files[0].title == "Intro"


def test_v6_db_reselects_every_show_once(tmp_path: Path) -> None:
    """v2.0.2 changes the quality rule (§spec:quality-selection: a lossless copy split
    across format names; only the uploader's non-audio files staged), so its one
    migration clears every stored change signal once: the first sync after the upgrade
    looks each show up and applies the new rule, even on an incremental collection
    (§spec:incremental-discovery). Nothing else changes."""
    db_path = tmp_path / "v6.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO items (source_name, collection_name, identifier, status,"
        " recorded_manifest, change_signal) VALUES ('s', 'c', 'x', 'complete',"
        " '[{\"name\": \"a.flac\", \"size\": 1, \"md5\": null, \"library\": false}]',"
        " 'sig-1')"
    )
    conn.execute("UPDATE schema_version SET version = 6")
    conn.commit()
    conn.close()

    conn = connect(db_path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    item = ItemRepo(conn).get("s", "c", "x")
    assert item is not None
    assert item.change_signal is None
    assert item.recorded_manifest is not None
    assert [(f.name, f.library) for f in item.recorded_manifest.files] == [("a.flac", False)]


def test_v7_db_reselects_every_show_once(tmp_path: Path) -> None:
    """v2.0.3 keeps archive files and system or editor junk out of the library
    (§spec:quality-selection), so its one migration clears every stored change signal
    once, as v7 did: the first sync after the upgrade looks each show up and takes those
    files out of the library, even on an incremental collection
    (§spec:incremental-discovery). Nothing else changes."""
    db_path = tmp_path / "v7.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO items (source_name, collection_name, identifier, status,"
        " recorded_manifest, change_signal) VALUES ('s', 'c', 'x', 'complete',"
        " '[{\"name\": \"raw.zip\", \"size\": 1, \"md5\": null, \"library\": true}]',"
        " 'sig-1')"
    )
    conn.execute("UPDATE schema_version SET version = 7")
    conn.commit()
    conn.close()

    conn = connect(db_path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    item = ItemRepo(conn).get("s", "c", "x")
    assert item is not None
    assert item.change_signal is None
    assert item.recorded_manifest is not None
    assert [(f.name, f.library) for f in item.recorded_manifest.files] == [("raw.zip", True)]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_fresh_db_has_rescheduled_syncs(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    assert "rescheduled_syncs" in _tables(conn)
    assert RescheduledRepo(conn).get("s", "c") is None


def test_v8_db_gains_empty_rescheduled_syncs(tmp_path: Path) -> None:
    """v9 adds the rescheduled-sync record as an additive migration
    (§spec:state, §spec:rescheduled-syncs): nothing else changes."""
    db_path = tmp_path / "v8.db"
    conn = connect(db_path)
    ItemRepo(conn).upsert(_make_item())
    conn.execute("DROP TABLE rescheduled_syncs")
    conn.execute("UPDATE schema_version SET version = 8")
    conn.close()

    conn = connect(db_path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 9
    assert "rescheduled_syncs" in _tables(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM rescheduled_syncs").fetchone()["n"] == 0
    item = ItemRepo(conn).get("s", "c", "x")
    assert item is not None
    assert item.change_signal == "2024-05-01T00:00:00Z|12345"


def test_rescheduled_keeps_the_oldest_skip_once_per_collection(tmp_path: Path) -> None:
    """One entry per collection; a second skip neither adds an entry nor moves the
    time (§spec:rescheduled-syncs)."""
    repo = RescheduledRepo(connect(tmp_path / "s.db"))
    first = datetime(2026, 11, 10, 3, 0, 0)
    assert repo.record("s", "b", first) == first
    assert repo.record("s", "b", datetime(2026, 11, 11, 3, 0, 0)) == first
    assert repo.get("s", "b") == first
    rows = repo.conn.execute("SELECT * FROM rescheduled_syncs").fetchall()
    assert len(rows) == 1


def test_rescheduled_clear_removes_only_its_collection(tmp_path: Path) -> None:
    repo = RescheduledRepo(connect(tmp_path / "s.db"))
    repo.record("s", "b", datetime(2026, 11, 10, 3, 0, 0))
    repo.record("s", "c", datetime(2026, 11, 10, 4, 0, 0))
    repo.record("t", "b", datetime(2026, 11, 10, 5, 0, 0))
    repo.clear("s", "b")
    assert repo.get("s", "b") is None
    assert repo.get("s", "c") == datetime(2026, 11, 10, 4, 0, 0)
    assert repo.get("t", "b") == datetime(2026, 11, 10, 5, 0, 0)


def test_rescheduled_list_is_oldest_skip_first(tmp_path: Path) -> None:
    """The catch-up order (§spec:rescheduled-syncs step 4)."""
    repo = RescheduledRepo(connect(tmp_path / "s.db"))
    assert repo.list() == []
    repo.record("s", "c", datetime(2026, 11, 10, 4, 0, 0))
    repo.record("t", "b", datetime(2026, 11, 10, 2, 0, 0))
    repo.record("s", "b", datetime(2026, 11, 10, 3, 0, 0))
    assert repo.list() == [
        ("t", "b", datetime(2026, 11, 10, 2, 0, 0)),
        ("s", "b", datetime(2026, 11, 10, 3, 0, 0)),
        ("s", "c", datetime(2026, 11, 10, 4, 0, 0)),
    ]
