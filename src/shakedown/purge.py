"""Removing a collection (§spec:collection-removal): the collections the state
database still holds files for that the config file no longer names, and the
`purge` run that deletes their files.

A purge deletes what the placement records say Shakedown put in the library
(§spec:library-ownership), and with `everything` the collection's archive folder
and its shows' records. It runs under the writer lock, because it deletes what a
running sync may be writing.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from shakedown import stopping
from shakedown.activity import ActivityKind, ActivityRecorder, ActivityRepo
from shakedown.config import Config, ConfigError, load
from shakedown.db import connect, open_read_only, transaction
from shakedown.lock import run_exclusive
from shakedown.models import OperationStatus, OperationType
from shakedown.state import (
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    PlacementRepo,
    RescheduledRepo,
)
from shakedown.trigger import announce_opened, announce_started

log = logging.getLogger(__name__)

Pair = tuple[str, str]


class PurgeRefused(Exception):
    """A purge that may not start: `configured` (the file names the collection) or
    `not_held` (the state database holds nothing for it)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def configured_pairs(config: Config) -> set[Pair]:
    """Every (source, collection) the config file names."""
    return {(s.name, c.name) for s in config.sources for c in s.collections}


def held_collections(conn: sqlite3.Connection) -> set[Pair]:
    """Every (source, collection) the state database holds shows or library
    placements for."""
    cur = conn.execute(
        """
        SELECT source_name, collection_name FROM items
        UNION
        SELECT source_name, collection_name FROM library_placements
        """
    )
    return {(row[0], row[1]) for row in cur}


def is_held(conn: sqlite3.Connection, source: str, collection: str) -> bool:
    return (source, collection) in held_collections(conn)


def unconfigured(conn: sqlite3.Connection, configured: Iterable[Pair]) -> list[dict[str, Any]]:
    """`{source, collection, shows, archive_bytes, library_bytes}` for every held
    collection not in ``configured``. `shows` and `archive_bytes` cover the shows with
    an archive copy (the set `status` sums); `library_bytes` covers those of them the
    library still holds, so it reads 0 once a library delete has run."""
    answer = []
    for source, collection in sorted(held_collections(conn) - set(configured)):
        row = conn.execute(
            """
            SELECT COUNT(*) AS shows,
                   COALESCE(SUM(archive_bytes), 0) AS archive_bytes,
                   COALESCE(SUM(CASE WHEN EXISTS (
                       SELECT 1 FROM library_placements p
                       WHERE p.source_name = i.source_name
                         AND p.collection_name = i.collection_name
                         AND p.identifier = i.identifier
                   ) THEN library_bytes ELSE 0 END), 0) AS library_bytes
            FROM items i
            WHERE source_name=? AND collection_name=? AND archive_path IS NOT NULL
            """,
            (source, collection),
        ).fetchone()
        answer.append({
            "source": source,
            "collection": collection,
            "shows": int(row["shows"]),
            "archive_bytes": int(row["archive_bytes"]),
            "library_bytes": int(row["library_bytes"]),
        })
    return answer


def check_purgeable(config: Config, source: str, collection: str) -> None:
    """Raise PurgeRefused unless ``config`` leaves the collection out and the state
    database holds files for it. Reads only."""
    if (source, collection) in configured_pairs(config):
        raise PurgeRefused("configured", _configured_message(source, collection))
    state_db = config.state_db
    assert state_db is not None  # filled in by Config validation
    held = False
    if state_db.exists():  # no database yet: nothing is held
        with closing(open_read_only(state_db)) as conn:
            held = is_held(conn, source, collection)
    if not held:
        raise PurgeRefused("not_held", f"nothing is held for {source}/{collection}")


def _configured_message(source: str, collection: str) -> str:
    return f"{source}/{collection} is in the config file; remove it first"


def _one_folder(name: str) -> bool:
    """A name that is exactly one folder below its parent, so a delete aimed at it
    can never reach the archive or library root."""
    return name not in ("", ".", "..") and "/" not in name


