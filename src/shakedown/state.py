"""DB-backed repositories for items, runs, drift, library placement, and pending handoffs."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shakedown.db import transaction
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
        not_playable_reason=row["not_playable_reason"],
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
                change_signal, not_playable_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, collection_name, identifier) DO UPDATE SET
                status=excluded.status,
                archive_path=excluded.archive_path,
                discovered_at=COALESCE(excluded.discovered_at, items.discovered_at),
                downloaded_at=COALESCE(excluded.downloaded_at, items.downloaded_at),
                last_verified_at=COALESCE(excluded.last_verified_at, items.last_verified_at),
                restriction_reason=excluded.restriction_reason,
                source_metadata=excluded.source_metadata,
                recorded_manifest=COALESCE(excluded.recorded_manifest, items.recorded_manifest),
                change_signal=COALESCE(excluded.change_signal, items.change_signal),
                not_playable_reason=excluded.not_playable_reason
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
                item.not_playable_reason,
            ),
        )

    def list_for_collection(self, source: str, collection: str) -> Iterator[Item]:
        cur = self.conn.execute(
            "SELECT * FROM items WHERE source_name=? AND collection_name=? ORDER BY identifier",
            (source, collection),
        )
        for row in cur:
            yield _row_to_item(row)

    def list_for_planning(self, source: str, collection: str) -> Iterator[Item]:
        """The collection's items without their source metadata, which planning never
        reads and which dominates memory on a large collection. Never upsert one of
        these: `get()` the full item first."""
        cur = self.conn.execute(
            """
            SELECT source_name, collection_name, identifier, status, archive_path,
                   discovered_at, downloaded_at, last_verified_at, restriction_reason,
                   NULL AS source_metadata, recorded_manifest, change_signal,
                   not_playable_reason
            FROM items WHERE source_name=? AND collection_name=? ORDER BY identifier
            """,
            (source, collection),
        )
        for row in cur:
            yield _row_to_item(row)

    def list_restricted(self, source: str, collection: str) -> list[tuple[str, str | None]]:
        """(identifier, restriction_reason) of the collection's unavailable items and of
        complete items whose lossless files are refused (they carry a reason)."""
        cur = self.conn.execute(
            """
            SELECT identifier, restriction_reason FROM items
            WHERE source_name=? AND collection_name=?
              AND (status=? OR (status=? AND restriction_reason IS NOT NULL))
            ORDER BY identifier
            """,
            (source, collection, ItemStatus.UNAVAILABLE.value, ItemStatus.COMPLETE.value),
        )
        return [(row["identifier"], row["restriction_reason"]) for row in cur]

    def list_not_playable(self, source: str, collection: str) -> list[tuple[str, str]]:
        """(identifier, reason) of the collection's complete shows with no copy a
        common player reads (§spec:quality-selection)."""
        cur = self.conn.execute(
            """
            SELECT identifier, not_playable_reason FROM items
            WHERE source_name=? AND collection_name=? AND status=?
              AND not_playable_reason IS NOT NULL
            ORDER BY identifier
            """,
            (source, collection, ItemStatus.COMPLETE.value),
        )
        return [(row["identifier"], row["not_playable_reason"]) for row in cur]

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

    def archived_bytes(self, source: str, collection: str) -> int:
        """Total size of the archive copies on record, from their manifests: a cheap
        stand-in for walking the archive tree, which on a NAS takes tens of seconds."""
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(json_extract(f.value, '$.size')), 0) AS total
            FROM items, json_each(items.recorded_manifest) AS f
            WHERE source_name=? AND collection_name=? AND archive_path IS NOT NULL
            """,
            (source, collection),
        ).fetchone()
        return int(row["total"])

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

    def mark_started(self, run: Run, started_at: datetime) -> None:
        """Move a run opened ahead of time to when its work actually began."""
        run.started_at = started_at
        self.conn.execute(
            "UPDATE runs SET started_at=? WHERE id=?", (_iso(started_at), run.id)
        )

    def finish(self, run: Run) -> None:
        self.conn.execute(
            """
            UPDATE runs SET
                finished_at=?, items_discovered=?, items_new=?, items_updated=?,
                items_failed=?, bytes_downloaded=?, errors=?, stale=?,
                failure_notice_error=?
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
                run.failure_notice_error,
                run.id,
            ),
        )

    def close_abandoned(self, run_id: int, reason: str, finished_at: datetime) -> None:
        """Close a run its process left open (it was killed), adding ``reason`` to
        its errors. A run that is already closed is left as it is."""
        row = self.conn.execute(
            "SELECT errors, finished_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None or row["finished_at"] is not None:
            return
        errors = [*json.loads(row["errors"] or "[]"), reason]
        self.conn.execute(
            "UPDATE runs SET finished_at=?, errors=? WHERE id=? AND finished_at IS NULL",
            (_iso(finished_at), json.dumps(errors), run_id),
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
            failure_notice_error=row["failure_notice_error"],
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

    def mark_started(self, outcome: OperationOutcome, started_at: datetime) -> None:
        """Move an outcome opened ahead of time to when its work actually began."""
        outcome.started_at = started_at
        outcome.updated_at = started_at
        self.conn.execute(
            "UPDATE operation_outcomes SET started_at=?, updated_at=? WHERE id=?",
            (_iso(started_at), _iso(started_at), outcome.id),
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

    def close_abandoned(self, outcome_id: int, reason: str, finished_at: datetime) -> None:
        """Mark an outcome its process left in progress (it was killed) as failed
        before completion. An outcome that is already finished is left as it is."""
        outcome = self.get(outcome_id)
        if outcome is None or outcome.finished_at is not None:
            return
        self.finish(
            outcome,
            OperationStatus.FAILED_BEFORE_COMPLETION,
            finished_at,
            phase=outcome.phase,
            affected_item=outcome.affected_item,
            affected_path=outcome.affected_path,
            completed_work=outcome.completed_work,
            preservation_context="completed archive work retained",
            deletion_context=outcome.deletion_context,
            safe_next_action="check the logged error and rerun sync",
            errors=[*outcome.errors, reason],
        )

    def resolve_actionable(
        self,
        operation: OperationType,
        source: str,
        collection: str,
        resolved_at: datetime,
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
        self.conn.execute(
            f"""
            UPDATE operation_outcomes
            SET resolved_at=?
            WHERE operation=?
              AND source_name=?
              AND collection_name=?
              AND resolved_at IS NULL
              AND status IN ({placeholders})
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


class PlacementRepo:
    """The library placement record (§spec:library-ownership, §spec:library-cleanup):
    which show owns each library file shakedown placed, each show's folder, files found
    in the library that shakedown did not place, and which collections have adopted
    their pre-existing links. Paths are relative to the collection's library folder."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- placed files ---

    def placed_inodes(self, source: str, collection: str, identifier: str) -> dict[str, int]:
        """path -> placed inode for every file the show has placed."""
        cur = self.conn.execute(
            """
            SELECT path, inode FROM library_placements
            WHERE source_name=? AND collection_name=? AND identifier=?
            """,
            (source, collection, identifier),
        )
        return {row["path"]: row["inode"] for row in cur}

    def placed_identifiers(self, source: str, collection: str) -> set[str]:
        """Every show with at least one placed file in the collection."""
        cur = self.conn.execute(
            """
            SELECT DISTINCT identifier FROM library_placements
            WHERE source_name=? AND collection_name=?
            """,
            (source, collection),
        )
        return {row["identifier"] for row in cur}

    def owner_of(self, source: str, collection: str, path: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT identifier FROM library_placements
            WHERE source_name=? AND collection_name=? AND path=?
            """,
            (source, collection, path),
        ).fetchone()
        return row["identifier"] if row else None

    def add_placement(
        self, source: str, collection: str, identifier: str, path: str, inode: int
    ) -> None:
        """Record a placed file (re-recording the same owner updates the inode). Another
        show claiming a recorded path raises IntegrityError (one owner per path)."""
        if self.owner_of(source, collection, path) == identifier:
            self.conn.execute(
                """
                UPDATE library_placements SET inode=?
                WHERE source_name=? AND collection_name=? AND path=?
                """,
                (inode, source, collection, path),
            )
            return
        self.conn.execute(
            """
            INSERT INTO library_placements
                (source_name, collection_name, identifier, path, inode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, collection, identifier, path, inode),
        )

    def remove_placement(self, source: str, collection: str, path: str) -> None:
        self.conn.execute(
            """
            DELETE FROM library_placements
            WHERE source_name=? AND collection_name=? AND path=?
            """,
            (source, collection, path),
        )

    # --- show folders ---

    def set_folder(
        self, source: str, collection: str, identifier: str, folder: str, *, separated: bool
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO library_folders
                (source_name, collection_name, identifier, folder, separated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_name, collection_name, identifier) DO UPDATE SET
                folder=excluded.folder, separated=excluded.separated
            """,
            (source, collection, identifier, folder, int(separated)),
        )

    def delete_folder(self, source: str, collection: str, identifier: str) -> None:
        self.conn.execute(
            """
            DELETE FROM library_folders
            WHERE source_name=? AND collection_name=? AND identifier=?
            """,
            (source, collection, identifier),
        )

    def folders(self, source: str, collection: str) -> dict[str, str]:
        cur = self.conn.execute(
            """
            SELECT identifier, folder FROM library_folders
            WHERE source_name=? AND collection_name=?
            """,
            (source, collection),
        )
        return {row["identifier"]: row["folder"] for row in cur}

    def list_separated(self, source: str, collection: str) -> list[tuple[str, str]]:
        """(identifier, folder) of every separation in effect, by folder."""
        cur = self.conn.execute(
            """
            SELECT identifier, folder FROM library_folders
            WHERE source_name=? AND collection_name=? AND separated=1
            ORDER BY folder, identifier
            """,
            (source, collection),
        )
        return [(row["identifier"], row["folder"]) for row in cur]

    # --- files shakedown did not place ---

    def add_unplaced(self, source: str, collection: str, path: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO library_unplaced (source_name, collection_name, path)
            VALUES (?, ?, ?)
            """,
            (source, collection, path),
        )

    def remove_unplaced(self, source: str, collection: str, path: str) -> None:
        self.conn.execute(
            "DELETE FROM library_unplaced WHERE source_name=? AND collection_name=? AND path=?",
            (source, collection, path),
        )

    def list_unplaced(self, source: str, collection: str) -> list[str]:
        cur = self.conn.execute(
            """
            SELECT path FROM library_unplaced
            WHERE source_name=? AND collection_name=? ORDER BY path
            """,
            (source, collection),
        )
        return [row["path"] for row in cur]

    # --- one-time adoption of pre-existing links ---

    def is_adopted(self, source: str, collection: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM library_adoption WHERE source_name=? AND collection_name=?",
            (source, collection),
        ).fetchone()
        return row is not None

    def mark_adopted(self, source: str, collection: str, at: datetime) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO library_adoption (source_name, collection_name, adopted_at)
            VALUES (?, ?, ?)
            """,
            (source, collection, _iso(at)),
        )


