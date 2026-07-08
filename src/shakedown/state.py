"""DB-backed repositories for items, runs, and drift records."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from shakedown.models import Item, ItemStatus, Manifest, Run


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        source_name=row["source_name"],
        collection_name=row["collection_name"],
        identifier=row["identifier"],
        status=ItemStatus(row["status"]),
        archive_path=Path(row["archive_path"]) if row["archive_path"] else None,
        discovered_at=_parse_iso(row["discovered_at"]),
        downloaded_at=_parse_iso(row["downloaded_at"]),
        last_verified_at=_parse_iso(row["last_verified_at"]),
        restriction_reason=row["restriction_reason"],
        source_metadata=json.loads(row["source_metadata"]) if row["source_metadata"] else {},
        recorded_manifest=Manifest.from_json(json.loads(row["recorded_manifest"]))
        if row["recorded_manifest"]
        else None,
        change_signal=row["change_signal"],
    )


class ItemRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, source: str, collection: str, identifier: str) -> Item | None:
        cur = self.conn.execute(
            "SELECT * FROM items WHERE source_name=? AND collection_name=? AND identifier=?",
            (source, collection, identifier),
        )
        row = cur.fetchone()
        return _row_to_item(row) if row else None

    def upsert(self, item: Item) -> None:
        self.conn.execute(
            """
            INSERT INTO items (
                source_name, collection_name, identifier, status,
                archive_path, discovered_at, downloaded_at, last_verified_at,
                restriction_reason, source_metadata, recorded_manifest,
                change_signal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, collection_name, identifier) DO UPDATE SET
                status=excluded.status,
                archive_path=excluded.archive_path,
                discovered_at=COALESCE(excluded.discovered_at, items.discovered_at),
                downloaded_at=COALESCE(excluded.downloaded_at, items.downloaded_at),
                last_verified_at=COALESCE(excluded.last_verified_at, items.last_verified_at),
                restriction_reason=excluded.restriction_reason,
                source_metadata=excluded.source_metadata,
                recorded_manifest=COALESCE(excluded.recorded_manifest, items.recorded_manifest),
                change_signal=COALESCE(excluded.change_signal, items.change_signal)
            """,
            (
                item.source_name,
                item.collection_name,
                item.identifier,
                item.status.value,
                str(item.archive_path) if item.archive_path else None,
                _iso(item.discovered_at),
                _iso(item.downloaded_at),
                _iso(item.last_verified_at),
                item.restriction_reason,
                json.dumps(item.source_metadata),
                json.dumps(item.recorded_manifest.to_json()) if item.recorded_manifest else None,
                item.change_signal,
            ),
        )

    def list_for_collection(self, source: str, collection: str) -> Iterator[Item]:
        cur = self.conn.execute(
            "SELECT * FROM items WHERE source_name=? AND collection_name=? ORDER BY identifier",
            (source, collection),
        )
        for row in cur:
            yield _row_to_item(row)

    def count_by_status(self, source: str, collection: str) -> dict[ItemStatus, int]:
        cur = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM items
            WHERE source_name=? AND collection_name=?
            GROUP BY status
            """,
            (source, collection),
        )
        return {ItemStatus(row["status"]): row["n"] for row in cur}

    def delete(self, source: str, collection: str, identifier: str) -> None:
        self.conn.execute(
            "DELETE FROM items WHERE source_name=? AND collection_name=? AND identifier=?",
            (source, collection, identifier),
        )


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, source: str, collection: str, started_at: datetime) -> Run:
        cur = self.conn.execute(
            "INSERT INTO runs (source_name, collection_name, started_at) VALUES (?, ?, ?)",
            (source, collection, _iso(started_at)),
        )
        return Run(
            id=cur.lastrowid,
            source_name=source,
            collection_name=collection,
            started_at=started_at,
        )

    def finish(self, run: Run) -> None:
        self.conn.execute(
            """
            UPDATE runs SET
                finished_at=?, items_discovered=?, items_new=?, items_updated=?,
                items_failed=?, bytes_downloaded=?, errors=?, stale=?
            WHERE id=?
            """,
            (
                _iso(run.finished_at),
                run.items_discovered,
                run.items_new,
                run.items_updated,
                run.items_failed,
                run.bytes_downloaded,
                json.dumps(run.errors),
                int(run.stale),
                run.id,
            ),
        )

    def latest(self, source: str, collection: str) -> Run | None:
        cur = self.conn.execute(
            """
            SELECT * FROM runs
            WHERE source_name=? AND collection_name=?
            ORDER BY started_at DESC LIMIT 1
            """,
            (source, collection),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Run(
            id=row["id"],
            source_name=row["source_name"],
            collection_name=row["collection_name"],
            started_at=_parse_iso(row["started_at"]),  # type: ignore[arg-type]
            finished_at=_parse_iso(row["finished_at"]),
            items_discovered=row["items_discovered"],
            items_new=row["items_new"],
            items_updated=row["items_updated"],
            items_failed=row["items_failed"],
            bytes_downloaded=row["bytes_downloaded"],
            errors=json.loads(row["errors"]),
            stale=bool(row["stale"]),
        )


class DriftRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        source: str,
        collection: str,
        identifier: str,
        file_name: str,
        observed_md5: str | None,
        expected_md5: str | None,
        observed_at: datetime,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO drift (
                source_name, collection_name, identifier, file_name,
                observed_md5, expected_md5, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, collection_name, identifier, file_name) DO UPDATE SET
                observed_md5=excluded.observed_md5,
                expected_md5=excluded.expected_md5,
                observed_at=excluded.observed_at
            """,
            (source, collection, identifier, file_name, observed_md5, expected_md5, _iso(observed_at)),
        )

    def clear(self, source: str, collection: str, identifier: str, file_name: str) -> None:
        self.conn.execute(
            """
            DELETE FROM drift
            WHERE source_name=? AND collection_name=? AND identifier=? AND file_name=?
            """,
            (source, collection, identifier, file_name),
        )

    def count(self, source: str, collection: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drift WHERE source_name=? AND collection_name=?",
            (source, collection),
        )
        return cur.fetchone()["n"]

    def list(self, source: str, collection: str) -> Iterator[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT * FROM drift
            WHERE source_name=? AND collection_name=?
            ORDER BY identifier, file_name
            """,
            (source, collection),
        )
        yield from cur
