"""SQLite schema and connection helpers. Co-located with the archive (PRD §6)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 6

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
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
        change_signal      TEXT,
        -- why the show has no copy a common player reads (§spec:quality-selection)
        not_playable_reason TEXT,
        PRIMARY KEY (source_name, collection_name, identifier)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS items_status_idx
        ON items (source_name, collection_name, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name        TEXT NOT NULL,
        collection_name    TEXT NOT NULL,
        started_at         TEXT NOT NULL,
        finished_at        TEXT,
        items_discovered   INTEGER NOT NULL DEFAULT 0,
        items_new          INTEGER NOT NULL DEFAULT 0,
        items_updated      INTEGER NOT NULL DEFAULT 0,
        items_failed       INTEGER NOT NULL DEFAULT 0,
        bytes_downloaded   INTEGER NOT NULL DEFAULT 0,
        errors             TEXT NOT NULL DEFAULT '[]',
        stale              INTEGER NOT NULL DEFAULT 0,
        failure_notice_error TEXT,
        kind               TEXT NOT NULL DEFAULT 'sync'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runs_collection_idx
        ON runs (source_name, collection_name, started_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS drift (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        file_name        TEXT NOT NULL,
        observed_md5     TEXT,
        expected_md5     TEXT,
        observed_at      TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name, identifier, file_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_outcomes (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        operation            TEXT NOT NULL,
        status               TEXT NOT NULL,
        source_name          TEXT NOT NULL,
        collection_name      TEXT NOT NULL,
        started_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL,
        finished_at          TEXT,
        phase                TEXT,
        affected_item        TEXT,
        affected_path        TEXT,
        completed_work       TEXT NOT NULL DEFAULT '{}',
        preservation_context TEXT,
        deletion_context     TEXT,
        safe_next_action     TEXT,
        errors               TEXT NOT NULL DEFAULT '[]',
        resolved_at          TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS operation_outcomes_scope_idx
        ON operation_outcomes (
            source_name, collection_name, resolved_at, started_at DESC, id DESC
        )
    """,
]


# The library placement record (§spec:library-ownership, §spec:library-cleanup). Paths
# are relative to the collection's library folder (<library_root>/<source>/<collection>).
_PLACEMENT_SCHEMA = [
    # Every file shakedown placed in the library, with the inode it placed there: a
    # different file later found at that path is not shakedown's. The primary key on
    # the path makes "one owner per library path" a database invariant.
    """
    CREATE TABLE IF NOT EXISTS library_placements (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        path             TEXT NOT NULL,
        inode            INTEGER NOT NULL,
        PRIMARY KEY (source_name, collection_name, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS library_placements_show_idx
        ON library_placements (source_name, collection_name, identifier)
    """,
    # Each staged show's library folder, and whether it was separated (labelled with
    # its identifier) because another show renders the same folder.
    """
    CREATE TABLE IF NOT EXISTS library_folders (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        folder           TEXT NOT NULL,
        separated        INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (source_name, collection_name, identifier)
    )
    """,
    # Library files found where shakedown stages that it did not place; left alone and
    # reported by `status`.
    """
    CREATE TABLE IF NOT EXISTS library_unplaced (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        path             TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name, path)
    )
    """,
    # Collections whose existing library links were adopted into the record (once
    # after the upgrade, or after a database rebuilt from scratch).
    """
    CREATE TABLE IF NOT EXISTS library_adoption (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        adopted_at       TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name)
    )
    """,
]
_SCHEMA += _PLACEMENT_SCHEMA

