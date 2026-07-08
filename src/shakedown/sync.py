"""Sync orchestrator: discover → plan → fetch → stage → notify → record. PRD §9.

CRITICAL INVARIANT: this module never hashes on-disk bytes. Item-presence and
change-detection are decided by manifest-vs-manifest comparison against the
state DB (PRD §5). The only on-disk hashing in the codebase lives in verify.py
under --deep.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect, transaction
from shakedown.models import Item, ItemStatus
from shakedown.notify import SyncCompletePayload, fire_complete
from shakedown.plugins import registry
from shakedown.plugins.base import FetchResult, ItemDescriptor, SourcePlugin
from shakedown.staging import stage_item, staging_dir_for, unstage_item
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
    stale: bool = False  # source enumeration failed; existing items left untouched
    # Items fetched and staged this run, for the sync.complete batch payload
    # (SPEC §spec:handoff). Each entry: {identifier, archive_path, staging_path}.
    staged: list[dict[str, str]] = field(default_factory=list)


def run_sync(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    dry_run: bool = False,
) -> int:
    """Top-level sync entrypoint. Returns process exit code.

    Collections sync in bounded parallel, capped by the global
    `max_concurrent_collections` (SPEC §spec:configuration, §spec:sync-workflow).
    Each collection opens its own DB connection and is otherwise independent, so
    one collection's failure degrades only that collection's run.
    """
    work: list[tuple[SourceConfig, CollectionConfig, SourcePlugin]] = []
    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        plugin = registry.for_source(source)
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue
            work.append((source, collection, plugin))

    if not work:
        return 0

    # Initialize the state DB once, up front. The one-time WAL switch and schema
    # migration need brief exclusive access; doing it here keeps concurrent
    # collection workers from racing on it (each then opens an already-WAL,
    # already-migrated DB). SPEC §spec:sync-workflow.
    connect(config.state_db).close()  # type: ignore[arg-type]

    overall_failed = 0
    max_workers = min(config.max_concurrent_collections, len(work))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_sync_collection, config, source, collection, plugin, dry_run=dry_run):
                (source, collection)
            for source, collection, plugin in work
        }
        for fut in as_completed(futures):
            source, collection = futures[fut]
            try:
                stats = fut.result()
            except Exception:
                log.exception("sync failed for %s/%s", source.name, collection.name)
                overall_failed += 1
                continue
            if stats.stale:
                log.warning("%s/%s: STALE (source enumeration failed)", source.name, collection.name)
            else:
                log.info(
                    "%s/%s: discovered=%d new=%d updated=%d failed=%d bytes=%d",
                    source.name, collection.name,
                    stats.discovered, stats.new, stats.updated, stats.failed,
                    stats.bytes_downloaded,
                )
            if stats.failed > 0 or stats.stale:
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
    try:
        for desc in plugin.discover(collection):
            descriptors.append(desc)
            seen_identifiers.add(desc.identifier)
    except Exception as e:
        # Source enumeration failed (unreachable or permanently-gone source). Flag
        # the collection stale so `status` reports it, and leave every existing item
        # untouched — a failed enumeration is not evidence any item disappeared
        # (§spec:failure-behavior). We do not run plan/fetch/stage this run.
        log.warning("[%s/%s] source enumeration failed: %s", source.name, collection.name, e)
        stats.stale = True
        stats.errors.append(f"source enumeration failed: {e}")
        finished = datetime.now()
        if not dry_run:
            _fire_notifications(config, source, collection, stats, started, finished)
        if run is not None:
            run.stale = True
            run.errors = stats.errors
            run.finished_at = finished
            runs.finish(run)
        return stats
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
                _record_disappeared(items, config, source.name, collection, p.existing)

    # Phase 5: notify. Fire before recording so any delivery failure is captured
    # in the run's errors (visible in `status`) but never aborts the sync.
    finished = datetime.now()
    _fire_notifications(config, source, collection, stats, started, finished)

    # Phase 6: record run
    if run is not None:
        run.items_discovered = stats.discovered
        run.items_new = stats.new
        run.items_updated = stats.updated
        run.items_failed = stats.failed
        run.bytes_downloaded = stats.bytes_downloaded
        run.errors = stats.errors
        run.finished_at = finished
        runs.finish(run)

    return stats


def _fire_notifications(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    stats: CollectionSyncStats,
    started: datetime,
    finished: datetime,
) -> None:
    """Fire the once-per-(collection, run) handoff and failure notifications
    (SPEC §spec:handoff).

    ``sync.complete`` fires when the run staged at least one item. Delivery is
    best-effort: a failure is appended to ``stats.errors`` (recorded in the run,
    surfaced by `status`) and never raises.
    """
    staging_root = str(config.library_root / source.name / collection.name)
    run_obj = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "items_new": stats.new,
        "items_updated": stats.updated,
        "items_failed": stats.failed,
        "bytes_downloaded": stats.bytes_downloaded,
    }

    if stats.staged:
        err = fire_complete(
            collection,
            SyncCompletePayload(
                source=source.name,
                collection=collection.name,
                staging_root=staging_root,
                run=run_obj,
                staged=stats.staged,
            ),
        )
        if err:
            stats.errors.append(err)


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
            ItemStatus.PRUNED,
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

            # Collect for the once-per-run sync.complete batch payload; the handoff
            # fires once after the run, not per item (SPEC §spec:handoff).
            staging_dir = staging_dir_for(
                config, collection, source.name, new_item.source_metadata, new_item.archive_path  # type: ignore[arg-type]
            )
            stats.staged.append(
                {
                    "identifier": desc.identifier,
                    "archive_path": str(new_item.archive_path),
                    "staging_path": str(staging_dir),
                }
            )


@dataclass
class _FetchOutcome:
    success: bool
    archive_path: Path
    bytes_downloaded: int = 0
    error: str | None = None


# Bounded core-owned retries for transient fetch faults (checksum mismatch,
# truncated download, rate limits). SPEC §spec:failure-behavior.
_MAX_FETCH_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff for the Nth (1-based) attempt, capped."""
    return min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


