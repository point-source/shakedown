"""Sync orchestrator: discover → plan → fetch → stage → notify → record. PRD §9.

CRITICAL INVARIANT: this module never hashes on-disk bytes. Item-presence and
change-detection are decided by manifest-vs-manifest comparison against the
state DB (PRD §5). The only on-disk hashing in the codebase lives in verify.py
under --deep.
"""
from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect, transaction
from shakedown.models import Item, ItemStatus
from shakedown.notify import HandoffPayload
from shakedown.notify import fire as fire_handoff
from shakedown.plugins import registry
from shakedown.plugins.base import ItemDescriptor, SourcePlugin
from shakedown.staging import stage_item, staging_dir_for
from shakedown.state import ItemRepo, RunRepo

log = logging.getLogger(__name__)


class PlanAction(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    CHANGED_UPSTREAM = "changed-upstream"
    UNAVAILABLE = "unavailable"
    DISAPPEARED = "disappeared"


@dataclass
class PlannedItem:
    descriptor: ItemDescriptor | None  # None for disappeared (no longer in source)
    existing: Item | None
    action: PlanAction


@dataclass
class CollectionSyncStats:
    discovered: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


def run_sync(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    dry_run: bool = False,
) -> int:
    """Top-level sync entrypoint. Returns process exit code."""
    overall_failed = 0
    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        plugin = registry.for_source(source)
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue
            try:
                stats = _sync_collection(
                    config, source, collection, plugin, dry_run=dry_run
                )
                log.info(
                    "%s/%s: discovered=%d new=%d updated=%d failed=%d bytes=%d",
                    source.name, collection.name,
                    stats.discovered, stats.new, stats.updated, stats.failed,
                    stats.bytes_downloaded,
                )
                if stats.failed > 0:
                    overall_failed += 1
            except Exception:
                log.exception("sync failed for %s/%s", source.name, collection.name)
                overall_failed += 1
    return 0 if overall_failed == 0 else 1


def _sync_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    *,
    dry_run: bool,
) -> CollectionSyncStats:
    """Sync one (source, collection) end-to-end."""
    started = datetime.now()
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)

    run = runs.start(source.name, collection.name, started) if not dry_run else None

    stats = CollectionSyncStats()
    staged_paths: set[Path] = set()

    # Phase 1: discover
    descriptors: list[ItemDescriptor] = []
    seen_identifiers: set[str] = set()
    log.info("[%s/%s] discovering...", source.name, collection.name)
    for desc in plugin.discover(collection):
        descriptors.append(desc)
        seen_identifiers.add(desc.identifier)
    stats.discovered = len(descriptors)

    # Phase 2: plan
    plan = _build_plan(items, source.name, collection.name, descriptors, seen_identifiers)
    _log_plan(source.name, collection.name, plan)

    if dry_run:
        return stats

    # Phase 3 + 4: fetch + stage
    fetch_targets = [p for p in plan if p.action in (PlanAction.NEW, PlanAction.CHANGED_UPSTREAM)]
    _execute_fetch_and_stage(
        config, source, collection, plugin, items, fetch_targets, stats, staged_paths
    )

    # Phase 4b: re-stage UNCHANGED items so missing hardlinks (PRD §13) are restored.
    # stage_item is idempotent — already-linked files share an inode and are skipped.
    for p in plan:
        if (
            p.action == PlanAction.UNCHANGED
            and p.existing is not None
            and p.existing.recorded_manifest is not None
        ):
            stage_result = stage_item(
                config, collection, source.name, p.existing, p.existing.recorded_manifest,
                staged_paths=staged_paths,
            )
            for c in stage_result.collisions:
                log.warning("[%s/%s] staging collision: %s", source.name, collection.name, c)
                stats.errors.append(f"staging collision: {c}")
                stats.failed += 1

    # Persist new state for unavailable / disappeared / unchanged in one shot
    with transaction(conn):
        for p in plan:
            if p.action == PlanAction.UNAVAILABLE and p.descriptor is not None:
                _record_unavailable(items, source.name, collection.name, p.descriptor)
            elif p.action == PlanAction.DISAPPEARED and p.existing is not None:
                _record_disappeared(items, collection, p.existing)

    # Phase 6: record run
    if run is not None:
        run.items_discovered = stats.discovered
        run.items_new = stats.new
        run.items_updated = stats.updated
        run.items_failed = stats.failed
        run.bytes_downloaded = stats.bytes_downloaded
        run.errors = stats.errors
        run.finished_at = datetime.now()
        runs.finish(run)

    return stats


def _files_missing(item: Item) -> bool:
    """Existence-only check across the recorded manifest. Never hashes bytes (PRD §5)."""
    if item.archive_path is None or item.recorded_manifest is None:
        return True
    return any(
        not (item.archive_path / mf.name).is_file()
        for mf in item.recorded_manifest.files
    )


