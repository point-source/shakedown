"""Rebuild the library staging tree from the DB. PRD §10.

Used after a `library_layout` change or after manually wiping `/data/library/`.
Never downloads anything — the archive layer is the source of truth.
"""
from __future__ import annotations

import logging
import shutil

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.staging import stage_item
from shakedown.state import ItemRepo

log = logging.getLogger(__name__)


def run_restage(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
) -> int:
    """Wipe and rebuild the library tree under each (source, collection) scope."""
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    failed = 0

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue

            scope = config.library_root / source.name / collection.name
            if scope.exists():
                shutil.rmtree(scope)

            linked = 0
            collisions = 0
            staged_paths: set = set()
            for item in items.list_for_collection(source.name, collection.name):
                if item.status != ItemStatus.COMPLETE or item.recorded_manifest is None:
                    continue
                result = stage_item(
                    config, collection, source.name, item, item.recorded_manifest,
                    staged_paths=staged_paths,
                )
                linked += result.linked
                collisions += len(result.collisions)
                for c in result.collisions:
                    log.warning("[%s/%s] %s", source.name, collection.name, c)
            log.info(
                "[%s/%s] restage: linked=%d collisions=%d",
                source.name, collection.name, linked, collisions,
            )
            if collisions:
                failed += 1
    return 0 if failed == 0 else 1
