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
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect, transaction
from shakedown.models import Item, ItemStatus, Run
from shakedown.notify import (
    SyncCompletePayload,
    SyncFailedPayload,
    fire_complete,
    fire_failure,
)
from shakedown.plugins import registry
from shakedown.plugins.base import FetchResult, ItemDescriptor, SourcePlugin
from shakedown.staging import stage_item, staging_dir_for, unstage_item
from shakedown.state import ItemRepo, RunRepo

log = logging.getLogger(__name__)


class SourceBudget:
    """Shared per-source concurrency ceiling (SPEC §spec:source-budget).

    One instance per source, shared across all of that source's collections and
    both its discovery and download workers, so the number of simultaneous
    connections Shakedown opens to the upstream host never exceeds the configured
    budget — no matter how many collections run concurrently.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._sem = threading.BoundedSemaphore(size)

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()


def _source_budget_size(config: Config, source: SourceConfig) -> int:
    return source.max_concurrent_requests or config.max_concurrent_downloads


def _describe_one(
    plugin: SourcePlugin,
    identifier: str,
    collection: CollectionConfig,
    budget: SourceBudget,
) -> ItemDescriptor | None:
    with budget.slot():
        return plugin.describe_item(identifier, collection)


def _stream_descriptors(
    plugin: SourcePlugin,
    collection: CollectionConfig,
    budget: SourceBudget,
    enumerated: set[str],
) -> Iterator[ItemDescriptor]:
    """Yield item descriptors as they resolve, fanning per-item metadata resolution
    across the shared per-source budget (SPEC §spec:discovery-pipeline).

    Sources exposing `enumerate_items()` get concurrent `describe_item()` resolution
    bounded by the source budget, yielded via `as_completed` so a consumer can act on
    each item the moment it resolves (the overlap lever). Sources without the
    enumerate/describe split fall back to the serial `discover()` stream. A transient
    per-item fault yields None from `describe_item` and is dropped, exactly as the serial
    path skipped a failed metadata fetch. Materializing the id list or a `describe_item`
    call raising propagates to the consumer, which treats it as an enumeration failure.

    Every identifier the enumeration returned is added to ``enumerated`` — including one
    whose `describe_item` transiently failed and was dropped. That set, not the resolved
    descriptors, is the prune signal (SPEC §spec:prune-safety): an enumerated-but-unresolved
    item is still present in the collection, so it must never be read as `disappeared`. In
    the `discover()` fallback there is no enumerate/describe split, so ``enumerated``
    collects the resolved identifiers instead.
    """
    identifiers = plugin.enumerate_items(collection)
    if identifiers is None:
        for desc in plugin.discover(collection):
            enumerated.add(desc.identifier)
            yield desc
        return
    ids = list(identifiers)  # materialize the enumeration (may raise -> stale)
    enumerated.update(ids)
    if not ids:
        return
    with ThreadPoolExecutor(max_workers=min(budget.size, len(ids))) as pool:
        futures = [
            pool.submit(_describe_one, plugin, identifier, collection, budget)
            for identifier in ids
        ]
        for fut in as_completed(futures):
            desc = fut.result()
            if desc is not None:
                yield desc


def _discover_descriptors(
    plugin: SourcePlugin,
    collection: CollectionConfig,
    budget: SourceBudget,
) -> tuple[list[ItemDescriptor], set[str]]:
    """Materialize the full enumeration for the dry-run path (Discover + Plan, no fetch).
    The real run streams the same descriptors (`_stream_descriptors`) into the fetch
    pipeline instead of buffering them here.

    Returns the resolved descriptors plus the set of *enumerated* identifiers — the prune
    signal. The latter is a superset of the described identifiers: an item enumeration
    listed but whose description failed is present for prune purposes even though it
    resolved to no descriptor (SPEC §spec:prune-safety).
    """
    enumerated: set[str] = set()
    descriptors = list(_stream_descriptors(plugin, collection, budget, enumerated))
    return descriptors, enumerated


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

    # One shared concurrency budget per distinct source, so all of that source's
    # collections (and their discovery + download workers) draw on a single
    # bounded pool — the hard ceiling on simultaneous upstream connections holds
    # even with max_concurrent_collections > 1 (SPEC §spec:source-budget).
    budgets: dict[str, SourceBudget] = {}
    for source, _collection, _plugin in work:
        if source.name not in budgets:
            budgets[source.name] = SourceBudget(_source_budget_size(config, source))

    overall_failed = 0
    max_workers = min(config.max_concurrent_collections, len(work))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _sync_collection, config, source, collection, plugin,
                dry_run=dry_run, budget=budgets[source.name],
            ): (source, collection)
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


def _finish_run(runs: RunRepo, run: Run, stats: CollectionSyncStats, finished: datetime) -> None:
    """Persist the run record from the accumulated stats (SPEC §spec:sync-workflow, step 6)."""
    run.items_discovered = stats.discovered
    run.items_new = stats.new
    run.items_updated = stats.updated
    run.items_failed = stats.failed
    run.bytes_downloaded = stats.bytes_downloaded
    run.stale = stats.stale
    run.errors = stats.errors
    run.finished_at = finished
    runs.finish(run)


def _sync_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    *,
    dry_run: bool,
    budget: SourceBudget,
) -> CollectionSyncStats:
    """Sync one (source, collection) end-to-end.

    Discover, classify, and fetch overlap: each discovered item is classified as it
    resolves and, if `new`/`changed-upstream`, handed to the fetch stage immediately —
    so downloads begin while discovery is still enumerating (SPEC §spec:discovery-pipeline).
    The prune/`disappeared` decision needs the *complete* enumeration, so it is held
    back as a post-enumeration barrier that runs only after discovery finishes
    successfully; a failed enumeration marks the collection stale and prunes nothing.
    """
    started = datetime.now()
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)

    run = runs.start(source.name, collection.name, started) if not dry_run else None

    stats = CollectionSyncStats()
    staged_paths: set[Path] = set()

    log.info("[%s/%s] discovering...", source.name, collection.name)

    if dry_run:
        # Dry-run performs Discover and Plan only (SPEC §spec:sync-workflow): no fetch,
        # no overlap to exploit, no writes. Materialize the enumeration and report the
        # plan without touching disk or DB.
        try:
            descriptors, enumerated_identifiers = _discover_descriptors(
                plugin, collection, budget
            )
        except Exception as e:
            log.warning("[%s/%s] source enumeration failed: %s", source.name, collection.name, e)
            stats.stale = True
            stats.errors.append(f"source enumeration failed: {e}")
            return stats
        stats.discovered = len(descriptors)
        plan = _build_plan(
            items, source.name, collection.name, descriptors, enumerated_identifiers,
            prune_disappeared=collection.prune_disappeared,
        )
        _log_plan(source.name, collection.name, plan)
        return stats

    # Snapshot the pre-run DB state once. It drives per-item classification (is this
    # `new`/`changed-upstream`/`unchanged`?) and, after enumeration, the prune barrier
    # (which recorded items were not seen this run?). A fetch upserts rows during the
    # run, but the disappeared decision is computed against this pre-run snapshot, so a
    # freshly fetched item is never mistaken for a vanished one.
    by_identifier = {
        item.identifier: item
        for item in items.list_for_collection(source.name, collection.name)
    }

    # Phases 1-4, pipelined: discover -> classify -> fetch + stage, overlapping downloads
    # with the remaining enumeration (SPEC §spec:discovery-pipeline, overlap lever).
    enumerated_identifiers, deferred, enum_error = _pipelined_discover_and_fetch(
        config, source, collection, plugin, items, stats, staged_paths, budget, by_identifier,
    )
    stats.discovered = len(enumerated_identifiers)

    if enum_error is not None:
        # Enumeration failed (source unreachable or faulted partway). Flag the collection
        # stale so `status` reports it, and DO NOT run the prune barrier — a partial or
        # failed enumeration is never read as items having disappeared
        # (§spec:failure-behavior, §spec:discovery-pipeline). Any items already fetched
        # before the failure are valid and were recorded as they completed.
        log.warning(
            "[%s/%s] source enumeration failed: %s", source.name, collection.name, enum_error
        )
        stats.stale = True
        stats.errors.append(f"source enumeration failed: {enum_error}")
        finished = datetime.now()
        _fire_notifications(config, source, collection, stats, started, finished)
        if run is not None:
            _finish_run(runs, run, stats, finished)
        return stats

    # Post-enumeration barrier: the enumeration is complete and successful, so the
    # prune/`disappeared` set (in the DB but not returned by this enumeration) is now safe
    # to compute. Keyed on `enumerated_identifiers` — an item enumeration listed but whose
    # description failed counts as present and is never pruned (SPEC §spec:prune-safety).
    disappeared = _disappeared_plan(
        by_identifier, enumerated_identifiers, collection.prune_disappeared
    )
    _log_pipeline_plan(source.name, collection.name, stats, deferred, disappeared)

    # Re-stage UNCHANGED items so missing hardlinks (PRD §13) are restored.
    # stage_item is idempotent — already-linked files share an inode and are skipped.
    for p in deferred:
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

    # Persist new state for unavailable / disappeared in one shot.
    with transaction(conn):
        for p in deferred:
            if p.action == PlanAction.UNAVAILABLE and p.descriptor is not None:
                _record_unavailable(items, source.name, collection.name, p.descriptor)
        for p in disappeared:
            assert p.existing is not None
            _record_disappeared(items, config, source.name, collection, p.existing)

    # Phase 5: notify. Fire before recording so any delivery failure is captured
    # in the run's errors (visible in `status`) but never aborts the sync.
    finished = datetime.now()
    _fire_notifications(config, source, collection, stats, started, finished)

    # Phase 6: record run
    if run is not None:
        _finish_run(runs, run, stats, finished)

    return stats


def _pipelined_discover_and_fetch(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    items: ItemRepo,
    stats: CollectionSyncStats,
    staged_paths: set[Path],
    budget: SourceBudget,
    by_identifier: dict[str, Item],
) -> tuple[set[str], list[PlannedItem], Exception | None]:
    """Overlap discover → classify → fetch (SPEC §spec:discovery-pipeline, overlap lever).

    Each descriptor is classified the moment it resolves; a `new`/`changed-upstream`
    item is submitted to the fetch pool immediately, so its download begins while the
    rest of the collection is still being enumerated — both stages drawing from the
    shared per-source ``budget`` (SPEC §spec:source-budget). `unchanged`/`unavailable`
    items need no fetch and are deferred to the caller's post-enumeration barrier.

    Returns ``(enumerated, deferred, enum_error)``:

    - ``enumerated`` — every identifier the enumeration returned; the input to the
      prune/`disappeared` barrier, which the caller runs only when ``enum_error`` is None.
      Prune eligibility keys on absence from *this* set, never on absence from the
      described set: an item enumeration listed but whose `describe_item` failed this run
      is still present and must never be pruned or flagged `disappeared`
      (SPEC §spec:prune-safety). Such an item is left entirely untouched — not in
      ``deferred``, not fetched — so its DB row is reconsidered unchanged next run.
    - ``deferred`` — the UNCHANGED / UNAVAILABLE planned items.
    - ``enum_error`` — the exception if enumeration faulted partway (the collection goes
      stale and nothing is pruned), else None for a complete, successful enumeration.

    Downloads run concurrently, but their results (DB writes, staging, stats) are
    applied on this orchestrating thread only, so per-run counters stay exact without
    locking (SPEC §spec:discovery-pipeline: counts exact under concurrent producers and
    consumers).
    """
    archive_root = config.archive_root / source.name / collection.name
    enumerated: set[str] = set()
    deferred: list[PlannedItem] = []
    enum_error: Exception | None = None

    with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as fetch_pool:
        fetch_futures: dict[Future[_FetchOutcome], PlannedItem] = {}

        def dispatch(desc: ItemDescriptor) -> None:
            planned = _classify_one(desc, by_identifier)
            if planned.action in (PlanAction.NEW, PlanAction.CHANGED_UPSTREAM):
                archive_root.mkdir(parents=True, exist_ok=True)
                fut = fetch_pool.submit(
                    _fetch_one_budgeted, budget, plugin, planned, archive_root, collection
                )
                fetch_futures[fut] = planned
            else:
                deferred.append(planned)

        def on_skip(identifier: str, existing: Item) -> None:
            # Signal-unchanged: classify UNCHANGED without a metadata fetch. The identifier
            # is already in `enumerated` (the stream records every enumerated id), so the
            # prune barrier never mistakes it for disappeared; deferred here for the
            # caller's idempotent re-stage pass (§spec:incremental-discovery).
            deferred.append(PlannedItem(None, existing, PlanAction.UNCHANGED))

        descriptor_stream = (
            _stream_descriptors_incremental(
                plugin, collection, budget, by_identifier, on_skip, enumerated
            )
            if collection.incremental_discovery
            else _stream_descriptors(plugin, collection, budget, enumerated)
        )
        try:
            # Descriptors stream in as each resolves; dispatch classifies and fetches
            # each immediately, so downloads overlap the remaining enumeration.
            for desc in descriptor_stream:
                dispatch(desc)
        except Exception as e:  # any enumeration fault marks the collection stale
            enum_error = e

        # Drain every fetch already in flight. Those downloads are valid whether or not
        # the enumeration completed, and recording them keeps the archive and DB
        # consistent (SPEC §spec:discovery-pipeline: an item may be fetched and staged
        # before discovery completes).
        for fut in as_completed(fetch_futures):
            _process_fetch_result(
                fut, fetch_futures[fut], config, source, collection, items, stats, staged_paths
            )

    return enumerated, deferred, enum_error


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

    ``sync.complete`` fires when the run staged at least one item; ``sync.failed``
    fires when the run failed (source enumeration stale, or one or more items
    failed). Both are best-effort: a delivery failure is appended to
    ``stats.errors`` (recorded in the run, surfaced by `status`) and never raises.
    """
    run_errors = list(stats.errors)  # substantive errors, before any delivery attempt
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

    if stats.stale or stats.failed > 0:
        err = fire_failure(
            config.notifications,
            SyncFailedPayload(
                source=source.name,
                collection=collection.name,
                staging_root=staging_root,
                run=run_obj,
                errors=run_errors,
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


def _classify_one(desc: ItemDescriptor, by_identifier: dict[str, Item]) -> PlannedItem:
    """Classify a single descriptor by manifest-vs-manifest comparison (PRD §9 step 2).

    Never hashes bytes: the comparison is between the source's *current* manifest and
    the manifest recorded at fetch time. PRD §5 also requires the expected file paths
    still exist on disk — manifest-equality alone isn't enough; vanished files trigger
    a re-fetch. This decision needs only *this* item's state, so it is safe to make the
    moment the item is discovered, before the rest of the enumeration
    (SPEC §spec:discovery-pipeline).
    """
    existing = by_identifier.get(desc.identifier)
    if desc.is_restricted:
        return PlannedItem(desc, existing, PlanAction.UNAVAILABLE)
    if existing is None or existing.recorded_manifest is None:
        return PlannedItem(desc, existing, PlanAction.NEW)
    if existing.recorded_manifest == desc.manifest:
        if _files_missing(existing):
            return PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM)
        return PlannedItem(desc, existing, PlanAction.UNCHANGED)
    return PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM)


