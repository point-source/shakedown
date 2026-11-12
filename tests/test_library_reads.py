"""What the library view reads from the state database (§spec:library-stats):
library bytes from the recorded manifests, the most recently mirrored shows, and
each collection's last good sync (§spec:run-history "Last good sync")."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from shakedown.activity import ABANDONED_ERROR, ActivityKind, ActivityRepo
from shakedown.db import connect
from shakedown.history import last_good_sync_at
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile, Run, RunKind
from shakedown.state import ItemRepo, RunRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _item(identifier: str, *, collection: str = "c", files: tuple[ManifestFile, ...],
          archived: bool = True, downloaded: datetime | None = datetime(2026, 11, 1),
          metadata: dict | None = None) -> Item:
    return Item(
        source_name="s", collection_name=collection, identifier=identifier,
        status=ItemStatus.COMPLETE,
        archive_path=Path(f"/a/{identifier}") if archived else None,
        discovered_at=datetime(2026, 10, 1), downloaded_at=downloaded,
        source_metadata=metadata or {}, recorded_manifest=Manifest(files=files),
    )


def test_library_bytes_leave_out_archive_only_files(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "s.db")) as conn:
        items = ItemRepo(conn)
        items.upsert(_item("shn", files=(
            ManifestFile("t1.shn", 500, "a", library=False),
            ManifestFile("t1.mp3", 88, "b"),
            ManifestFile("info.txt", 10, "c"),
        )))
        items.upsert(_item("flac", files=(ManifestFile("t1.flac", 400, "d"),)))
        items.upsert(_item("gone", files=(ManifestFile("t1.flac", 999, "e"),), archived=False))
        items.upsert(_item("other", collection="d", files=(
            ManifestFile("t1.flac", 70, "f"),)))

        assert items.library_bytes("s", "c") == 88 + 10 + 400
        assert items.archived_bytes("s", "c") == 500 + 88 + 10 + 400
        assert items.library_bytes("s", "d") == items.archived_bytes("s", "d") == 70


def test_library_bytes_equal_the_staged_files_after_a_sync(
    tmp_roots: tuple[Path, Path],
) -> None:
    """An SHN show stages its playable copy, not the SHN originals
    (§spec:quality-selection): library bytes are the staged files' sizes."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd77"] = FakeItem("gd77", files=[
        FakeFile("t01.shn", b"S" * 500, format="Shorten"),
        FakeFile("t01_vbr.mp3", b"M" * 88, format="VBR MP3", derived_from="t01.shn"),
        FakeFile("info.txt", b"lineage", format="Text"),
    ])
    assert run_sync(config) == 0

    staged = sum(p.stat().st_size for p in (library / "fake-src" / "coll1").rglob("*")
                 if p.is_file())
    with closing(connect(config.state_db)) as conn:
        items = ItemRepo(conn)
        assert items.library_bytes("fake-src", "coll1") == staged == 88 + 7
        assert items.library_bytes("fake-src", "coll1") < items.archived_bytes(
            "fake-src", "coll1")


def test_recently_downloaded_lists_the_newest_20_of_the_configured_collections(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 11, 1)
    files = (ManifestFile("t.flac", 1, "m"),)
    with closing(connect(tmp_path / "s.db")) as conn:
        items = ItemRepo(conn)
        for n in range(25):
            items.upsert(_item(f"show{n:02d}", files=files,
                               downloaded=base + timedelta(hours=n),
                               metadata={"title": f"Show {n}"}))
        items.upsert(_item("never", files=files, downloaded=None))
        items.upsert(_item("elsewhere", collection="gone", files=files,
                           downloaded=base + timedelta(days=9)))
        # A re-fetch moves a show to the front.
        items.upsert(_item("show03", files=files, downloaded=base + timedelta(days=5),
                           metadata={"title": "Show 3"}))

        rows = items.recently_downloaded([("s", "c")], limit=20)

    assert len(rows) == 20
    assert rows[0] == ("s", "c", "show03", base + timedelta(days=5), "Show 3")
    assert [r[2] for r in rows[1:4]] == ["show24", "show23", "show22"]
    assert "elsewhere" not in {r[2] for r in rows}


