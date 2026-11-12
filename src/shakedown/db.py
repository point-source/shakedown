"""SQLite schema and connection helpers. Co-located with the archive (PRD §6)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 10

# The schema version that added `rescheduled_syncs`: a skipped sync records its entry
# only into a database already at this version or later (§spec:rescheduled-syncs).
RESCHEDULED_SINCE_VERSION = 9

# The schema version that added `run_activity`: a run that takes no writer lock
# (`verify`) records its progress only into a database already at this version or
# later — it never migrates (§spec:run-activity, §spec:state).
ACTIVITY_SINCE_VERSION = 10

# How long any connection waits on another writer's SQLite lock before giving up.
BUSY_TIMEOUT_MS = 5000

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
        -- when the show was first found missing from a complete listing
        -- (§spec:item-lifecycle)
        disappeared_at     TEXT,
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

# Tag filling (§spec:tag-filling), kept beside — never inside — the recorded manifest,
# which stays the source's word and the change-detection key (§spec:sync-identity).
_TAG_SCHEMA = [
    # One row per file shakedown changed: its checksum after tagging (what `verify
    # --deep` compares against) and the tag values shakedown wrote that are still its
    # own (JSON object), the only values it may later rewrite.
    """
    CREATE TABLE IF NOT EXISTS tag_files (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        file_name        TEXT NOT NULL,
        post_tag_md5     TEXT NOT NULL,
        written          TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (source_name, collection_name, identifier, file_name)
    )
    """,
    # Fingerprint of the tag values a show was last filled with: a sync revisits a
    # show only when its computed values no longer match, so a tagged show's files are
    # never re-read.
    """
    CREATE TABLE IF NOT EXISTS tag_shows (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        identifier       TEXT NOT NULL,
        fingerprint      TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name, identifier)
    )
    """,
]
_SCHEMA += _TAG_SCHEMA

# Per collection whose sync was skipped because another run held the writer lock, the
# time of its oldest skip (§spec:rescheduled-syncs). The primary key makes "one entry
# per collection" a database invariant.
_RESCHEDULE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS rescheduled_syncs (
        source_name      TEXT NOT NULL,
        collection_name  TEXT NOT NULL,
        since            TEXT NOT NULL,
        PRIMARY KEY (source_name, collection_name)
    )
    """,
]
_SCHEMA += _RESCHEDULE_SCHEMA