def _build_plan(
    items: ItemRepo,
    source_name: str,
    collection_name: str,
    descriptors: list[ItemDescriptor],
    seen_identifiers: set[str],
) -> list[PlannedItem]:
    """Manifest-vs-manifest classification for every item.

    PRD §9 step 2: never hash bytes. Comparison is between the source's *current*
    manifest and the manifest we recorded at fetch time. PRD §5 also requires that
    the expected file paths still exist on disk — manifest-equality alone isn't
    enough; vanished files must trigger a re-fetch.
    """
    plan: list[PlannedItem] = []
    by_identifier: dict[str, Item] = {
        item.identifier: item for item in items.list_for_collection(source_name, collection_name)
    }
    for desc in descriptors:
        existing = by_identifier.get(desc.identifier)
        if desc.is_restricted:
            plan.append(PlannedItem(desc, existing, PlanAction.UNAVAILABLE))
            continue
        if existing is None:
            plan.append(PlannedItem(desc, None, PlanAction.NEW))
            continue
        if existing.recorded_manifest is None:
            plan.append(PlannedItem(desc, existing, PlanAction.NEW))
            continue
        if existing.recorded_manifest == desc.manifest:
            if _files_missing(existing):
                plan.append(PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM))
            else:
                plan.append(PlannedItem(desc, existing, PlanAction.UNCHANGED))
        else:
            plan.append(PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM))

    # Disappeared: in DB but not in this discover.
    for identifier, existing in by_identifier.items():
        if identifier not in seen_identifiers and existing.status not in (
            ItemStatus.DISAPPEARED,
            ItemStatus.UNAVAILABLE,
        ):
            plan.append(PlannedItem(None, existing, PlanAction.DISAPPEARED))
    return plan


def _log_plan(source_name: str, collection_name: str, plan: list[PlannedItem]) -> None:
    counts: dict[str, int] = {}
    for p in plan:
        counts[p.action.value] = counts.get(p.action.value, 0) + 1
    log.info("[%s/%s] plan: %s", source_name, collection_name, counts or "(empty)")


def _execute_fetch_and_stage(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    items: ItemRepo,
    targets: list[PlannedItem],
    stats: CollectionSyncStats,
    staged_paths: set[Path] | None = None,
) -> None:
    """Concurrent fetch (capped by max_concurrent_downloads); sequential staging.

    Each item is its own atomic unit: fetch into temp, persist row + create
    hardlinks once the bytes are on disk and the manifest is recorded.
    """
    if not targets:
        return

    archive_root = config.archive_root / source.name / collection.name
    archive_root.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as pool:
        futures = {
            pool.submit(_fetch_one, plugin, target, archive_root, collection): target
            for target in targets
        }
        for fut in as_completed(futures):
            target = futures[fut]
            try:
                outcome = fut.result()
            except Exception as e:
                log.exception("fetch crash for %s", target.descriptor and target.descriptor.identifier)
                stats.failed += 1
                stats.errors.append(f"{target.descriptor.identifier if target.descriptor else '?'}: {e}")
                continue

            assert target.descriptor is not None  # NEW/CHANGED_UPSTREAM always have a descriptor
            desc = target.descriptor

            if not outcome.success:
                stats.failed += 1
                stats.errors.append(f"{desc.identifier}: {outcome.error}")
                _record_failed(items, source.name, collection, desc, outcome.error)
                continue

            stats.bytes_downloaded += outcome.bytes_downloaded
            if target.action == PlanAction.NEW:
                stats.new += 1
            else:
                stats.updated += 1

            new_item = _record_complete(
                items, source.name, collection, desc, outcome.archive_path
            )
            stage_result = stage_item(
                config, collection, source.name, new_item, desc.manifest,
                staged_paths=staged_paths,
            )
            if stage_result.collisions:
                for c in stage_result.collisions:
                    log.warning("[%s/%s] staging collision: %s", source.name, collection.name, c)
                    stats.errors.append(f"staging collision: {c}")
                    stats.failed += 1

            staging_dir = staging_dir_for(
                config, collection, source.name, new_item.source_metadata, new_item.archive_path  # type: ignore[arg-type]
            )
            fire_handoff(
                collection,
                HandoffPayload(
                    event="item_complete",
                    source=source.name,
                    collection=collection.name,
                    item_identifier=desc.identifier,
                    archive_path=str(new_item.archive_path),
                    staging_path=str(staging_dir),
                    source_metadata_excerpt={
                        k: desc.metadata.get(k)
                        for k in ("title", "creator", "date", "year", "venue")
                        if desc.metadata.get(k) is not None
                    },
                ),
            )


@dataclass
class _FetchOutcome:
    success: bool
    archive_path: Path
    bytes_downloaded: int = 0
    error: str | None = None