def run_purge(
    config: Config, config_path: Path, source: str, collection: str, *, everything: bool
) -> int:
    """The `purge` run: delete the collection's library files, and with
    ``everything`` its archive folder and shows' records. Exit 1 when another run
    holds the lock, the run is refused, or a delete failed."""
    return run_exclusive(
        config, lambda: _run_purge(config, config_path, source, collection, everything)
    )


def _run_purge(
    config: Config, config_path: Path, source: str, collection: str, everything: bool
) -> int:
    state_db = config.state_db
    assert state_db is not None  # filled in by Config validation
    with closing(connect(state_db)) as conn, ActivityRecorder(state_db) as recorder:
        outcomes = OperationOutcomeRepo(conn)
        progress = recorder.open_entry(source, collection, ActivityKind.PURGE)
        # A purge has no phases or show counts to report.
        progress.phase(None)
        outcome = outcomes.start(OperationType.PURGE, source, collection, datetime.now())
        # The run history shows this outcome's errors (§spec:run-history).
        progress.outcome_id = outcome.id
        announce_started()
        announce_opened(None, outcome.id, source, collection)

        deletes = _Deletes(config, conn, source, collection)
        refusal = _refusal(config_path, source, collection, conn)
        if refusal is not None:
            log.error("%s", refusal)
            deletes.errors.append(refusal)
            status = OperationStatus.FAILED_BEFORE_COMPLETION
        else:
            deletes.library()
            if everything:
                deletes.archive()
            # Nothing is owed to a collection that is gone (§spec:collection-removal).
            RescheduledRepo(conn).clear(source, collection)
            PendingHandoffRepo(conn).drop(source, collection)
            if stopping.requested():
                status = OperationStatus.STOPPED
            elif deletes.errors:
                status = OperationStatus.COMPLETED_WITH_ITEM_ISSUES
            else:
                status = OperationStatus.COMPLETED
        outcomes.finish(
            outcome, status, datetime.now(),
            completed_work={"scope": "everything" if everything else "library",
                            "bytes_freed": deletes.bytes_freed,
                            "files_removed": deletes.files_removed,
                            "shows_removed": deletes.shows_removed},
            errors=deletes.errors,
        )
        if progress.activity_id is not None:
            ActivityRepo(conn).record_freed(progress.activity_id, deletes.bytes_freed)
        recorder.end(progress, status)
        log.info("deleted %d files of %s/%s; %d bytes freed", deletes.files_removed,
                 source, collection, deletes.bytes_freed)
        return 0 if status == OperationStatus.COMPLETED else 1


def _refusal(
    config_path: Path, source: str, collection: str, conn: sqlite3.Connection
) -> str | None:
    """Why the purge may not delete, decided under the lock from the file as it is
    now: a collection added back since the request keeps every file."""
    if not (_one_folder(source) and _one_folder(collection)):
        return f"{source!r}/{collection!r} is not one folder name; nothing deleted"
    try:
        current = load(config_path)
    except ConfigError as e:
        return f"the config file cannot be read, so nothing was deleted: {e}"
    if (source, collection) in configured_pairs(current):
        return _configured_message(source, collection)
    if not is_held(conn, source, collection):
        return f"nothing is held for {source}/{collection}"
    return None


