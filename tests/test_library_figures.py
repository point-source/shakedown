"""Each show's byte figures are stored with the show (§spec:library-stats "Stored
figures"): `status`, `GET /api/library` and the growth snapshots sum them through a
covering index and never read a manifest, so a cold page cache on a busy NAS cannot
make them slow. A reader of an older database, which never migrates, still answers
from the manifests."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from shakedown.db import SCHEMA_VERSION, connect, open_unmigrated
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.state import ItemRepo

S, C = "src", "coll"


def _item(identifier: str, files: list[ManifestFile] | None, *,
          status: ItemStatus = ItemStatus.COMPLETE, archived: bool = True) -> Item:
    return Item(
        source_name=S, collection_name=C, identifier=identifier, status=status,
        archive_path=Path(f"/archive/{identifier}") if archived else None,
        discovered_at=datetime(2026, 11, 1),
        downloaded_at=datetime(2026, 11, 2) if archived else None,
        recorded_manifest=Manifest(files=tuple(files)) if files is not None else None,
    )


# An SHN original kept archive-only beside the FLAC copy the library stages.
KEPT_TWICE = [
    ManifestFile("t01.flac", 300, "a"),
    ManifestFile("t02.flac", None, "b"),  # a size the source never listed
    ManifestFile("t01.shn", 500, "c", library=False),
    ManifestFile("info.txt", 7, "d"),
]
PLAIN = [ManifestFile("t01.flac", 1000, "e"), ManifestFile("t02.flac", 2000, "f")]


def _manifest_sums(conn: sqlite3.Connection) -> tuple[int, int]:
    """The figures as the manifests give them: the computation the stored figures
    replace, kept here to check the backfill and the old-schema answer."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(json_extract(f.value, '$.size')), 0),
               COALESCE(SUM(CASE WHEN COALESCE(json_extract(f.value, '$.library'), 1) != 0
                                 THEN json_extract(f.value, '$.size') END), 0)
        FROM items, json_each(items.recorded_manifest) AS f
        WHERE source_name=? AND collection_name=? AND archive_path IS NOT NULL
        """,
        (S, C),
    ).fetchone()
    return int(row[0]), int(row[1])


def _seed(repo: ItemRepo) -> None:
    repo.upsert(_item("kept-twice", KEPT_TWICE))
    repo.upsert(_item("plain", PLAIN))
    repo.upsert(_item("never-fetched", None, status=ItemStatus.FAILED, archived=False))
    # Removed from the archive, its manifest kept on record: no archive bytes.
    repo.upsert(_item("gone", PLAIN, status=ItemStatus.DISAPPEARED, archived=False))


def test_upsert_stores_the_figures_the_manifests_give(tmp_path: Path) -> None:
    repo = ItemRepo(connect(tmp_path / "s.db"))
    _seed(repo)
    assert repo.archived_bytes(S, C) == 300 + 500 + 7 + 1000 + 2000
    assert repo.library_bytes(S, C) == 300 + 7 + 1000 + 2000
    assert (repo.archived_bytes(S, C), repo.library_bytes(S, C)) == _manifest_sums(repo.conn)
    assert repo.list_playable_copies(S, C) == ["kept-twice"]


def test_an_upsert_without_a_manifest_keeps_the_recorded_figures(tmp_path: Path) -> None:
    """The record keeps a show's manifest when a write carries none (a failed refetch),
    so its figures stay with it."""
    repo = ItemRepo(connect(tmp_path / "s.db"))
    repo.upsert(_item("plain", PLAIN))
    repo.upsert(_item("plain", None, status=ItemStatus.FAILED))
    assert repo.archived_bytes(S, C) == 3000
    assert repo.library_bytes(S, C) == 3000


def test_a_new_manifest_replaces_the_figures(tmp_path: Path) -> None:
    repo = ItemRepo(connect(tmp_path / "s.db"))
    repo.upsert(_item("show", KEPT_TWICE))
    repo.upsert(_item("show", PLAIN))  # re-fetched as FLAC only
    assert repo.archived_bytes(S, C) == repo.library_bytes(S, C) == 3000
    assert repo.list_playable_copies(S, C) == []


def test_a_deleted_show_leaves_the_totals(tmp_path: Path) -> None:
    repo = ItemRepo(connect(tmp_path / "s.db"))
    _seed(repo)
    repo.delete(S, C, "plain")
    assert repo.archived_bytes(S, C) == 807
    assert repo.library_bytes(S, C) == 307


def test_only_a_complete_show_counts_as_kept_twice(tmp_path: Path) -> None:
    repo = ItemRepo(connect(tmp_path / "s.db"))
    repo.upsert(_item("show", KEPT_TWICE, status=ItemStatus.FAILED))
    assert repo.list_playable_copies(S, C) == []


def _to_v11(db: Path) -> None:
    """Turn a current database back into v11: no stored figures."""
    with closing(sqlite3.connect(db)) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='items'"
            " AND (sql LIKE '%archive_bytes%' OR sql LIKE '%playable_copy%')"
        ).fetchall():
            conn.execute(f"DROP INDEX {name}")
        for column in ("archive_bytes", "library_bytes", "playable_copy"):
            conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
        conn.execute("UPDATE schema_version SET version = 11")
        conn.commit()


def test_the_v12_migration_backfills_every_show_once(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with closing(connect(db)) as conn:
        _seed(ItemRepo(conn))
    _to_v11(db)

    with closing(connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        repo = ItemRepo(conn)
        assert (repo.archived_bytes(S, C), repo.library_bytes(S, C)) == _manifest_sums(conn)
        assert repo.archived_bytes(S, C) == 3807
        assert repo.list_playable_copies(S, C) == ["kept-twice"]


def test_a_reader_of_a_v11_database_answers_from_the_manifests_without_migrating(
    tmp_path: Path,
) -> None:
    db = tmp_path / "s.db"
    with closing(connect(db)) as conn:
        _seed(ItemRepo(conn))
    _to_v11(db)

    with closing(open_unmigrated(db)) as conn:
        repo = ItemRepo(conn)
        assert repo.archived_bytes(S, C) == 3807
        assert repo.library_bytes(S, C) == 3307
        assert repo.list_playable_copies(S, C) == ["kept-twice"]
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11


def test_the_fresh_schema_matches_the_migrated_one(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    connect(db).close()
    _to_v11(db)
    migrated = connect(db)
    fresh = connect(tmp_path / "fresh.db")

    def shape(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        indexes = sorted(
            r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='items'"
                " AND sql IS NOT NULL"
            )
        )
        return columns, indexes

    assert shape(fresh) == shape(migrated)


def _reads(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Record every (table, column) the next statements read."""
    seen: set[tuple[str, str]] = set()

    def authorizer(action: int, table: str | None, column: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and table and column:
            seen.add((table, column))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    return seen


def test_twenty_thousand_shows_are_summed_without_reading_a_manifest(tmp_path: Path) -> None:
    """A collection the size of the operator's largest, with full manifests and
    metadata: the byte totals and the kept-twice list read only the index."""
    db = tmp_path / "s.db"
    with closing(connect(db)) as conn:
        repo = ItemRepo(conn)
        files = [ManifestFile(f"d{d}t{t:02d}.flac", 30_000_000 + t, "0" * 32, title=f"Song {t}")
                 for d in (1, 2, 3) for t in range(1, 15)]
        conn.execute("BEGIN")
        for i in range(20_000):
            item = _item(f"gd{i:05d}", files + ([KEPT_TWICE[2]] if i % 50 == 0 else []))
            item.source_metadata = {"title": f"Show {i}", "description": "x" * 2000}
            repo.upsert(item)
        conn.execute("COMMIT")

    with closing(open_unmigrated(db)) as conn:
        repo = ItemRepo(conn)
        seen = _reads(conn)
        archive, library = repo.archived_bytes(S, C), repo.library_bytes(S, C)
        kept = repo.list_playable_copies(S, C)
        conn.set_authorizer(None)

        per_show = sum(f.size or 0 for f in files)
        assert library == per_show * 20_000
        assert archive == library + 500 * 400
        assert len(kept) == 400
        assert not {c for t, c in seen if t == "items"} & {"recorded_manifest", "source_metadata"}
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT SUM(archive_bytes), SUM(library_bytes) FROM items"
            " WHERE source_name=? AND collection_name=? AND archive_path IS NOT NULL", (S, C)))
        assert "COVERING INDEX" in plan, plan


def test_stored_figures_are_plain_integers_in_the_record(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    ItemRepo(conn).upsert(_item("kept-twice", KEPT_TWICE))
    row = conn.execute("SELECT archive_bytes, library_bytes, playable_copy, recorded_manifest"
                       " FROM items").fetchone()
    assert (row[0], row[1], row[2]) == (807, 307, 1)
    assert len(json.loads(row[3])) == 4