def test_recently_downloaded_title_is_the_sources_title_or_null(tmp_path: Path) -> None:
    files = (ManifestFile("t.flac", 1, "m"),)
    with closing(connect(tmp_path / "s.db")) as conn:
        items = ItemRepo(conn)
        cases = {
            "plain": {"title": "Live at the Fillmore"},
            "listed": {"title": ["First", "Second"]},
            "missing": {},
            "number": {"title": 1977},
        }
        for n, (identifier, metadata) in enumerate(cases.items()):
            items.upsert(_item(identifier, files=files, metadata=metadata,
                               downloaded=datetime(2026, 11, 1, n)))
        titles = {r[2]: r[4] for r in items.recently_downloaded([("s", "c")], limit=20)}

    assert titles == {"plain": "Live at the Fillmore", "listed": "First",
                      "missing": None, "number": None}


def _run(conn, *, kind: RunKind = RunKind.SYNC, finished: datetime | None,
         stale: bool = False, errors: list[str] | None = None, failed: int = 0) -> Run:
    started = (finished or datetime(2026, 11, 12)) - timedelta(minutes=10)
    run = RunRepo(conn).start("s", "c", started, kind=kind)
    if finished is not None:
        run.finished_at = finished
        run.stale = stale
        run.errors = errors or []
        run.items_failed = failed
        RunRepo(conn).finish(run)
    return run


def test_last_good_sync_is_the_latest_completed_sync_or_catch_up(tmp_path: Path) -> None:
    day = datetime(2026, 11, 10, 3, 0)
    with closing(connect(tmp_path / "s.db")) as conn:
        assert last_good_sync_at(conn, "s", "c", datetime.now()) is None
        _run(conn, finished=day)
        assert last_good_sync_at(conn, "s", "c", datetime.now()) == day
        _run(conn, kind=RunKind.CATCH_UP, finished=day + timedelta(hours=1), failed=2)
        assert last_good_sync_at(conn, "s", "c", datetime.now()) == day + timedelta(hours=1)
        # Not good: a stale listing, a crash, a killed run, a refetch, an open run.
        _run(conn, finished=day + timedelta(hours=2), stale=True)
        _run(conn, finished=day + timedelta(hours=3), errors=["sync crashed: boom"])
        _run(conn, finished=day + timedelta(hours=4), errors=[ABANDONED_ERROR])
        # Closed by serve after it saw its child killed (trigger.close_abandoned).
        _run(conn, finished=day + timedelta(hours=4, minutes=30),
             errors=["run ended unexpectedly (killed by SIGKILL)"])
        _run(conn, kind=RunKind.REFETCH, finished=day + timedelta(hours=5))
        _run(conn, finished=None)
        assert last_good_sync_at(conn, "s", "c", datetime.now()) == day + timedelta(hours=1)


def test_an_ended_activity_entry_decides_the_result(tmp_path: Path) -> None:
    day = datetime(2026, 11, 10, 3, 0)
    with closing(connect(tmp_path / "s.db")) as conn:
        good = _run(conn, finished=day)
        killed = _run(conn, finished=day + timedelta(hours=1))
        activity = ActivityRepo(conn)
        entry = activity.create("s", "c", ActivityKind.SYNC, run_id=killed.id,
                                started_by_serve=False, at=day)
        activity.end(entry, "failed_before_completion", day + timedelta(hours=1))
        assert good.id is not None
        assert last_good_sync_at(conn, "s", "c", datetime.now()) == day


def test_last_good_sync_on_a_pre_v10_database(tmp_path: Path) -> None:
    db = tmp_path / "v9.db"
    day = datetime(2026, 11, 10, 3, 0)
    with closing(connect(db)) as conn:
        _run(conn, finished=day)
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    from shakedown.db import connect_reader

    with closing(connect_reader(db)) as conn:
        assert last_good_sync_at(conn, "s", "c", datetime.now()) == day
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