class _Deletes:
    """One purge's deletes, the space they freed, and what could not be deleted."""

    def __init__(self, config: Config, conn: sqlite3.Connection, source: str,
                 collection: str) -> None:
        self.conn, self.source, self.collection = conn, source, collection
        self.library_base = config.library_root / source / collection
        self.archive_base = config.archive_root / source / collection
        self.placements = PlacementRepo(conn)
        self.bytes_freed = 0
        self.files_removed = 0
        self.shows_removed = 0
        self.errors: list[str] = []

    def library(self) -> None:
        """Delete every file the placement records say Shakedown placed, then the
        folders that leaves empty, up to and including the collection's library
        folder. A file found at a placed path that is not the one placed is someone
        else's: it stays and is recorded as not placed by Shakedown."""
        rows = self.conn.execute(
            """
            SELECT identifier, path, inode FROM library_placements
            WHERE source_name=? AND collection_name=? ORDER BY identifier, path
            """,
            (self.source, self.collection),
        ).fetchall()
        by_show: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_show.setdefault(row["identifier"], []).append(row)
        # Every folder a placement or a show's folder record names is swept, so a
        # show whose files were already gone (by hand, or a stopped purge) leaves
        # no empty folder behind.
        touched = {(self.library_base / row["path"]).parent for row in rows}
        touched |= {self.library_base / folder
                    for folder in self.placements.folders(self.source, self.collection).values()}
        for placed in by_show.values():
            if stopping.requested():
                return
            with transaction(self.conn):
                for row in placed:
                    self._unplace(row["path"], row["inode"])
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM library_folders WHERE source_name=? AND collection_name=?",
                (self.source, self.collection),
            )
        touched.add(self.library_base)
        for folder in sorted(touched, key=lambda p: len(p.parts), reverse=True):
            current = folder
            while current.is_relative_to(self.library_base):
                try:
                    current.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    break  # holds someone's file, or one that could not be deleted
                current = current.parent

    def _unplace(self, rel: str, inode: int) -> None:
        path = self.library_base / rel
        try:
            if path.lstat().st_ino != inode:
                self.placements.remove_placement(self.source, self.collection, rel)
                self.placements.add_unplaced(self.source, self.collection, rel)
                return
        except FileNotFoundError:
            pass  # already gone: only the record is left to drop
        else:
            if not self._unlink(path):
                return  # stays recorded, so a later purge retries it
        self.placements.remove_placement(self.source, self.collection, rel)

    def archive(self) -> None:
        """Delete each show's archive folder and, once it is gone, the show's
        records; then what else the collection's archive folder holds. A show whose
        folder could not be deleted keeps its records, so the collection stays
        listed and a later purge retries it."""
        items = ItemRepo(self.conn)
        shows = [item.identifier for item in items.list_for_collection(
            self.source, self.collection)]
        kept = 0
        for identifier in shows:
            if stopping.requested():
                return
            if not _one_folder(identifier):
                self.errors.append(f"{identifier!r} is not one folder name; kept")
                kept += 1
                continue
            if not self._delete_tree(self.archive_base / identifier):
                kept += 1
                continue
            with transaction(self.conn):
                items.delete(self.source, self.collection, identifier)
                self.conn.execute(
                    "DELETE FROM drift WHERE source_name=? AND collection_name=?"
                    " AND identifier=?",
                    (self.source, self.collection, identifier),
                )
            self.shows_removed += 1
        if kept or not self._delete_tree(self.archive_base):
            return
        with transaction(self.conn):
            for table in ("library_unplaced", "library_adoption"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE source_name=? AND collection_name=?",
                    (self.source, self.collection),
                )

    def _delete_tree(self, root: Path) -> bool:
        """Delete a folder and everything in it, never following a link out of it.
        True when nothing of it is left."""
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            return self._unlink(root)
        if not root.exists():
            return True
        complete = True

        def unreadable(e: OSError) -> None:
            nonlocal complete
            complete = False
            self.errors.append(f"{e.filename}: could not delete: {e.strerror or e}")

        for folder, dirs, files in os.walk(root, topdown=False, onerror=unreadable):
            for name in files:
                complete &= self._unlink(Path(folder, name))
            for name in dirs:
                complete &= self._rmdir(Path(folder, name))
        return self._rmdir(root) and complete

    def _rmdir(self, folder: Path) -> bool:
        if folder.is_symlink():
            return self._unlink(folder)
        try:
            folder.rmdir()
        except FileNotFoundError:
            return True
        except OSError:
            return False  # still holds what could not be deleted, reported already
        return True

    def _unlink(self, path: Path) -> bool:
        """Delete one file; count its size as freed when it was its last link."""
        try:
            info = os.lstat(path)
            os.unlink(path)
        except FileNotFoundError:
            return True
        except OSError as e:
            self.errors.append(f"{path}: could not delete: {e.strerror or e}")
            return False
        self.files_removed += 1
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            self.bytes_freed += info.st_size
        return True
