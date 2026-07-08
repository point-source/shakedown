"""DB-backed repositories for items, runs, and drift records."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from shakedown.models import (
    Item,
    ItemStatus,
    Manifest,
    OperationOutcome,
    OperationStatus,
    OperationType,
    Run,
)


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


def _row_to_operation_outcome(row: sqlite3.Row) -> OperationOutcome:
    return OperationOutcome(
        id=row["id"],
        operation=OperationType(row["operation"]),
        status=OperationStatus(row["status"]),
        source_name=row["source_name"],
        collection_name=row["collection_name"],
        started_at=_parse_iso(row["started_at"]),  # type: ignore[arg-type]
        updated_at=_parse_iso(row["updated_at"]),  # type: ignore[arg-type]
        finished_at=_parse_iso(row["finished_at"]),
        phase=row["phase"],
        affected_item=row["affected_item"],
        affected_path=row["affected_path"],
        completed_work=json.loads(row["completed_work"]),
        preservation_context=row["preservation_context"],
        deletion_context=row["deletion_context"],
        safe_next_action=row["safe_next_action"],
        errors=json.loads(row["errors"]),
        resolved_at=_parse_iso(row["resolved_at"]),
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
                items_failed=?, bytes_downloaded=?, errors=?, stale=?,
                collisions_dropped=?, collision_paths=?
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
                run.collisions_dropped,
                json.dumps(run.collision_paths),
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
            collisions_dropped=row["collisions_dropped"],
            collision_paths=json.loads(row["collision_paths"]),
        )


class OperationOutcomeRepo:
    ACTIONABLE_STATUSES = (
        OperationStatus.IN_PROGRESS,
        OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
        OperationStatus.FAILED_BEFORE_COMPLETION,
        OperationStatus.STALE_ENUMERATION,
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(
        self,
        operation: OperationType,
        source: str,
        collection: str,
        started_at: datetime,
        *,
        phase: str | None = None,
        safe_next_action: str | None = None,
    ) -> OperationOutcome:
        cur = self.conn.execute(
            """
            INSERT INTO operation_outcomes (
                operation, status, source_name, collection_name, started_at, updated_at,
                phase, safe_next_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation.value,
                OperationStatus.IN_PROGRESS.value,
                source,
                collection,
                _iso(started_at),
                _iso(started_at),
                phase,
                safe_next_action,
            ),
        )
        return OperationOutcome(
            id=cur.lastrowid,
            operation=operation,
            status=OperationStatus.IN_PROGRESS,
            source_name=source,
            collection_name=collection,
            started_at=started_at,
            updated_at=started_at,
            phase=phase,
            safe_next_action=safe_next_action,
        )

    def finish(
        self,
        outcome: OperationOutcome,
        status: OperationStatus,
        finished_at: datetime,
        *,
        phase: str | None = None,
        affected_item: str | None = None,
        affected_path: str | None = None,
        completed_work: dict[str, Any] | None = None,
        preservation_context: str | None = None,
        deletion_context: str | None = None,
        safe_next_action: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        outcome.status = status
        outcome.finished_at = finished_at
        outcome.updated_at = finished_at
        outcome.phase = phase
        outcome.affected_item = affected_item
        outcome.affected_path = affected_path
        outcome.completed_work = completed_work or {}
        outcome.preservation_context = preservation_context
        outcome.deletion_context = deletion_context
        outcome.safe_next_action = safe_next_action
        outcome.errors = errors or []
        self.conn.execute(
            """
            UPDATE operation_outcomes SET
                status=?, updated_at=?, finished_at=?, phase=?, affected_item=?,
                affected_path=?, completed_work=?, preservation_context=?,
                deletion_context=?, safe_next_action=?, errors=?
            WHERE id=?
            """,
            (
                outcome.status.value,
                _iso(outcome.updated_at),
                _iso(outcome.finished_at),
                outcome.phase,
                outcome.affected_item,
                outcome.affected_path,
                json.dumps(outcome.completed_work),
                outcome.preservation_context,
                outcome.deletion_context,
                outcome.safe_next_action,
                json.dumps(outcome.errors),
                outcome.id,
            ),
        )

    def resolve_actionable(
        self,
        operation: OperationType,
        source: str,
        collection: str,
        resolved_at: datetime,
        *,
        affected_item: str | None = None,
    ) -> None:
        statuses = tuple(s.value for s in self.ACTIONABLE_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        params: list[object] = [
            _iso(resolved_at),
            operation.value,
            source,
            collection,
            *statuses,
        ]
        item_filter = ""
        if affected_item is not None:
            item_filter = "AND (affected_item IS NULL OR affected_item=?)"
            params.append(affected_item)
        self.conn.execute(
            f"""
            UPDATE operation_outcomes
            SET resolved_at=?
            WHERE operation=?
              AND source_name=?
              AND collection_name=?
              AND resolved_at IS NULL
              AND status IN ({placeholders})
              {item_filter}
            """,
            params,
        )

    def latest_actionable(self, source: str, collection: str) -> OperationOutcome | None:
        statuses = tuple(s.value for s in self.ACTIONABLE_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        cur = self.conn.execute(
            f"""
            SELECT * FROM operation_outcomes
            WHERE source_name=?
              AND collection_name=?
              AND resolved_at IS NULL
              AND status IN ({placeholders})
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (source, collection, *statuses),
        )
        row = cur.fetchone()
        return _row_to_operation_outcome(row) if row else None

    def get(self, outcome_id: int) -> OperationOutcome | None:
        cur = self.conn.execute(
            "SELECT * FROM operation_outcomes WHERE id=?",
            (outcome_id,),
        )
        row = cur.fetchone()
        return _row_to_operation_outcome(row) if row else None


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
