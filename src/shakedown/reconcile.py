"""shakedown reconcile: rebuild state DB from archive tree + source manifests.

Disaster recovery / migration from a manual setup. PRD §10, §13. Never re-downloads —
falls back to source for the manifest, but trusts on-disk presence as the source of
truth for "do I have this?". Tolerates an unreachable source: items on disk are still
recorded, just without a recorded_manifest.
"""
from __future__ import annotations

import logging
from datetime import datetime

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect, transaction
from shakedown.models import Item, ItemStatus
from shakedown.plugins import registry
from shakedown.plugins.base import ItemDescriptor
from shakedown.state import ItemRepo

log = logging.getLogger(__name__)


def run_reconcile(config: Config) -> int:
    """For every (source, collection), walk the archive tree and rebuild item rows."""
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)

    for source in config.sources:
        plugin = registry.for_source(source)
        for collection in source.collections:
            _reconcile_collection(config, source, collection, plugin, items, conn)
    return 0


def _reconcile_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin,
    items: ItemRepo,
    conn,
) -> None:
    archive_root = config.archive_root / source.name / collection.name
    on_disk_ids: set[str] = set()
    if archive_root.is_dir():
        on_disk_ids = {
            p.name for p in archive_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }

    descriptors_by_id: dict[str, ItemDescriptor] = {}
    try:
        for desc in plugin.discover(collection):
            descriptors_by_id[desc.identifier] = desc
    except Exception:
        log.exception(
            "[%s/%s] source discover failed; reconciling on-disk items without manifests",
            source.name, collection.name,
        )

    log.info(
        "[%s/%s] reconciling %d on-disk items (%d also seen upstream)",
        source.name, collection.name, len(on_disk_ids),
        len(on_disk_ids & descriptors_by_id.keys()),
    )

    with transaction(conn):
        # 1. Drop DB rows for this collection that no longer exist on disk.
        #    This is what makes reconcile a true rebuild rather than an upsert pass.
        for existing in list(items.list_for_collection(source.name, collection.name)):
            if existing.identifier not in on_disk_ids:
                items.delete(source.name, collection.name, existing.identifier)

        # 2. Upsert every on-disk identifier, using the source manifest if available.
        for identifier in on_disk_ids:
            archive_path = archive_root / identifier
            existing = items.get(source.name, collection.name, identifier)
            desc = descriptors_by_id.get(identifier)
            now = datetime.now()

            if desc is not None:
                # Source-confirmed: COMPLETE with the source's current manifest.
                item = Item(
                    source_name=source.name,
                    collection_name=collection.name,
                    identifier=identifier,
                    status=ItemStatus.COMPLETE,
                    archive_path=archive_path,
                    discovered_at=existing.discovered_at if existing else now,
                    downloaded_at=existing.downloaded_at if existing else now,
                    last_verified_at=now,
                    source_metadata=desc.metadata,
                    recorded_manifest=desc.manifest,
                )
            else:
                # On disk but not in current source enumeration. Two sub-cases:
                #   (a) source is unreachable → preserve any prior manifest/metadata
                #       so subsequent syncs still classify correctly when source returns;
                #   (b) item disappeared upstream → status flips to DISAPPEARED per
                #       PRD §6 enum. We can't tell (a) and (b) apart from one reconcile;
                #       prefer DISAPPEARED only when we previously had a row, COMPLETE
                #       otherwise (no row means no prior knowledge of upstream presence).
                status = ItemStatus.DISAPPEARED if existing else ItemStatus.COMPLETE
                item = Item(
                    source_name=source.name,
                    collection_name=collection.name,
                    identifier=identifier,
                    status=status,
                    archive_path=archive_path,
                    discovered_at=existing.discovered_at if existing else now,
                    downloaded_at=existing.downloaded_at if existing else now,
                    last_verified_at=now,
                    source_metadata=existing.source_metadata if existing else {},
                    recorded_manifest=existing.recorded_manifest if existing else None,
                )
            items.upsert(item)

    log.info(
        "[%s/%s] reconcile recorded %d items", source.name, collection.name, len(on_disk_ids)
    )