@dataclass(frozen=True)
class PendingShow:
    """A show covered by a handoff notice not yet delivered (§spec:durable-handoff)."""

    identifier: str
    since: datetime
    last_reason: str


class PendingHandoffRepo:
    """Per collection, the shows covered by handoff notices not yet delivered
    (§spec:durable-handoff): resent with the collection's next notice until one is
    delivered, and shown in `status` meanwhile."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list(self, source: str, collection: str) -> list[PendingShow]:
        cur = self.conn.execute(
            """
            SELECT identifier, since, last_reason FROM handoff_pending
            WHERE source_name=? AND collection_name=? ORDER BY identifier
            """,
            (source, collection),
        )
        return [
            PendingShow(row["identifier"], datetime.fromisoformat(row["since"]),
                        row["last_reason"])
            for row in cur
        ]

    def mark_undelivered(
        self, source: str, collection: str, identifiers: Iterable[str], reason: str,
        at: datetime,
    ) -> None:
        """Record a notice that was not delivered: its shows join the list (a show
        already pending keeps its first time), and the whole list takes the reason."""
        with transaction(self.conn):
            self.conn.executemany(
                """
                INSERT INTO handoff_pending
                    (source_name, collection_name, identifier, since, last_reason)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source_name, collection_name, identifier) DO NOTHING
                """,
                [(source, collection, i, _iso(at), reason) for i in identifiers],
            )
            self.conn.execute(
                """
                UPDATE handoff_pending SET last_reason=?
                WHERE source_name=? AND collection_name=?
                """,
                (reason, source, collection),
            )

    def clear(self, source: str, collection: str, identifiers: Iterable[str]) -> None:
        """Remove shows from the list: a notice covering them was delivered, or they
        left the library."""
        self.conn.executemany(
            """
            DELETE FROM handoff_pending
            WHERE source_name=? AND collection_name=? AND identifier=?
            """,
            [(source, collection, i) for i in identifiers],
        )

    def drop(self, source: str, collection: str) -> None:
        """Forget the collection's whole list (its handoff was removed from config)."""
        self.conn.execute(
            "DELETE FROM handoff_pending WHERE source_name=? AND collection_name=?",
            (source, collection),
        )