def _signal_unchanged(signal: str | None, existing: Item | None) -> bool:
    """True iff the source's cheap change signal proves this item is unchanged and
    still on disk, so the per-item metadata fetch can be skipped
    (§spec:incremental-discovery). A missing current signal, a missing stored
    signal, a mismatch, or vanished files all return False and fall through to the
    full manifest comparison — the skip is only ever a *skip*, never a substitute
    for the manifest source of truth (§spec:sync-identity)."""
    return (
        signal is not None
        and existing is not None
        and existing.change_signal is not None
        and existing.change_signal == signal
        and not _files_missing(existing)
    )


def _stream_descriptors_incremental(
    plugin: SourcePlugin,
    collection: CollectionConfig,
    budget: SourceBudget,
    by_identifier: dict[str, Item],
    on_skip: Callable[[str, Item], None],
    enumerated: set[str],
) -> Iterator[ItemDescriptor]:
    """Incremental-discovery variant of _stream_descriptors (§spec:incremental-discovery).

    Requests the cheap per-item change signal from the source's search. An item whose
    stored signal matches (and whose files are present) is reported UNCHANGED via
    on_skip WITHOUT the per-item metadata fetch — describe_item()/get_item() is never
    called for it. Every other item (signal changed, no stored signal, or vanished
    files) falls through to describe_item() and the full manifest comparison, exactly
    as today; the fresh signal is attached to its descriptor so a successful fetch
    persists it. Falls back to the non-incremental stream when the plugin exposes no
    signal (enumerate_with_signals returns None).
    """
    signals = plugin.enumerate_with_signals(collection)
    if signals is None:
        yield from _stream_descriptors(plugin, collection, budget, enumerated)
        return
    pairs = list(signals)  # materialize the enumeration (may raise -> stale)
    enumerated.update(identifier for identifier, _ in pairs)
    to_describe: list[tuple[str, str | None]] = []
    for identifier, signal in pairs:
        existing = by_identifier.get(identifier)
        if _signal_unchanged(signal, existing):
            assert existing is not None  # _signal_unchanged guarantees it
            on_skip(identifier, existing)
        else:
            to_describe.append((identifier, signal))
    if not to_describe:
        return
    with ThreadPoolExecutor(max_workers=min(budget.size, len(to_describe))) as pool:
        futures = {
            pool.submit(_describe_one, plugin, identifier, collection, budget): signal
            for identifier, signal in to_describe
        }
        for fut in as_completed(futures):
            desc = fut.result()
            if desc is not None:
                yield replace(desc, change_signal=futures[fut])


