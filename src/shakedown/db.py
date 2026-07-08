"""SQLite schema and connection helpers. Co-located with the archive (PRD §6)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

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
        stale              INTEGER NOT NULL DEFAULT 0
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
]


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


# Version-gated, forward-only upgrade steps keyed by the version they produce.
_MIGRATIONS = {
    2: _migrate_to_2,
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