# Per run and collection, its live progress and result (§spec:run-activity): one row
# per collection a run works on. History of its own — trimmed to 30 days without
# touching item, run, or placement records (§spec:state). No foreign key to `runs`:
# `run_id` is null for kinds without a run record (restage, verify, reconcile).
_ACTIVITY_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS run_activity (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name             TEXT NOT NULL,
        collection_name         TEXT NOT NULL,
        kind                    TEXT NOT NULL,
        started_by_serve        INTEGER NOT NULL DEFAULT 0,
        run_id                  INTEGER,
        started_at              TEXT NOT NULL,
        finished_at             TEXT,
        last_heartbeat_at       TEXT NOT NULL,
        last_progress_at        TEXT NOT NULL,
        state                   TEXT NOT NULL,
        result                  TEXT,
        end_reason              TEXT,
        phase                   TEXT,
        items_listed            INTEGER,
        items_to_look_up        INTEGER,
        items_looked_up         INTEGER,
        items_to_fetch          INTEGER,
        items_done              INTEGER,
        items_failed            INTEGER,
        current_items           TEXT NOT NULL DEFAULT '[]',
        bytes_downloaded        INTEGER,
        bytes_to_fetch          INTEGER,
        speed_bytes_per_second  REAL,
        eta_seconds             INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS run_activity_live_idx
        ON run_activity (state, finished_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS run_activity_collection_idx
        ON run_activity (source_name, collection_name, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS run_activity_run_idx ON run_activity (run_id)
    """,
]
_SCHEMA += _ACTIVITY_SCHEMA


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and lazily create) the state DB at db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    # Collections sync in parallel (max_concurrent_collections), so multiple
    # connections may contend for the single writer lock — including during the
    # WAL switch and migration below. Set the busy timeout FIRST so every later
    # statement waits instead of failing immediately with SQLITE_BUSY.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def open_unmigrated(db_path: Path) -> sqlite3.Connection:
    """Open an existing state DB as it is: no WAL switch, no migration, and never
    created when missing (raises sqlite3.OperationalError instead). For a skipped
    sync, which records its entry without the writer lock while a run of an older
    release may still be writing (§spec:rescheduled-syncs)."""
    conn = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def connect_reader(db_path: Path) -> sqlite3.Connection:
    """Open the state DB for a command that takes no writer lock (`status`, `item
    show`, `sync --dry-run`, `verify` without `--reconform`, `validate`, serve's
    `GET /status` and `/metrics`). Only writers holding the writer lock
    migrate: a reader that migrated under a run of an older release would make that
    run refuse the database the next time it opens it. Readers therefore tolerate an
    older schema. A missing database is created at the current schema: no run has
    written one yet."""
    if not db_path.exists():
        return connect(db_path)
    return open_unmigrated(db_path)


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
    (§spec:single-show-runs), the tag-filling records (§spec:tag-filling), and drop
    the skip-the-loser collision columns, superseded by clash separation
    (§spec:library-ownership).

    The quality rule (§spec:quality-selection) adds each show's not-playable reason,
    and clears every stored change signal once: the first sync after the upgrade then
    looks every show up and applies the new rule, even on an incremental collection
    (§spec:incremental-discovery).

    Each show records the date it was first found missing upstream
    (§spec:item-lifecycle). Shows already missing before the upgrade get none: the
    only candidate, `last_verified_at`, is rewritten by every reconcile, so it is not
    reliably that date."""
    for stmt in _PLACEMENT_SCHEMA + _HANDOFF_SCHEMA + _TAG_SCHEMA:
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
    if not _has_column(conn, "items", "disappeared_at"):
        conn.execute("ALTER TABLE items ADD COLUMN disappeared_at TEXT")
    conn.execute("UPDATE items SET change_signal = NULL")


def _migrate_to_7(conn: sqlite3.Connection) -> None:
    """v6 → v7: the quality rule changes (§spec:quality-selection: a lossless copy whose
    tracks the source lists under different format names counts as one copy, and the
    library stages only the uploader's own non-audio files), so every stored change
    signal is cleared once, as v6 did: the first sync after the upgrade looks every
    show up and applies the new rule, even on an incremental collection
    (§spec:incremental-discovery)."""
    conn.execute("UPDATE items SET change_signal = NULL")


def _migrate_to_8(conn: sqlite3.Connection) -> None:
    """v7 → v8: the quality rule changes (§spec:quality-selection: the library no longer
    stages archive files or system and editor leftovers, whoever provided them), so every
    stored change signal is cleared once, as v7 did: the first sync after the upgrade
    looks every show up and takes those files out of the library, even on an
    incremental collection (§spec:incremental-discovery)."""
    conn.execute("UPDATE items SET change_signal = NULL")


def _migrate_to_9(conn: sqlite3.Connection) -> None:
    """v8 → v9: add the rescheduled-sync record (§spec:state, §spec:rescheduled-syncs).
    Additive: it starts empty, and nothing else changes."""
    for stmt in _RESCHEDULE_SCHEMA:
        conn.execute(stmt)


def _migrate_to_10(conn: sqlite3.Connection) -> None:
    """v9 → v10, the one migration of the dashboard increment (§spec:state): add the
    activity entries (§spec:run-activity). Additive: it starts empty, and nothing else
    changes. Later parts of the increment (network activity, growth snapshots) add
    their tables here too — the increment moves the schema forward exactly once."""
    for stmt in _ACTIVITY_SCHEMA:
        conn.execute(stmt)


# Version-gated, forward-only upgrade steps keyed by the version they produce.
_MIGRATIONS = {
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
    5: _migrate_to_5,
    6: _migrate_to_6,
    7: _migrate_to_7,
    8: _migrate_to_8,
    9: _migrate_to_9,
    10: _migrate_to_10,
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