def _within_archive(archive_root: Path, candidate: Path) -> bool:
    """True iff `candidate` stays inside `archive_root` (no traversal/absolute escape).

    Uses lexical normalization (not `resolve()`) so a symlink already inside the
    archive tree isn't rejected; the check exists to stop a remote identifier from
    escaping the tree, not to police the operator's own layout.
    """
    root = os.path.normpath(archive_root)
    cand = os.path.normpath(candidate)
    return cand == root or cand.startswith(root + os.sep)


def _fetch_with_retries(
    plugin: SourcePlugin,
    desc: ItemDescriptor,
    tmp_dir: Path,
    collection: CollectionConfig,
    *,
    max_attempts: int = _MAX_FETCH_ATTEMPTS,
) -> FetchResult:
    """Call plugin.fetch with bounded retries and backoff (SPEC §spec:failure-behavior).

    Retries are core-owned, not delegated to the plugin's library: a transient
    fault (checksum mismatch, truncated download, rate limit) is retried up to
    `max_attempts` times. When the source supplies a `Retry-After`, we honor it
    in place of exponential backoff. The temp dir is wiped before each attempt so
    a partial write from the previous try never contaminates the next.
    """
    for attempt in range(1, max_attempts + 1):
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        result = plugin.fetch(
            desc, tmp_dir, collection.format_filters, collection.exclude_filters
        )
        if result.success or not result.retriable or attempt == max_attempts:
            return result

        delay = result.retry_after if result.retry_after is not None else _backoff_seconds(attempt)
        log.warning(
            "fetch attempt %d/%d for %s failed (%s); retrying in %.1fs",
            attempt, max_attempts, desc.identifier, result.error, delay,
        )
        time.sleep(delay)
    raise AssertionError("unreachable: max_attempts must be >= 1")