def _disappeared_plan(
    by_identifier: dict[str, Item],
    enumerated_identifiers: set[str],
    prune_disappeared: bool,
) -> list[PlannedItem]:
    """Items in the DB but absent from this (complete) enumeration — the prune/`disappeared`
    barrier. Callers MUST run this only after a complete, successful enumeration; a
    partial discovery is never read as items having disappeared
    (SPEC §spec:discovery-pipeline, §spec:failure-behavior).

    Eligibility keys on absence from ``enumerated_identifiers`` — every identifier the
    source *listed* — never on absence from the successfully-*described* set. An item the
    enumeration returned counts as present even when its `describe_item` failed this run,
    so a transient metadata fault can never masquerade as a takedown (SPEC §spec:prune-safety).

    Already-terminal states are skipped so a still-absent item isn't reprocessed every
    run — except that an item already flagged `disappeared` stays reachable for pruning
    once a collection opts into `prune_disappeared`, so the retain→prune transition takes
    effect on the next sync (§spec:item-lifecycle).
    """
    terminal = {ItemStatus.UNAVAILABLE, ItemStatus.PRUNED}
    if not prune_disappeared:
        terminal.add(ItemStatus.DISAPPEARED)
    return [
        PlannedItem(None, existing, PlanAction.DISAPPEARED)
        for identifier, existing in by_identifier.items()
        if identifier not in enumerated_identifiers and existing.status not in terminal
    ]


