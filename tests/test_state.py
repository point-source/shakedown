from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile, Run
from shakedown.state import DriftRepo, ItemRepo, RunRepo


def _make_item(identifier: str = "x") -> Item:
    m = Manifest(files=(ManifestFile("a.flac", 100, "d41d"),))
    return Item(
        source_name="s", collection_name="c", identifier=identifier,
        status=ItemStatus.COMPLETE, archive_path=Path("/data/archive/s/c/x"),
        discovered_at=datetime(2026, 4, 26), downloaded_at=datetime(2026, 4, 26),
        recorded_manifest=m,
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


def test_drift_repo(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    drift = DriftRepo(conn)
    now = datetime.now()
    drift.record("s", "c", "x", "a.flac", "newhash", "oldhash", now)
    drift.record("s", "c", "x", "b.flac", "newhash2", "oldhash2", now)
    assert drift.count("s", "c") == 2
    drift.clear("s", "c", "x", "a.flac")
    assert drift.count("s", "c") == 1
