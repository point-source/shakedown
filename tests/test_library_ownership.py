"""End to end: library ownership and stale-copy cleanup through the real commands
(§spec:library-ownership, §spec:library-cleanup; §req:success-criteria #19, #20)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from shakedown.db import connect
from shakedown.restage import run_restage
from shakedown.state import PlacementRepo, RunRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

LAYOUT = "{date} - {venue}"


def _base(library: Path) -> Path:
    return library / "fake-src" / "coll1"


def _tree(library: Path) -> list[tuple[str, int]]:
    """(path, inode) of every library file — equal trees are the same links."""
    base = _base(library)
    return sorted(
        (str(p.relative_to(base)), p.stat().st_ino) for p in base.rglob("*") if p.is_file()
    )


def _names(library: Path) -> list[str]:
    return [path for path, _ in _tree(library)]


def _seed(identifier: str, *, files: dict[str, bytes] | None = None, **metadata) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=n, content=c) for n, c in (files or {"t1.flac": b"x"}).items()],
        metadata=metadata,
    )


def _two_tapes() -> None:
    _seed("gd77-05-08.sbd", files={"t1.flac": b"sbd"}, date="1977-05-08", venue="Barton Hall")
    _seed("gd77-05-08.aud", files={"t1.flac": b"aud"}, date="1977-05-08", venue="Barton Hall")
    _seed("gd77-05-09.sbd", files={"t1.flac": b"other"}, date="1977-05-09", venue="Buffalo")


def _last_errors(config) -> list[str]:
    run = RunRepo(connect(config.state_db)).latest("fake-src", "coll1")
    assert run is not None
    return run.errors


def test_two_recordings_of_one_show_each_get_a_labelled_folder(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _two_tapes()

    assert run_sync(config) == 0  # a separation is not a failure

    assert _names(library) == [
        "1977-05-08 - Barton Hall [gd77-05-08.aud]/t1.flac",
        "1977-05-08 - Barton Hall [gd77-05-08.sbd]/t1.flac",
        "1977-05-09 - Buffalo/t1.flac",
    ]
    first = _tree(library)

    assert run_sync(config) == 0
    assert _tree(library) == first
    assert run_restage(config) == 0
    assert _tree(library) == first


def test_second_recording_arriving_later_moves_the_first_once(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("gd-a", date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0
    assert _names(library) == ["1977-05-08 - Barton Hall/t1.flac"]

    _seed("gd-b", date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0

    assert _names(library) == [
        "1977-05-08 - Barton Hall [gd-a]/t1.flac",
        "1977-05-08 - Barton Hall [gd-b]/t1.flac",
    ]
    assert FakePlugin.fetch_count == {"gd-a": 1, "gd-b": 1}


def test_own_file_under_a_shows_name_is_untouched_and_reported(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("gd-a", files={"t1.flac": b"a1", "t2.flac": b"a2"},
          date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0
    folder = _base(library) / "1977-05-08 - Barton Hall"
    (folder / "t1.flac").unlink()
    mine = folder / "t1.flac"
    mine.write_bytes(b"my own edit")

    assert run_sync(config) == 1  # a per-show issue

    assert mine.read_bytes() == b"my own edit"
    assert (folder / "t2.flac").is_file()
    assert any(
        e.startswith("gd-a: ") and "already holds a file shakedown did not place" in e
        for e in _last_errors(config)
    )
    assert FakePlugin.fetch_count == {"gd-a": 1}


def test_layout_change_synced_without_restage_leaves_no_stale_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    _two_tapes()
    assert run_sync(make_config(archive, library, library_layout=LAYOUT)) == 0

    relaid = make_config(archive, library, library_layout="{venue}/{date}")
    assert run_sync(relaid) == 0

    assert _names(library) == [
        "Barton Hall/1977-05-08 [gd77-05-08.aud]/t1.flac",
        "Barton Hall/1977-05-08 [gd77-05-08.sbd]/t1.flac",
        "Buffalo/1977-05-09/t1.flac",
    ]
    # `find <library> -type f -links 1` lists nothing shakedown placed.
    assert [p for p in library.rglob("*") if p.is_file() and p.stat().st_nlink == 1] == []
    assert FakePlugin.fetch_count == {i: 1 for i in FakePlugin.items}


def test_refetch_with_new_metadata_moves_the_show(tmp_roots: tuple[Path, Path]) -> None:
    """An upstream rename is a metadata change: the show moves, the old folder goes."""
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("gd-a", files={"t1.flac": b"v1"}, date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0

    _seed("gd-a", files={"t1.flac": b"v2"}, date="1977-05-08", venue="Cornell")
    assert run_sync(config) == 0

    assert _names(library) == ["1977-05-08 - Cornell/t1.flac"]
    assert (_base(library) / "1977-05-08 - Cornell" / "t1.flac").read_bytes() == b"v2"


def test_pruned_show_leaves_neither_archive_nor_library_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT, prune_disappeared=True)
    _two_tapes()
    assert run_sync(config) == 0

    del FakePlugin.items["gd77-05-08.aud"]
    assert run_sync(config) == 0

    assert not (archive / "fake-src" / "coll1" / "gd77-05-08.aud").exists()
    assert _names(library) == [
        "1977-05-08 - Barton Hall/t1.flac",
        "1977-05-09 - Buffalo/t1.flac",
    ]


def test_restage_keeps_files_shakedown_did_not_place(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("gd-a", date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0
    notes = _base(library) / "my-notes.txt"
    notes.write_text("keep me")

    assert run_restage(config) == 0

    assert notes.read_text() == "keep me"
    assert _names(library) == ["1977-05-08 - Barton Hall/t1.flac", "my-notes.txt"]


def _downgrade_to_v5(db_path: Path) -> None:
    """Turn the state DB into what the previous version left: schema 5, the collision
    columns, and no placement record."""
    conn = sqlite3.connect(db_path)
    for table in ("library_placements", "library_folders", "library_unplaced",
                  "library_adoption"):
        conn.execute(f"DROP TABLE {table}")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "collisions_dropped" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN collisions_dropped INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE runs ADD COLUMN collision_paths TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE schema_version SET version=5")
    conn.commit()
    conn.close()


def test_upgrade_clears_a_show_linked_into_two_folders_without_downloading(
    tmp_roots: tuple[Path, Path],
) -> None:
    """The ARU case on the NAS: the old version linked a show into two folders and left
    a single-link copy whose archive file was since replaced."""
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("aru-1", files={"t1.flac": b"one", "t2.flac": b"two"},
          date="1992-03-01", venue="Variety Playhouse")
    assert run_sync(config) == 0
    current = _base(library) / "1992-03-01 - Variety Playhouse"
    old = _base(library) / "1992-03-01 - Variety Playhouse Atlanta"
    old.mkdir()
    for name in ("t1.flac", "t2.flac"):
        os.link(current / name, old / name)
    stray = old / "t0.flac"
    stray.write_bytes(b"old copy, archive file replaced since")
    assert config.state_db is not None
    _downgrade_to_v5(config.state_db)

    assert run_sync(config) == 0

    assert _names(library) == [
        "1992-03-01 - Variety Playhouse Atlanta/t0.flac",
        "1992-03-01 - Variety Playhouse/t1.flac",
        "1992-03-01 - Variety Playhouse/t2.flac",
    ]
    assert stray.is_file()
    assert FakePlugin.fetch_count == {"aru-1": 1}
    repo = PlacementRepo(connect(config.state_db))
    assert repo.list_unplaced("fake-src", "coll1") == [
        "1992-03-01 - Variety Playhouse Atlanta/t0.flac"
    ]


# --- surfaces: run summary, status text/JSON (GET /status shares the JSON) ----------


def test_run_summary_reports_separations_without_failing(
    tmp_roots: tuple[Path, Path], caplog
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _two_tapes()

    with caplog.at_level("INFO"):
        assert run_sync(config) == 0

    assert (
        "fake-src/coll1: 2 shows render the same library folder; each was staged into "
        "its own folder labelled with its identifier"
    ) in caplog.messages


def test_status_lists_separations_and_files_not_placed(
    tmp_roots: tuple[Path, Path], capsys
) -> None:
    import json

    from shakedown.status import print_status

    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _two_tapes()
    assert run_sync(config) == 0
    notes = _base(library) / "1977-05-09 - Buffalo" / "notes.txt"
    notes.write_text("mine")
    # A layout change moves Buffalo; its old folder keeps my notes, which get reported.
    relaid = make_config(archive, library, library_layout="{venue} {date}")
    assert run_sync(relaid) == 0

    print_status(relaid, as_json=True)
    summary = json.loads(capsys.readouterr().out)[0]
    sbd = _base(library) / "Barton Hall 1977-05-08 [gd77-05-08.sbd]"
    aud = _base(library) / "Barton Hall 1977-05-08 [gd77-05-08.aud]"
    assert summary["separations"] == [
        {"identifier": "gd77-05-08.aud", "folder": str(aud)},
        {"identifier": "gd77-05-08.sbd", "folder": str(sbd)},
    ]
    assert summary["unplaced_files"] == [str(notes)]

    print_status(relaid, as_json=False)
    out = capsys.readouterr().out
    assert "  shared folder names (2 shows, each labelled by identifier):\n" in out
    assert f"    - gd77-05-08.aud → {str(aud)!r}\n" in out
    assert f"    - gd77-05-08.sbd → {str(sbd)!r}\n" in out
    assert (
        "  library files not placed by shakedown: 1 (left in place; paths in status --json)\n"
    ) in out


def test_status_caps_the_separation_list(tmp_roots: tuple[Path, Path], capsys) -> None:
    from shakedown.status import print_status

    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    for i in range(12):
        _seed(f"gd-{i:02d}", date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0

    print_status(config, as_json=False)
    out = capsys.readouterr().out
    assert "  shared folder names (12 shows, each labelled by identifier):\n" in out
    assert sum(line.startswith("    - gd-") for line in out.splitlines()) == 10
    assert "    ... and 2 more\n" in out


def test_status_is_quiet_without_separations_or_strays(
    tmp_roots: tuple[Path, Path], capsys
) -> None:
    from shakedown.status import print_status

    archive, library = tmp_roots
    config = make_config(archive, library, library_layout=LAYOUT)
    _seed("gd-a", date="1977-05-08", venue="Barton Hall")
    assert run_sync(config) == 0

    print_status(config, as_json=False)
    out = capsys.readouterr().out
    assert "shared folder names" not in out
    assert "not placed by shakedown" not in out