def _fetch_one(
    plugin: SourcePlugin,
    target: PlannedItem,
    archive_root: Path,
    collection: CollectionConfig,
) -> _FetchOutcome:
    """Core-owned atomic fetch into <archive_root>/<identifier>/ (SPEC §spec:sync-workflow).

    The plugin downloads into a fresh, core-owned temp dir and never touches the
    final archive location. The core verifies the manifest files landed, then
    atomically renames the temp dir into place — so no plugin can commit partial
    state to the archive. A failure leaves only the temp dir (swept on the next
    run) and the prior archive bytes untouched (SPEC §spec:failure-behavior:
    archive durability across fetch failures).
    """
    assert target.descriptor is not None
    desc = target.descriptor
    dest = archive_root / desc.identifier

    # Trust boundary: the identifier comes from a remote source and drives
    # os.rename / shutil.rmtree / plugin downloads below. Reject anything that
    # would escape the archive tree (traversal via `..`, an absolute path, or a
    # separator) before it touches the filesystem.
    if not _within_archive(archive_root, dest):
        log.warning("[%s] rejecting unsafe identifier: %r", desc.identifier, desc.identifier)
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            error=f"unsafe identifier escapes archive tree: {desc.identifier!r}",
        )

    tmp_dir = archive_root / f".tmp-{desc.identifier}"
    stale_dir = archive_root / f".stale-{desc.identifier}"

    # Sweep any stale swap-aside dir from a prior crashed promotion; the temp dir
    # is swept per-attempt inside _fetch_with_retries.
    if stale_dir.exists():
        shutil.rmtree(stale_dir)

    fetch_result = _fetch_with_retries(plugin, desc, tmp_dir, collection)
    if not fetch_result.success:
        # Leave tmp_dir for next-run cleanup; the archive copy (if any) is untouched.
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            bytes_downloaded=fetch_result.bytes_downloaded,
            error=fetch_result.error,
        )

    # Core-owned completeness guard before promotion: route the existence-only
    # check through the plugin's own verify() contract (no byte hashing —
    # SPEC §spec:sync-identity) so a plugin can't commit partial state even if it
    # over-reports success.
    verified = plugin.verify(desc, tmp_dir)
    if not verified.ok:
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            bytes_downloaded=fetch_result.bytes_downloaded,
            error=f"missing after fetch: {verified.missing_files[:5]}",
        )

    _promote_atomically(tmp_dir, dest, stale_dir)
    return _FetchOutcome(
        success=True,
        archive_path=dest,
        bytes_downloaded=fetch_result.bytes_downloaded,
    )


def _promote_atomically(tmp_dir: Path, dest: Path, stale_dir: Path) -> None:
    """Atomically swap a fully-fetched temp dir into the archive location.

    Rename any prior dest aside, install the new dest, then discard the
    staged-aside copy. If the install rename fails, restore the prior bytes.
    Renames within one filesystem are atomic, so dest is either the old tree or
    the new one, never a partial mix (SPEC §spec:sync-workflow).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    had_prior = dest.exists()
    if had_prior:
        os.rename(dest, stale_dir)
    try:
        os.rename(tmp_dir, dest)
    except Exception:
        if had_prior and stale_dir.exists() and not dest.exists():
            os.rename(stale_dir, dest)
        raise
    if stale_dir.exists():
        shutil.rmtree(stale_dir)


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
    config: Config,
    source_name: str,
    collection: CollectionConfig,
    existing: Item,
) -> None:
    """Handle an item that vanished from the source's enumeration (§spec:item-lifecycle).

    Default: retain the local files and flag the item `disappeared` — an upstream
    takedown never deletes the user's copy. With `prune_disappeared: true`: delete
    the archive files and staging links but *retain the DB record* marked `pruned`,
    so `shakedown status` can still report the takedown.
    """
    if collection.prune_disappeared:
        if existing.archive_path and existing.archive_path.exists():
            shutil.rmtree(existing.archive_path)
        unstage_item(config, collection, source_name, existing)
        existing.status = ItemStatus.PRUNED
        existing.archive_path = None
        existing.last_verified_at = datetime.now()
        items.upsert(existing)
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
