"""Removing a collection (§spec:collection-removal): the collections the state
database still holds files for that the config file no longer names."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from shakedown.config import Config

Pair = tuple[str, str]


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