def _fetch_one(
    plugin: SourcePlugin,
    target: PlannedItem,
    archive_root: Path,
    collection: CollectionConfig,
) -> _FetchOutcome:
    """Plugin-mediated fetch into <archive_root>/<identifier>/. Atomic per PRD §9 step 3.

    The plugin owns the swap-in of new bytes onto an existing dest. On failure, the
    plugin must leave the prior archive directory intact (PRD §5: archive layer is
    durable; only an explicit user wipe should remove archive bytes).
    """
    assert target.descriptor is not None
    desc = target.descriptor
    dest = archive_root / desc.identifier

    fetch_result = plugin.fetch(
        desc, dest, collection.format_filters, collection.exclude_filters
    )
    return _FetchOutcome(
        success=fetch_result.success,
        archive_path=dest,
        bytes_downloaded=fetch_result.bytes_downloaded,
        error=fetch_result.error,
    )


def _record_complete(
    items: ItemRepo,
    source_name: str,
    collection: CollectionConfig,
    desc: ItemDescriptor,
    archive_path: Path,
) -> Item:
    now = datetime.now()
    item = Item(
        source_name=source_name,
        collection_name=collection.name,
        identifier=desc.identifier,
        status=ItemStatus.COMPLETE,
        archive_path=archive_path,
        discovered_at=now,
        downloaded_at=now,
        last_verified_at=now,
        source_metadata=desc.metadata,
        recorded_manifest=desc.manifest,
    )
    items.upsert(item)
    return item


def _record_unavailable(
    items: ItemRepo,
    source_name: str,
    collection_name: str,
    desc: ItemDescriptor,
) -> None:
    existing = items.get(source_name, collection_name, desc.identifier)
    item = Item(
        source_name=source_name,
        collection_name=collection_name,
        identifier=desc.identifier,
        status=ItemStatus.UNAVAILABLE,
        archive_path=existing.archive_path if existing else None,
        discovered_at=existing.discovered_at if existing else datetime.now(),
        downloaded_at=existing.downloaded_at if existing else None,
        last_verified_at=datetime.now(),
        restriction_reason=desc.restriction_reason,
        source_metadata=desc.metadata,
        recorded_manifest=existing.recorded_manifest if existing else None,
    )
    items.upsert(item)


def _record_failed(
    items: ItemRepo,
    source_name: str,
    collection: CollectionConfig,
    desc: ItemDescriptor,
    error: str | None,
) -> None:
    existing = items.get(source_name, collection.name, desc.identifier)
    item = Item(
        source_name=source_name,
        collection_name=collection.name,
        identifier=desc.identifier,
        status=ItemStatus.FAILED,
        archive_path=existing.archive_path if existing else None,
        discovered_at=existing.discovered_at if existing else datetime.now(),
        downloaded_at=existing.downloaded_at if existing else None,
        last_verified_at=datetime.now(),
        restriction_reason=error,
        source_metadata=desc.metadata,
        recorded_manifest=existing.recorded_manifest if existing else None,
    )
    items.upsert(item)


def _record_disappeared(
    items: ItemRepo,
    collection: CollectionConfig,
    existing: Item,
) -> None:
    """PRD §9: disappeared items are NOT deleted unless prune_disappeared: true."""
    if collection.prune_disappeared:
        if existing.archive_path and existing.archive_path.exists():
            shutil.rmtree(existing.archive_path)
        items.delete(existing.source_name, existing.collection_name, existing.identifier)
        return
    existing.status = ItemStatus.DISAPPEARED
    existing.last_verified_at = datetime.now()
    items.upsert(existing)


# --- single-item operations called from `shakedown item ...` ---

def refetch_item(config: Config, identifier: str) -> int:
    """Force re-fetch of a single item by identifier across all configured collections."""
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    for source in config.sources:
        plugin = registry.for_source(source)
        for collection in source.collections:
            existing = items.get(source.name, collection.name, identifier)
            if existing is None:
                continue
            log.info("refetch: %s/%s/%s", source.name, collection.name, identifier)
            for desc in plugin.discover(collection):
                if desc.identifier != identifier:
                    continue
                target = PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM)
                stats = CollectionSyncStats()
                _execute_fetch_and_stage(
                    config, source, collection, plugin, items, [target], stats
                )
                return 0 if stats.failed == 0 else 1
    log.warning("identifier %s not found in any configured collection", identifier)
    return 1


def forget_item(config: Config, identifier: str) -> None:
    """Drop an item from the DB (does not delete files)."""
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    for source in config.sources:
        for collection in source.collections:
            if items.get(source.name, collection.name, identifier):
                items.delete(source.name, collection.name, identifier)
                log.info("forgot %s/%s/%s", source.name, collection.name, identifier)
