"""Growth snapshots (§spec:library-stats "Growth", §spec:state v10): every run that
holds the writer lock records one per collection at its end, the v10 migration
records the first, and history is kept 30 days."""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from shakedown.db import connect
from shakedown.growth import GrowthRepo
from shakedown.lock import run_exclusive
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.state import ItemRepo, RunRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _item(collection: str, identifier: str, status: ItemStatus, size: int,
          *, archived: bool = True) -> Item:
    return Item(
        source_name="s", collection_name=collection, identifier=identifier,
        status=status,
        archive_path=Path(f"/a/s/{collection}/{identifier}") if archived else None,
        discovered_at=datetime(2026, 10, 1), downloaded_at=datetime(2026, 10, 1),
        recorded_manifest=Manifest(files=(
            ManifestFile("t.flac", size, "m"),
            ManifestFile("t.shn", size * 2, "n", library=False),
        )),
    )


def _rows(conn) -> list[tuple]:
    return [
        (r["source_name"], r["collection_name"], r["complete"], r["archive_bytes"])
        for r in conn.execute(
            "SELECT * FROM growth_snapshots ORDER BY source_name, collection_name, id")
    ]


def test_v9_migration_records_one_snapshot_per_collection(tmp_path: Path) -> None:
    db = tmp_path / "v9.db"
    conn = connect(db)
    items = ItemRepo(conn)
    items.upsert(_item("a", "a1", ItemStatus.COMPLETE, 100))
    items.upsert(_item("a", "a2", ItemStatus.FAILED, 10))
    items.upsert(_item("a", "a3", ItemStatus.PENDING, 7, archived=False))
    items.upsert(_item("b", "b1", ItemStatus.COMPLETE, 50))
    conn.execute("DROP TABLE growth_snapshots")
    conn.execute("UPDATE schema_version SET version = 9")
    conn.close()

    with closing(connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 10
        items = ItemRepo(conn)
        expected = [
            ("s", c, items.count_by_status("s", c).get(ItemStatus.COMPLETE, 0),
             items.archived_bytes("s", c))
            for c in ("a", "b")
        ]
        assert _rows(conn) == expected
        assert expected == [("s", "a", 1, 330), ("s", "b", 1, 150)]
        assert items.get("s", "a", "a3") is not None


def test_fresh_db_has_no_snapshots(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "fresh.db")) as conn:
        assert _rows(conn) == []


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]):
    return make_config(*tmp_roots)


def _show(identifier: str, size: int = 300) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier,
        files=[FakeFile("t01.flac", b"F" * size, format="Flac")],
        metadata={"title": f"Title {identifier}"},
    )


def test_a_locked_sync_records_a_snapshot_at_its_end(config) -> None:
    _show("one")
    _show("two", 200)
    assert run_sync(config) == 0
    with closing(connect(config.state_db)) as conn:
        rows = _rows(conn)
        items = ItemRepo(conn)
        assert rows[-1] == ("fake-src", "coll1", 2, items.archived_bytes("fake-src", "coll1"))
        assert rows[-1][3] == 500


def test_a_failing_command_still_records_a_snapshot(config) -> None:
    def command() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_exclusive(config, command)
    with closing(connect(config.state_db)) as conn:
        assert _rows(conn) == [("fake-src", "coll1", 0, 0)]


def test_a_busy_lock_records_no_snapshot(config) -> None:
    from shakedown.lock import writer_lock

    with closing(connect(config.state_db)):
        pass
    with writer_lock(config):
        assert run_exclusive(config, lambda: 0) == 1
    with closing(connect(config.state_db)) as conn:
        assert _rows(conn) == []


def test_a_failing_snapshot_never_changes_the_run(
    config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _show("one")

    def broken(*_a, **_k) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(GrowthRepo, "record", broken)
    with caplog.at_level(logging.WARNING):
        assert run_sync(config) == 0
    assert "could not record library growth: disk on fire" in caplog.text
    with closing(connect(config.state_db)) as conn:
        item = ItemRepo(conn).get("fake-src", "coll1", "one")
        assert item is not None and item.status == ItemStatus.COMPLETE
        assert _rows(conn) == []


def _snap(conn, collection: str, at: datetime, complete: int, archive_bytes: int) -> None:
    conn.execute(
        "INSERT INTO growth_snapshots (source_name, collection_name, taken_at, complete,"
        " archive_bytes) VALUES ('s', ?, ?, ?, ?)",
        (collection, at.isoformat(), complete, archive_bytes),
    )


def test_trim_keeps_30_days_plus_the_baseline_and_never_touches_records(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "s.db")
    ItemRepo(conn).upsert(_item("a", "a1", ItemStatus.COMPLETE, 1))
    RunRepo(conn).start("s", "a", datetime(2026, 9, 1))
    now = datetime(2026, 11, 12, 12, 0)
    for days, n in ((40, 1), (35, 2), (10, 3), (0, 4)):
        _snap(conn, "a", now - timedelta(days=days), n, n)
    _snap(conn, "b", now - timedelta(days=50), 9, 9)

    GrowthRepo(conn).trim(now)

    kept = [(r["collection_name"], r["complete"]) for r in conn.execute(
        "SELECT * FROM growth_snapshots ORDER BY collection_name, taken_at")]
    # The newest snapshot before the cutoff stays: it carries into the window.
    assert kept == [("a", 2), ("a", 3), ("a", 4), ("b", 9)]
    assert ItemRepo(conn).get("s", "a", "a1") is not None
    assert RunRepo(conn).latest("s", "a") is not None


def test_series_carries_a_day_with_no_run_forward(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    today = date(2026, 11, 12)
    noon = datetime(2026, 11, 12, 12, 0)
    _snap(conn, "a", noon - timedelta(days=3), 1, 100)
    _snap(conn, "a", noon - timedelta(days=1, hours=2), 2, 200)
    _snap(conn, "a", noon - timedelta(days=1), 3, 300)  # later that day wins

    rows = GrowthRepo(conn).series([("s", "a"), ("s", "none")], today)

    assert rows == [
        {"date": "2026-11-09", "source": "s", "collection": "a", "complete": 1,
         "archive_bytes": 100},
        {"date": "2026-11-10", "source": "s", "collection": "a", "complete": 1,
         "archive_bytes": 100},
        {"date": "2026-11-11", "source": "s", "collection": "a", "complete": 3,
         "archive_bytes": 300},
        {"date": "2026-11-12", "source": "s", "collection": "a", "complete": 3,
         "archive_bytes": 300},
    ]


def test_series_spans_30_days_from_an_older_baseline(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    today = date(2026, 11, 12)
    _snap(conn, "a", datetime(2026, 10, 8), 1, 100)  # 35 days ago
    _snap(conn, "a", datetime(2026, 11, 7), 2, 200)

    rows = GrowthRepo(conn).series([("s", "a")], today)

    assert len(rows) == 30
    assert rows[0] == {"date": "2026-10-14", "source": "s", "collection": "a",
                       "complete": 1, "archive_bytes": 100}
    assert rows[-1]["date"] == "2026-11-12" and rows[-1]["complete"] == 2