# Shows covered by handoff notices not yet delivered (§spec:durable-handoff). `since` is
# when the show first went undelivered; `last_reason` is the newest failure, the same on
# every row of the collection.
_HANDOFF_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS handoff_pending (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        since            TEXT NOT NULL,
        last_reason      TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name, identifier)
    )
    """,
]
_SCHEMA += _HANDOFF_SCHEMA


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and lazily create) the state DB at db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    # Collections sync in parallel (max_concurrent_collections), so multiple
    # connections may contend for the single writer lock — including during the
    # WAL switch and migration below. Set the busy timeout FIRST so every later
    # statement waits instead of failing immediately with SQLITE_BUSY.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS brings a fresh DB fully up to date; for an
    # existing DB it is a no-op and the version-gated steps below apply the delta.
    for stmt in _SCHEMA:
        conn.execute(stmt)
    cur = conn.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        return

    current = row["version"]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported schema version: db={current} expected={SCHEMA_VERSION}"
        )
    while current < SCHEMA_VERSION:
        current += 1
        _MIGRATIONS[current](conn)
        conn.execute("UPDATE schema_version SET version=?", (current,))


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """v1 → v2: add runs.stale so `status` can report enumeration-failure staleness."""
    conn.execute("ALTER TABLE runs ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in cols)


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """v2 → v3: add items.change_signal for incremental discovery (§spec:incremental-discovery)."""
    if not _has_column(conn, "items", "change_signal"):
        conn.execute("ALTER TABLE items ADD COLUMN change_signal TEXT")


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """v3 → v4 added runs.collisions_dropped / runs.collision_paths for the skip-the-loser
    collision summary. Clash separation superseded it and v6 drops them, so there is
    nothing to add on the way through."""


def _migrate_to_5(conn: sqlite3.Connection) -> None:
    """v4 → v5: add operation_outcomes for durable recovery reporting."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operation_outcomes (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            operation            TEXT NOT NULL,
            status               TEXT NOT NULL,
            source_name          TEXT NOT NULL,
            collection_name      TEXT NOT NULL,
            started_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            finished_at          TEXT,
            phase                TEXT,
            affected_item        TEXT,
            affected_path        TEXT,
            completed_work       TEXT NOT NULL DEFAULT '{}',
            preservation_context TEXT,
            deletion_context     TEXT,
            safe_next_action     TEXT,
            errors               TEXT NOT NULL DEFAULT '[]',
            resolved_at          TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS operation_outcomes_scope_idx
            ON operation_outcomes (
                source_name, collection_name, resolved_at, started_at DESC, id DESC
            )
        """
    )


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    """v5 → v6, the one migration of the ownership, handoff, and discovery increment
    (§spec:state): add the library placement record, the pending-handoff record,
    each run's undelivered failure alert (§spec:durable-handoff) and its kind
    (§spec:single-show-runs), and drop the
    skip-the-loser collision columns, superseded by clash separation
    (§spec:library-ownership).

    The quality rule (§spec:quality-selection) adds each show's not-playable reason,
    and clears every stored change signal once: the first sync after the upgrade then
    looks every show up and applies the new rule, even on an incremental collection
    (§spec:incremental-discovery)."""
    for stmt in _PLACEMENT_SCHEMA + _HANDOFF_SCHEMA:
        conn.execute(stmt)
    if not _has_column(conn, "runs", "failure_notice_error"):
        conn.execute("ALTER TABLE runs ADD COLUMN failure_notice_error TEXT")
    if not _has_column(conn, "runs", "kind"):
        conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'sync'")
    for column in ("collisions_dropped", "collision_paths"):
        if _has_column(conn, "runs", column):
            conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
    if not _has_column(conn, "items", "not_playable_reason"):
        conn.execute("ALTER TABLE items ADD COLUMN not_playable_reason TEXT")
    conn.execute("UPDATE items SET change_signal = NULL")


# Version-gated, forward-only upgrade steps keyed by the version they produce.
_MIGRATIONS = {
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
    5: _migrate_to_5,
    6: _migrate_to_6,
}


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as a single SQLite transaction.

    Uses ``BEGIN IMMEDIATE`` so the write lock is acquired up front. A plain
    deferred ``BEGIN`` takes only a read lock and upgrades to a write on the first
    mutation; when two collection workers (max_concurrent_collections) both hold a
    read lock and try to upgrade, SQLite returns SQLITE_BUSY *immediately* to break
    the deadlock — ``busy_timeout`` cannot help a write-upgrade. Taking the write
    lock at BEGIN makes concurrent transactions serialize under ``busy_timeout``
    instead of failing with "database is locked".
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