def _build_plan(
    items: ItemRepo,
    source_name: str,
    collection_name: str,
    descriptors: list[ItemDescriptor],
    enumerated_identifiers: set[str],
    *,
    prune_disappeared: bool = False,
) -> list[PlannedItem]:
    """Full plan (per-item classification + prune barrier) over a materialized
    enumeration. Used by the dry-run path, which reports what would happen without
    fetching; the real run pipelines the same classification (`_classify_one`) and
    prune barrier (`_disappeared_plan`) with the fetch stage.

    ``enumerated_identifiers`` — every identifier the source listed — feeds the prune
    barrier, so the dry-run reports the same prune-safe plan as the real path: an item
    whose description failed is never shown as `disappeared` (SPEC §spec:prune-safety).
    """
    by_identifier: dict[str, Item] = {
        item.identifier: item for item in items.list_for_collection(source_name, collection_name)
    }
    plan = [_classify_one(desc, by_identifier) for desc in descriptors]
    plan.extend(_disappeared_plan(by_identifier, enumerated_identifiers, prune_disappeared))
    return plan


def _log_plan(source_name: str, collection_name: str, plan: list[PlannedItem]) -> None:
    counts: dict[str, int] = {}
    for p in plan:
        counts[p.action.value] = counts.get(p.action.value, 0) + 1
    log.info("[%s/%s] plan: %s", source_name, collection_name, counts or "(empty)")


def _log_pipeline_plan(
    source_name: str,
    collection_name: str,
    stats: CollectionSyncStats,
    deferred: list[PlannedItem],
    disappeared: list[PlannedItem],
) -> None:
    """Post-run plan summary for the pipelined path, from the accumulated stats plus the
    deferred (unchanged/unavailable) and disappeared sets."""
    counts = {
        PlanAction.NEW.value: stats.new,
        PlanAction.CHANGED_UPSTREAM.value: stats.updated,
        PlanAction.UNCHANGED.value: sum(1 for p in deferred if p.action == PlanAction.UNCHANGED),
        PlanAction.UNAVAILABLE.value: sum(
            1 for p in deferred if p.action == PlanAction.UNAVAILABLE
        ),
        PlanAction.DISAPPEARED.value: len(disappeared),
        "failed": stats.failed,
    }
    log.info("[%s/%s] plan: %s", source_name, collection_name, counts)


def _execute_fetch_and_stage(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    items: ItemRepo,
    targets: list[PlannedItem],
    stats: CollectionSyncStats,
    staged_paths: set[Path] | None = None,
    budget: SourceBudget | None = None,
) -> None:
    """Concurrent fetch (capped by max_concurrent_downloads); sequential staging.

    Each item is its own atomic unit: fetch into temp, persist row + create
    hardlinks once the bytes are on disk and the manifest is recorded.

    When a per-source ``budget`` is supplied, each fetch acquires a slot from it
    around the whole ``_fetch_one`` call, so simultaneous connections to the
    source's upstream host never exceed the shared per-source ceiling — the pool's
    worker count is only the per-collection parallelism (SPEC §spec:source-budget).
    """
    if not targets:
        return

    archive_root = config.archive_root / source.name / collection.name
    archive_root.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as pool:
        futures = {
            pool.submit(
                _fetch_one_budgeted, budget, plugin, target, archive_root, collection
            ): target
            for target in targets
        }
        for fut in as_completed(futures):
            _process_fetch_result(
                fut, futures[fut], config, source, collection, items, stats, staged_paths
            )


def _process_fetch_result(
    fut: Future[_FetchOutcome],
    target: PlannedItem,
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    items: ItemRepo,
    stats: CollectionSyncStats,
    staged_paths: set[Path] | None,
) -> None:
    """Apply one completed fetch: record the row, stage hardlinks, accumulate stats.

    Called only on the orchestrating thread (never a fetch worker), so the DB writes
    and the per-run counters it mutates are serialized and exact even though the
    downloads themselves ran concurrently (SPEC §spec:discovery-pipeline).
    """
    try:
        outcome = fut.result()
    except Exception as e:
        log.exception("fetch crash for %s", target.descriptor and target.descriptor.identifier)
        stats.failed += 1
        stats.errors.append(f"{target.descriptor.identifier if target.descriptor else '?'}: {e}")
        return

    assert target.descriptor is not None  # NEW/CHANGED_UPSTREAM always have a descriptor
    desc = target.descriptor

    if not outcome.success:
        stats.failed += 1
        stats.errors.append(f"{desc.identifier}: {outcome.error}")
        _record_failed(items, source.name, collection, desc, outcome.error)
        return

    stats.bytes_downloaded += outcome.bytes_downloaded
    if target.action == PlanAction.NEW:
        stats.new += 1
    else:
        stats.updated += 1

    new_item = _record_complete(items, source.name, collection, desc, outcome.archive_path)
    stage_result = stage_item(
        config, collection, source.name, new_item, desc.manifest, staged_paths=staged_paths,
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

        # Clamp a source-supplied Retry-After to the backoff cap: the value is
        # upstream-controlled and a fetch worker holds a SourceBudget slot while it
        # sleeps, so an unbounded value must never pin slots for an attacker-chosen wait.
        delay = (
            _backoff_seconds(attempt)
            if result.retry_after is None
            else min(result.retry_after, _MAX_BACKOFF_SECONDS)
        )
        log.warning(
            "fetch attempt %d/%d for %s failed (%s); retrying in %.1fs",
            attempt, max_attempts, desc.identifier, result.error, delay,
        )
        time.sleep(delay)
    raise AssertionError("unreachable: max_attempts must be >= 1")


def _fetch_one_budgeted(
    budget: SourceBudget | None,
    plugin: SourcePlugin,
    target: PlannedItem,
    archive_root: Path,
    collection: CollectionConfig,
) -> _FetchOutcome:
    """Acquire a per-source budget slot around the whole fetch (SPEC §spec:source-budget).

    The slot is held for the entire ``_fetch_one`` call so it counts as one live
    upstream connection; with no budget (e.g. a single-item refetch) the fetch runs
    unbounded.
    """
    if budget is None:
        return _fetch_one(plugin, target, archive_root, collection)
    with budget.slot():
        return _fetch_one(plugin, target, archive_root, collection)


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

    # Trust boundary: manifest file names also come from the remote source and
    # are joined onto the temp dir by the plugin's fetch(). Reject any that would
    # escape the item directory *before* fetch runs — the write happens during
    # fetch, so the post-fetch verify() can't catch an escape (it resolves to the
    # same out-of-tree path). This guards every plugin, not just well-behaved ones.
    unsafe_names = [
        mf.name for mf in desc.manifest.files if not _within_archive(dest, dest / mf.name)
    ]
    if unsafe_names:
        log.warning("[%s] rejecting unsafe manifest file name(s): %r", desc.identifier, unsafe_names[:5])
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            error=f"unsafe file name(s) escape item directory: {unsafe_names[:5]}",
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
        change_signal=desc.change_signal,
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
