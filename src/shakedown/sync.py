"""Sync orchestrator: discover → plan → fetch → stage → notify → record. PRD §9.

CRITICAL INVARIANT: this module never hashes on-disk bytes. Item-presence and
change-detection are decided by manifest-vs-manifest comparison against the
state DB (PRD §5). The only on-disk hashing in the codebase lives in verify.py
under --deep.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import time
from collections.abc import Collection, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, Config, ConfigError, SourceConfig
from shakedown.config import load as load_config
from shakedown.db import connect, connect_reader, transaction
from shakedown.discovery import (
    ItemDescriptor,
    ItemDetails,
    LookupFailure,
    SourceBudget,
    describe,
    look_up_rows,
    source_budgets,
    stream_items,
)
from shakedown.filesystem import FilesystemError, ensure_hardlinkable, is_within
from shakedown.lock import run_exclusive
from shakedown.models import (
    Item,
    ItemStatus,
    Manifest,
    ManifestFile,
    OperationOutcome,
    OperationStatus,
    OperationType,
    Run,
    RunKind,
)
from shakedown.notify import (
    SyncCompletePayload,
    SyncFailedPayload,
    fire_complete,
    fire_failure,
)
from shakedown.placement import (
    LibraryPlacement,
    PlacementReport,
    has_archive_copy,
    log_separations,
)
from shakedown.plugins import registry
from shakedown.plugins.base import FetchResult, ListedItem, SourcePlugin
from shakedown.reschedule import record_skipped
from shakedown.staging import (
    METADATA_SIDECAR,
    manifest_reserves_sidecar_name,
    write_sidecar,
)
from shakedown.state import (
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    RescheduledRepo,
    RunRepo,
    TagRecord,
    TagRepo,
)
from shakedown.tagging import (
    Desired,
    TagPlanner,
    TagWriteError,
    fill_collection_tags,
    fill_file,
    fingerprint,
    recover_interrupted_tagging,
    tag_failure,
)
from shakedown.trigger import announce_started, launched_by_serve

log = logging.getLogger(__name__)


# Recorded as an item's restriction_reason when the source lists no audio for it
# (operator-approved copy).
NO_FILES_REASON = "source lists no audio files"

# The command that retries a run of each kind, for its outcome's next safe action.
_RERUN = {
    RunKind.SYNC: "sync",
    RunKind.REFETCH: "item refetch",
    RunKind.RECONFORM: "verify --deep --reconform",
}


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
    # The descriptor's files to download; None downloads all of them. The rest are
    # kept from the archive copy (§spec:quality-selection: an upgrade downloads only
    # what is missing).
    fetch: tuple[ManifestFile, ...] | None = None
    # Each taggable file's computed tags, on a `fill_tags` collection: the downloaded
    # files are tagged before the show enters the archive (§spec:tag-filling).
    tags: Desired | None = None


def plan_files(
    selected: Manifest, existing: Item | None, *, refetch: bool = False
) -> tuple[Manifest, tuple[ManifestFile, ...]]:
    """The manifest to record for a show and the files to download for it.

    Selected files the archive already holds — same name, size, and checksum as
    recorded, and on disk (a stat, never a hash) — are kept; ``refetch`` downloads
    every selected file instead. Recorded files the rule no longer selects stay in the
    archive copy, recorded as archive-only, while they are on disk: a selection change
    never deletes archive audio (§spec:quality-selection).
    """
    if (
        not selected.files
        or existing is None
        or existing.recorded_manifest is None
        or existing.archive_path is None
    ):
        return selected, selected.files
    folder = existing.archive_path
    recorded = existing.recorded_manifest.fingerprint()
    chosen = {f.name for f in selected.files}
    fetch = tuple(
        f
        for f in selected.files
        if refetch
        or (f.name, f.size, f.md5) not in recorded
        or not (folder / f.name).is_file()
    )
    retained = tuple(
        replace(f, library=False)
        for f in existing.recorded_manifest.files
        if f.name not in chosen and (folder / f.name).is_file()
    )
    merged = Manifest(files=tuple(sorted(selected.files + retained, key=lambda f: f.name)))
    return merged, fetch


def _entries(manifest: Manifest) -> frozenset[ManifestFile]:
    """A manifest's files with everything the record keeps beyond identity: the
    library flag staging reads, and the per-file title and track tag filling reads
    (§spec:tag-filling)."""
    return frozenset(manifest.files)


@dataclass
class CollectionSyncStats:
    discovered: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)
    stale: bool = False  # source enumeration failed; existing items left untouched
    # Fetches the source refused (recorded unavailable, not failed).
    unavailable: int = 0
    # Shows counted in `failed` because staging them left an issue (a foreign file at a
    # target, a failed removal, a refused path, a disk error, a tag write that failed),
    # not because of a fetch.
    staging_issues: int = 0
    # Why the run's failure alert was not delivered, shown with this run only.
    failure_notice_error: str | None = None
    # Separations in effect after the run's staging pass (§spec:library-ownership):
    # reported, never a failure.
    separated: int = 0
    # Archive path of each show fetched this run, for the handoff payload.
    fetched: dict[str, Path] = field(default_factory=dict)
    # Items fetched and staged this run, for the sync.complete batch payload
    # (SPEC §spec:handoff). Each entry: {identifier, archive_path, staging_path}.
    staged: list[dict[str, str]] = field(default_factory=list)


def run_sync(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    dry_run: bool = False,
    refresh_metadata: bool = False,
    config_path: Path | None = None,
) -> int:
    """Top-level sync entrypoint. Returns process exit code.

    Collections sync in bounded parallel, capped by the global
    `max_concurrent_collections` (SPEC §spec:configuration, §spec:sync-workflow).
    Each collection opens its own DB connection and is otherwise independent, so
    one collection's failure degrades only that collection's run.

    With ``refresh_metadata`` the run instead re-resolves and rewrites the
    metadata.json sidecars for preserve-opted collections, without re-downloading
    media (§spec:metadata-preservation).

    Every run except a dry run holds the writer lock (`shakedown.lock`): a second
    concurrent writer exits non-zero instead of racing this one. A refused sync
    scoped with ``collection_filter`` — from the CLI or the scheduler, not `serve`,
    and not ``refresh_metadata`` — is also recorded as rescheduled
    (§spec:rescheduled-syncs).

    When its own work is done, a sync that holds the lock — except
    ``refresh_metadata`` — catches up the rescheduled collections, still holding it
    (`_catch_up`), re-reading the config from ``config_path``. Without a path (callers
    that built their config in code) there is no file to re-read and no catch-up. The
    run exits non-zero when its own work or any catch-up failed.
    """
    if dry_run:
        return _run_sync(
            config, _scope(config, source_filter, collection_filter), True, refresh_metadata
        )
    # Asked now: refusing the run tells serve it was busy, which consumes the variable.
    reschedule = (
        collection_filter is not None and not refresh_metadata and not launched_by_serve()
    )

    def record() -> None:
        record_skipped(config, [
            (source.name, collection.name)
            for source, collection in _scope(config, source_filter, collection_filter)
        ])

    def own_work_then_catch_up() -> int:
        own = _run_sync(
            config, _scope(config, source_filter, collection_filter), False, refresh_metadata
        )
        if refresh_metadata or config_path is None:
            return own
        caught_up = _catch_up(config_path)
        return 0 if own == 0 and caught_up == 0 else 1

    return run_exclusive(
        config, own_work_then_catch_up, on_busy=record if reschedule else None
    )


def _catch_up(config_path: Path) -> int:
    """Catch up the rescheduled collections once a lock-holding sync has finished its
    own work (§spec:rescheduled-syncs steps 2-5). Returns 1 when a catch-up failed or
    could not run.

    The config is read afresh: the finishing run may have loaded its own days ago, and
    a collection added since must not be dropped as unknown. Only entries outstanding
    now are taken, so the run ends even while skips keep arriving; later ones wait for
    the next sync to finish. The rest sync as one multi-collection sync on the ordinary
    bounded path, submitted oldest skip first so they start in that order. With an
    invalid config nothing runs and every entry is kept.
    """
    try:
        config = load_config(config_path)
        ensure_hardlinkable(config.archive_root, config.library_root)
    except (ConfigError, FilesystemError) as e:
        log.error(
            "catch-up not run: %s; rescheduled syncs kept for the next sync to finish", e
        )
        return 1
    configured = {(s.name, c.name): (s, c) for s, c in _scope(config, None, None)}
    due: list[tuple[SourceConfig, CollectionConfig, datetime]] = []
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        rescheduled = RescheduledRepo(conn)
        for source_name, collection_name, since in rescheduled.list():
            pair = configured.get((source_name, collection_name))
            if pair is None:
                rescheduled.clear(source_name, collection_name)
                log.warning(
                    "%s/%s: rescheduled sync dropped: no longer in the config",
                    source_name, collection_name,
                )
                continue
            due.append((*pair, since))
    if not due:
        return 0
    log.info(
        "catching up %d rescheduled collection(s), oldest skip first: %s",
        len(due),
        ", ".join(f"{s.name}/{c.name} (skipped {since.isoformat()})" for s, c, since in due),
    )
    return _run_sync(
        config,
        [(s, c) for s, c, _since in due],
        False,
        False,
        catch_up={(s.name, c.name): since for s, c, since in due},
    )


def _scope(
    config: Config, source_filter: str | None, collection_filter: str | None
) -> list[tuple[SourceConfig, CollectionConfig]]:
    """The (source, collection) pairs a sync with these filters covers."""
    return [
        (source, collection)
        for source in config.sources
        if not source_filter or source.name == source_filter
        for collection in source.collections
        if not collection_filter or collection.name == collection_filter
    ]


def _run_sync(
    config: Config,
    scope: list[tuple[SourceConfig, CollectionConfig]],
    dry_run: bool,
    refresh_metadata: bool,
    *,
    catch_up: dict[tuple[str, str], datetime] | None = None,
) -> int:
    """Sync ``scope`` in bounded parallel, starting collections in its order. With
    ``catch_up`` — each collection's oldest skip — the collections are catch-ups: their
    log lines say so, and each clears its rescheduled entry when it ends, whatever its
    result (§spec:rescheduled-syncs Clearing). A killed run clears nothing."""
    plugins: dict[str, SourcePlugin] = {}
    work: list[tuple[SourceConfig, CollectionConfig, SourcePlugin]] = []
    for source, collection in scope:
        if source.name not in plugins:
            plugins[source.name] = registry.for_source(source)
        work.append((source, collection, plugins[source.name]))

    if not work:
        if not dry_run:
            announce_started([])
        return 0

    # Initialize the state DB once, up front. The one-time WAL switch and schema
    # migration need brief exclusive access; doing it here keeps concurrent
    # collection workers from racing on it (each then opens an already-WAL,
    # already-migrated DB). SPEC §spec:sync-workflow.
    # A dry run takes no writer lock, so it reads the database without migrating it.
    (connect_reader if dry_run else connect)(config.state_db).close()  # type: ignore[arg-type]

    budgets = source_budgets(config, [(s, p) for s, _, p in work])

    if refresh_metadata:
        announce_started([])
        return _run_refresh(config, work, budgets)

    opened: dict[tuple[str, str], tuple[Run, OperationOutcome]] = (
        {} if dry_run else _open_runs(config, work)
    )

    overall_failed = 0
    max_workers = min(config.max_concurrent_collections, len(work))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _sync_collection, config, source, collection, plugin,
                dry_run=dry_run, budget=budgets[source.name],
                opened=opened.get((source.name, collection.name)),
                catch_up_note=_catch_up_note(catch_up, source, collection),
            ): (source, collection)
            for source, collection, plugin in work
        }
        for fut in as_completed(futures):
            source, collection = futures[fut]
            note = _catch_up_note(catch_up, source, collection)
            try:
                stats = fut.result()
            except Exception:
                log.exception("sync failed for %s/%s%s", source.name, collection.name, note)
                overall_failed += 1
                continue
            finally:
                if catch_up is not None:
                    _clear_rescheduled(config, source, collection)
            if stats.stale:
                log.warning(
                    "%s/%s: STALE (source enumeration failed)%s",
                    source.name, collection.name, note,
                )
            else:
                log.info(
                    "%s/%s: discovered=%d new=%d updated=%d failed=%d separated=%d bytes=%d%s",
                    source.name, collection.name,
                    stats.discovered, stats.new, stats.updated, stats.failed,
                    stats.separated, stats.bytes_downloaded, note,
                )
            # A separation is reported but never fails the run: no recording was lost.
            log_separations(source.name, collection.name, stats.separated)
            if stats.failed > 0 or stats.stale:
                overall_failed += 1
    return 0 if overall_failed == 0 else 1


def _catch_up_note(
    catch_up: dict[tuple[str, str], datetime] | None,
    source: SourceConfig,
    collection: CollectionConfig,
) -> str:
    """The suffix that marks a catch-up's log lines and names its skip time."""
    if catch_up is None:
        return ""
    return f" (catch-up of the sync skipped at {catch_up[(source.name, collection.name)].isoformat()})"


def _clear_rescheduled(config: Config, source: SourceConfig, collection: CollectionConfig) -> None:
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        RescheduledRepo(conn).clear(source.name, collection.name)


def _open_runs(
    config: Config, work: list[tuple[SourceConfig, CollectionConfig, SourcePlugin]]
) -> dict[tuple[str, str], tuple[Run, OperationOutcome]]:
    """Open every collection's run record and outcome before any collection starts,
    then announce them to `serve` if it started this run (§spec:api-sync-runs): serve
    answers a trigger with the run ids, so they must exist before the first collection
    begins. Each collection resets its start time when it actually begins. Only the
    first announce reaches serve, so a catch-up's runs, opened after the run's own,
    are not in its answer; they show in `GET /status` like any run."""
    started = datetime.now()
    opened: dict[tuple[str, str], tuple[Run, OperationOutcome]] = {}
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        runs = RunRepo(conn)
        outcomes = OperationOutcomeRepo(conn)
        for source, collection, _plugin in work:
            run = runs.start(source.name, collection.name, started)
            outcome = outcomes.start(
                OperationType.SYNC,
                source.name,
                collection.name,
                started,
                phase="discover",
                safe_next_action="restore source access and rerun sync",
            )
            opened[(source.name, collection.name)] = (run, outcome)
    announce_started([
        {
            "run_id": run.id,
            "outcome_id": outcome.id,
            "source": run.source_name,
            "collection": run.collection_name,
        }
        for run, outcome in opened.values()
    ])
    return opened


def _run_refresh(
    config: Config,
    work: list[tuple[SourceConfig, CollectionConfig, SourcePlugin]],
    budgets: dict[str, SourceBudget],
) -> int:
    """Drive `sync --refresh-metadata` across the selected collections (§spec:metadata-preservation).

    Only preserve-opted collections are refreshed; others are skipped with a note. Serial
    per collection — an explicit, operator-invoked maintenance pass, not the hot sync path —
    but each source's re-resolution still draws on its shared politeness budget.
    """
    overall_failed = 0
    for source, collection, plugin in work:
        if not collection.preserve_source_metadata:
            log.info(
                "[%s/%s] skipping metadata refresh: preserve_source_metadata not enabled",
                source.name, collection.name,
            )
            continue
        try:
            overall_failed += _refresh_collection(
                config, source, collection, plugin, budgets[source.name]
            )
        except Exception:
            log.exception("metadata refresh failed for %s/%s", source.name, collection.name)
            overall_failed += 1
    return 0 if overall_failed == 0 else 1


def _refresh_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    budget: SourceBudget,
) -> int:
    """Re-resolve source metadata for already-mirrored items and rewrite their sidecars.

    Re-resolution reuses the budgeted descriptor stream, so it honors the shared per-source
    politeness ceiling (§spec:source-budget). For each mirrored item still enumerated
    upstream, the fresh metadata is written to `metadata.json` (in place, so the library
    hardlink tracks it) and to the recorded `source_metadata`, then the collection is
    restaged, so a show whose folder renders differently moves — media is never
    re-downloaded. Returns 1 if a lookup failed or staging left a per-show issue, else 0.
    """
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    _recover(config, source.name, collection)
    mirrored = [
        it
        for it in items.list_for_collection(source.name, collection.name)
        if it.status == ItemStatus.COMPLETE
        and it.archive_path is not None
        and it.recorded_manifest is not None
    ]
    if not mirrored:
        log.info("[%s/%s] metadata refresh: no mirrored items", source.name, collection.name)
        return 0

    mirrored_ids = {it.identifier for it in mirrored}
    fresh: dict[str, ItemDescriptor] = {}
    failed = 0
    try:
        for found in stream_items(
            plugin, collection, budget, set(),
            look_up=lambda row: row.identifier in mirrored_ids,
        ):
            if isinstance(found, LookupFailure):
                # The item keeps its sidecar and recorded metadata; retried next refresh.
                failed += 1
                log.warning("[%s/%s] %s: %s", source.name, collection.name,
                            found.identifier, found.reason)
            else:
                fresh[found.identifier] = found
    except Exception as e:
        log.warning(
            "[%s/%s] metadata refresh enumeration failed: %s", source.name, collection.name, e
        )
        return 1

    refreshed = 0
    for item in mirrored:
        desc = fresh.get(item.identifier)
        if desc is None:
            continue  # not listed upstream, or its lookup failed (logged above)
        assert item.archive_path is not None and item.recorded_manifest is not None
        item.source_metadata = desc.metadata
        # Rewrite the DB record always, but skip the sidecar when a media file claims its
        # reserved name (staging applies the same guard, §spec:sync-identity).
        if not manifest_reserves_sidecar_name(item.recorded_manifest):
            write_sidecar(item.archive_path / METADATA_SIDECAR, desc.metadata)
        items.upsert(item)
        refreshed += 1
    # A changed venue or date changes the tags shakedown filled (§spec:tag-filling).
    tag_issues = _fill_tags(config, source.name, collection, items)
    for lines in tag_issues.values():
        for line in lines:
            log.warning("[%s/%s] %s", source.name, collection.name, line)
    report = LibraryPlacement(config, collection, source.name, items).finish()
    log.info(
        "[%s/%s] metadata refresh: refreshed=%d of %d mirrored",
        source.name, collection.name, refreshed, len(mirrored),
    )
    log_separations(source.name, collection.name, report.separations)
    if report.render_error is not None:
        log.warning("[%s/%s] %s: %s", source.name, collection.name, *report.render_error)
        return 1
    return 1 if report.issues or tag_issues or failed else 0


def _apply_placement_report(
    stats: CollectionSyncStats,
    report: PlacementReport,
    tag_issues: dict[str, list[str]],
) -> None:
    """Fold a staging pass, and the tag filling before it, into the run: each show
    with an issue counts once as failed (a disk error while linking already did,
    §spec:failure-behavior), separations are counted for the summary, and shows
    fetched this run join the handoff payload with the folder they ended up in."""
    if report.render_error is not None:
        identifier, message = report.render_error
        stats.failed += 1
        stats.staging_issues += 1
        stats.errors.append(f"{identifier}: {message}")
    for identifier in sorted(tag_issues.keys() | report.issues.keys()):
        stats.failed += 1
        stats.staging_issues += 1
        stats.errors.extend(tag_issues.get(identifier, []))
        stats.errors.extend(report.issues.get(identifier, []))
    stats.separated = report.separations
    for identifier, archive_path in stats.fetched.items():
        folder = report.folder_of.get(identifier)
        if folder is None:
            continue  # refused: nothing of it is in the library
        stats.staged.append(
            {
                "identifier": identifier,
                "archive_path": str(archive_path),
                "staging_path": str(folder),
            }
        )


def _fill_tags(
    config: Config, source_name: str, collection: CollectionConfig, items: ItemRepo
) -> dict[str, list[str]]:
    """The run's tag filling over mirrored shows, before its staging pass, on a
    `fill_tags` collection (§spec:tag-filling)."""
    if not collection.fill_tags:
        return {}
    return fill_collection_tags(
        config.archive_root / source_name / collection.name, items,
        source_name, collection.name,
    )


def _recover(config: Config, source_name: str, collection: CollectionConfig) -> None:
    """Finish what a crashed run left half done, before anything else reads the
    archive: an interrupted swap of a show's folder, and an interrupted tag write
    (whether or not the collection still fills tags)."""
    root = config.archive_root / source_name / collection.name
    _recover_interrupted_swaps(root)
    recover_interrupted_tagging(root)


def _finish_run(runs: RunRepo, run: Run, stats: CollectionSyncStats, finished: datetime) -> None:
    """Persist the run record from the accumulated stats (SPEC §spec:sync-workflow, step 6)."""
    run.items_discovered = stats.discovered
    run.items_new = stats.new
    run.items_updated = stats.updated
    run.items_failed = stats.failed
    run.bytes_downloaded = stats.bytes_downloaded
    run.stale = stats.stale
    run.errors = stats.errors
    run.failure_notice_error = stats.failure_notice_error
    run.finished_at = finished
    runs.finish(run)


def _completed_work(stats: CollectionSyncStats) -> dict[str, int]:
    return {
        "items_discovered": stats.discovered,
        "items_new": stats.new,
        "items_updated": stats.updated,
        "items_failed": stats.failed,
        "bytes_downloaded": stats.bytes_downloaded,
    }


def _deletion_context(collection: CollectionConfig) -> str:
    if collection.prune_disappeared:
        return "prune_disappeared enabled; deletion only after successful enumeration"
    return "no deletion requested"


def _affected_item_from_errors(errors: list[str]) -> str | None:
    if not errors:
        return None
    identifier, sep, _detail = errors[0].partition(":")
    return identifier if sep and identifier else None


def _sync_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    *,
    dry_run: bool,
    budget: SourceBudget,
    opened: tuple[Run, OperationOutcome] | None = None,
    catch_up_note: str = "",
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
    if catch_up_note:
        log.info("[%s/%s] starting%s", source.name, collection.name, catch_up_note)
    conn = (connect_reader if dry_run else connect)(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)
    outcomes = OperationOutcomeRepo(conn)

    run: Run | None = None
    outcome: OperationOutcome | None = None
    if opened is not None:
        # Opened up front by `_open_runs`; time the run from when this collection
        # actually begins, not from when it was queued behind the others.
        run, outcome = opened
        runs.mark_started(run, started)
        outcomes.mark_started(outcome, started)

    stats = CollectionSyncStats()
    try:
        return _sync_collection_phases(
            config, source, collection, plugin, items, runs, outcomes, run, outcome,
            stats, started, dry_run=dry_run, budget=budget,
        )
    except Exception as e:
        _close_crashed(runs, run, outcomes, outcome, stats, collection, e, RunKind.SYNC)
        raise


def _close_crashed(
    runs: RunRepo,
    run: Run | None,
    outcomes: OperationOutcomeRepo,
    outcome: OperationOutcome | None,
    stats: CollectionSyncStats,
    collection: CollectionConfig,
    error: Exception,
    kind: RunKind,
) -> None:
    """An unexpected crash must still close the run record and its outcome, so
    `status` never shows a run stuck in progress and the next run starts clean."""
    stats.errors.append(f"{kind.value} crashed: {error}")
    finished = datetime.now()
    if run is not None and run.finished_at is None:
        _finish_run(runs, run, stats, finished)
    if outcome is not None and outcome.finished_at is None:
        outcomes.finish(
            outcome,
            OperationStatus.FAILED_BEFORE_COMPLETION,
            finished,
            phase=outcome.phase,
            completed_work=_completed_work(stats),
            preservation_context="completed archive work retained",
            deletion_context=_deletion_context(collection),
            safe_next_action=f"check the logged error and rerun {_RERUN[kind]}",
            errors=stats.errors,
        )


def _finish_outcome(
    outcomes: OperationOutcomeRepo,
    outcome: OperationOutcome,
    stats: CollectionSyncStats,
    collection: CollectionConfig,
    finished: datetime,
    kind: RunKind,
    shows: Collection[str] | None = None,
) -> None:
    """Close a run's outcome once its work is done (§spec:recoverable-operation-reporting).
    A clean run clears earlier warnings of its own kind only: a single-show run did
    not redo a sync's work, and a sync does not refetch an unchanged show. A clean
    single-show run (``shows``) clears only warnings about the shows it acted on."""
    if stats.failed > 0:
        phase = "stage" if stats.failed == stats.staging_issues else "fetch"
        outcomes.finish(
            outcome,
            OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
            finished,
            phase=phase,
            affected_item=_affected_item_from_errors(stats.errors),
            completed_work=_completed_work(stats),
            preservation_context="completed archive work retained",
            deletion_context=_deletion_context(collection),
            safe_next_action=f"fix failed item issues and rerun {_RERUN[kind]}",
            errors=stats.errors,
        )
        return
    outcomes.finish(
        outcome,
        OperationStatus.COMPLETED,
        finished,
        phase="record",
        completed_work=_completed_work(stats),
        deletion_context=_deletion_context(collection),
    )
    outcomes.resolve_actionable(
        OperationType(kind.value), outcome.source_name, outcome.collection_name, finished,
        affected_items=shows,
    )


def _sync_collection_phases(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    items: ItemRepo,
    runs: RunRepo,
    outcomes: OperationOutcomeRepo,
    run: Run | None,
    outcome: OperationOutcome | None,
    stats: CollectionSyncStats,
    started: datetime,
    *,
    dry_run: bool,
    budget: SourceBudget,
) -> CollectionSyncStats:
    """The phases of `_sync_collection`, run inside its crash guard."""
    conn = items.conn

    log.info("[%s/%s] discovering...", source.name, collection.name)

    if dry_run:
        # Dry-run performs Discover and Plan only (SPEC §spec:sync-workflow): no fetch,
        # no overlap to exploit, no writes. Materialize the enumeration and report the
        # plan without touching disk or DB.
        enumerated_identifiers: set[str] = set()
        descriptors: list[ItemDescriptor] = []
        try:
            for found in stream_items(plugin, collection, budget, enumerated_identifiers):
                if isinstance(found, LookupFailure):
                    _note_lookup_failure(stats, source.name, collection.name, found)
                else:
                    descriptors.append(found)
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

    # Finish any swap or tag write a crashed run left half done, before anything
    # decides whether an item's archive copy exists.
    _recover(config, source.name, collection)

    # Snapshot the pre-run DB state once. It drives per-item classification (is this
    # `new`/`changed-upstream`/`unchanged`?) and, after enumeration, the prune barrier
    # (which recorded items were not seen this run?). A fetch upserts rows during the
    # run, but the disappeared decision is computed against this pre-run snapshot, so a
    # freshly fetched item is never mistaken for a vanished one.
    # The snapshot leaves out each item's source metadata (planning never reads it);
    # anything that writes or stages an item loads the full row first.
    by_identifier = {
        item.identifier: item
        for item in items.list_for_planning(source.name, collection.name)
    }
    # Every show's library folder, so each download is linked as soon as it lands
    # (§spec:discovery-pipeline) at the folder a full pass would give it.
    placement = LibraryPlacement(config, collection, source.name, items)
    placement.begin()

    # Phases 1-4, pipelined: discover -> classify -> fetch + stage, overlapping downloads
    # with the remaining enumeration (SPEC §spec:discovery-pipeline, overlap lever).
    planner = (
        TagPlanner(items, source.name, collection.name) if collection.fill_tags else None
    )
    enumerated_identifiers, deferred, enum_error = _pipelined_discover_and_fetch(
        config, source, collection, plugin, items, stats, placement, budget, by_identifier,
        planner,
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
        # Shows fetched before the failure are valid; stage them. Nothing is pruned.
        tag_issues = _fill_tags(config, source.name, collection, items)
        report = placement.finish()
        _apply_placement_report(stats, report, tag_issues)
        finished = datetime.now()
        _fire_notifications(
            config, items, source, collection, stats, started, finished, report.folder_of
        )
        if run is not None:
            _finish_run(runs, run, stats, finished)
        if outcome is not None:
            outcomes.finish(
                outcome,
                OperationStatus.STALE_ENUMERATION,
                finished,
                phase="discover",
                completed_work=_completed_work(stats),
                preservation_context="local recordings retained; no pruning performed",
                deletion_context=_deletion_context(collection),
                safe_next_action="restore source access and rerun sync",
                errors=stats.errors,
            )
        return stats

    # Post-enumeration barrier: the enumeration is complete and successful, so the
    # prune/`disappeared` set (in the DB but not returned by this enumeration) is now safe
    # to compute. Keyed on `enumerated_identifiers` — an item enumeration listed but whose
    # description failed counts as present and is never pruned (SPEC §spec:prune-safety).
    disappeared = _disappeared_plan(
        by_identifier, enumerated_identifiers, collection.prune_disappeared
    )
    _log_pipeline_plan(source.name, collection.name, stats, deferred, disappeared)
    _sweep_abandoned_tmp_dirs(
        config.archive_root / source.name / collection.name,
        enumerated=enumerated_identifiers,
        settled={_planned_identifier(p) for p in deferred},
    )

    # Bring UNCHANGED shows back to complete or refresh their reason; the staging pass
    # below relinks any of their files a library tool deleted (PRD §13).
    for p in deferred:
        if (
            p.action == PlanAction.UNCHANGED
            and p.existing is not None
            and p.existing.recorded_manifest is not None
        ):
            existing = items.get(source.name, collection.name, p.existing.identifier)
            if existing is None or existing.recorded_manifest is None:
                continue
            # A looked-up show brings its current reasons, its change signal (the full
            # comparison just proved it unchanged) and, when the rule now stages
            # different files, its new manifest; a show skipped on an unchanged signal
            # is complete and keeps its own.
            desc = p.descriptor
            update = desc is not None and (
                bool(desc.manifest.files)
                or desc.restriction_reason != existing.restriction_reason
                or desc.not_playable_reason != existing.not_playable_reason
                or (desc.change_signal or existing.change_signal) != existing.change_signal
            )
            if desc is not None:
                if desc.manifest.files:
                    existing.recorded_manifest = desc.manifest
                existing.restriction_reason = desc.restriction_reason
                existing.not_playable_reason = desc.not_playable_reason
                existing.change_signal = desc.change_signal or existing.change_signal
            if existing.status != ItemStatus.COMPLETE:
                # Back upstream with the files we already hold (e.g. after an empty
                # answer or a lifted restriction): the archive copy is complete again.
                _record_back_to_complete(items, existing, existing.restriction_reason)
            elif update:
                items.upsert(existing)

    # Persist the disappeared set in one shot (unavailable shows were recorded as listed).
    with transaction(conn):
        for p in disappeared:
            assert p.existing is not None
            existing = items.get(source.name, collection.name, p.existing.identifier)
            if existing is not None:
                _record_disappeared(items, source.name, collection, existing)

    # Stage: every show with an archive copy at its current folder, and every file
    # no longer placed (a pruned show, an old layout or name) removed
    # (§spec:library-ownership, §spec:library-cleanup). Tags are filled first, so the
    # library sees each show's files once, already tagged (§spec:tag-filling).
    tag_issues = _fill_tags(config, source.name, collection, items)
    report = placement.finish()
    _apply_placement_report(stats, report, tag_issues)

    # Phase 5: notify. Fire before recording so an undelivered notice is captured
    # in the run's errors but never aborts the sync (§spec:durable-handoff).
    finished = datetime.now()
    _fire_notifications(
        config, items, source, collection, stats, started, finished, report.folder_of
    )

    # Phase 6: record run
    if run is not None:
        _finish_run(runs, run, stats, finished)
    if outcome is not None:
        _finish_outcome(outcomes, outcome, stats, collection, finished, RunKind.SYNC)
    # The collection is finished with a complete enumeration, so a skip recorded for
    # it — before or during this run — is done (§spec:rescheduled-syncs).
    RescheduledRepo(conn).clear(source.name, collection.name)

    return stats


def _pipelined_discover_and_fetch(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    items: ItemRepo,
    stats: CollectionSyncStats,
    placement: LibraryPlacement,
    budget: SourceBudget,
    by_identifier: dict[str, Item],
    planner: TagPlanner | None,
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
      described set: an item enumeration listed but whose lookup failed this run
      is still present and must never be pruned or flagged `disappeared`
      (SPEC §spec:prune-safety). Such an item is listed failed and never fetched or
      deferred; a recorded item keeps its DB row unchanged for the next run.
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
        # Fetch workers announce completion here, so the orchestrating thread drains
        # only finished fetches instead of rescanning the whole backlog each item.
        finished: queue.SimpleQueue[Future[_FetchOutcome]] = queue.SimpleQueue()

        def dispatch(desc: ItemDescriptor) -> None:
            planned = _classify_one(desc, by_identifier)
            if planned.action in (PlanAction.NEW, PlanAction.CHANGED_UPSTREAM):
                if planner is not None:
                    assert planned.descriptor is not None
                    planned.tags = planner.plan(
                        desc.identifier, desc.metadata, planned.descriptor.manifest
                    )
                archive_root.mkdir(parents=True, exist_ok=True)
                fut = fetch_pool.submit(
                    _fetch_one_budgeted, budget, plugin, planned, archive_root, collection
                )
                fetch_futures[fut] = planned
                fut.add_done_callback(finished.put)
            elif planned.action == PlanAction.UNCHANGED:
                # Re-staging needs only the record and the current reasons: drop the
                # rest of the listed details now, keeping the manifest only when the
                # record must take it (files the rule no longer selects, new flags).
                assert planned.descriptor is not None and planned.existing is not None
                manifest = planned.descriptor.manifest
                recorded = planned.existing.recorded_manifest
                if recorded is not None and _entries(manifest) == _entries(recorded):
                    manifest = Manifest(files=())
                deferred.append(
                    PlannedItem(
                        replace(planned.descriptor, manifest=manifest, metadata={}),
                        planned.existing,
                        PlanAction.UNCHANGED,
                    )
                )
            else:
                # Unavailable is this show's own fact, safe to record the moment it is
                # listed (SPEC §spec:discovery-pipeline); keep only its identifier.
                assert planned.descriptor is not None
                _record_unavailable(items, source.name, collection.name, planned.descriptor)
                deferred.append(
                    PlannedItem(
                        replace(planned.descriptor, manifest=Manifest(files=()), metadata={}),
                        planned.existing,
                        PlanAction.UNAVAILABLE,
                    )
                )

        def on_skip(identifier: str, existing: Item) -> None:
            # Signal-unchanged: classify UNCHANGED without a metadata fetch. The identifier
            # is already in `enumerated` (the stream records every enumerated id), so the
            # prune barrier never mistakes it for disappeared; deferred here for the
            # caller's idempotent re-stage pass (§spec:incremental-discovery).
            deferred.append(PlannedItem(None, existing, PlanAction.UNCHANGED))

        def look_up(row: ListedItem) -> bool:
            # Incremental fast path (§spec:incremental-discovery): an item whose stored
            # change signal still matches is UNCHANGED without a lookup.
            existing = by_identifier.get(row.identifier)
            if collection.incremental_discovery and _signal_unchanged(
                row.change_signal, existing
            ):
                assert existing is not None  # _signal_unchanged guarantees it
                on_skip(row.identifier, existing)
                return False
            return True

        descriptor_stream = stream_items(
            plugin, collection, budget, enumerated, look_up=look_up
        )
        def process(fut: Future[_FetchOutcome]) -> None:
            _process_fetch_result(
                fut, fetch_futures.pop(fut), source, collection, items, stats, placement,
            )

        descriptors = iter(descriptor_stream)
        # Downloads take far longer than listing: take the next show only while few
        # wait for a download, so listed shows never pile up in memory.
        backlog = 2 * config.max_concurrent_downloads
        while True:
            while len(fetch_futures) >= backlog:
                process(finished.get())
            try:
                # Descriptors stream in as each resolves; dispatch classifies and
                # fetches each immediately, so downloads overlap the enumeration.
                desc = next(descriptors, None)
                if desc is None:
                    break
                if isinstance(desc, LookupFailure):
                    _record_lookup_failure(items, source.name, collection.name, desc, stats)
                else:
                    dispatch(desc)
            except Exception as e:  # any enumeration fault marks the collection stale
                enum_error = e
                break
            # Record and link every download that has finished so far, without waiting
            # for the listing to end (hours on a big collection): if the run dies, the
            # finished downloads are already in the DB and are not fetched again.
            while not finished.empty():
                process(finished.get())

        # Drain every fetch still in flight. Those downloads are valid whether or not
        # the enumeration completed, and recording them keeps the archive and DB
        # consistent (SPEC §spec:discovery-pipeline: an item may be fetched and staged
        # before discovery completes).
        for fut in as_completed(list(fetch_futures)):
            process(fut)

    return enumerated, deferred, enum_error


def _fire_notifications(
    config: Config,
    items: ItemRepo,
    source: SourceConfig,
    collection: CollectionConfig,
    stats: CollectionSyncStats,
    started: datetime,
    finished: datetime,
    folder_of: dict[str, Path],
) -> None:
    """Send the once-per-(collection, run) handoff and failure notifications
    (SPEC §spec:handoff, §spec:durable-handoff).

    The handoff goes out on every run that has shows to hand off — newly staged or
    still pending from an earlier run. ``sync.failed`` fires when the run failed
    (source enumeration stale, or one or more items failed). A notice that is not
    delivered is appended to ``stats.errors`` and never changes the run's outcome.
    """
    run_errors = list(stats.errors)  # substantive errors, before any delivery attempt
    run_obj = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "items_new": stats.new,
        "items_updated": stats.updated,
        "items_failed": stats.failed,
        "bytes_downloaded": stats.bytes_downloaded,
    }

    err = send_collection_handoff(
        config, items, source.name, collection,
        staged=stats.staged, folder_of=folder_of, run=run_obj,
    )
    if err:
        stats.errors.append(f"handoff not delivered: {err}")

    if stats.stale or stats.failed > 0:
        err = fire_failure(
            config.notifications,
            SyncFailedPayload(
                source=source.name,
                collection=collection.name,
                staging_root=_staging_root(config, source.name, collection),
                run=run_obj,
                errors=run_errors,
            ),
        )
        if err:
            log.warning(
                "[%s/%s] failure notice not delivered: %s", source.name, collection.name, err
            )
            stats.errors.append(f"failure notice not delivered: {err}")
            stats.failure_notice_error = err


def _staging_root(config: Config, source_name: str, collection: CollectionConfig) -> str:
    return str(config.library_root / source_name / collection.name)


def send_collection_handoff(
    config: Config,
    items: ItemRepo,
    source_name: str,
    collection: CollectionConfig,
    *,
    staged: list[dict[str, str]],
    folder_of: dict[str, Path],
    run: dict[str, Any],
) -> str | None:
    """Send this collection's handoff for one run (§spec:durable-handoff): ONE notice
    whose ``staged`` list holds the shows the run staged plus every show still pending
    from earlier runs, at their current library folders. Returns why the notice was not
    delivered, or ``None``. Called by every run of a collection, sync or single-show.

    ``staged`` holds this run's entries ({identifier, archive_path, staging_path});
    ``folder_of`` is the library folder of every show staged after the run's full
    placement pass. A delivered notice clears the shows it covered; an undelivered one
    keeps them all pending with the reason. A pending show without an archive copy
    (pruned) leaves the list; one with a copy but no folder this pass stays pending,
    left out of this notice. With no ``on_complete`` the pending list is dropped.
    """
    pending = PendingHandoffRepo(items.conn)
    if collection.on_complete is None:
        pending.drop(source_name, collection.name)
        return None

    entries = {entry["identifier"]: entry for entry in staged}
    gone: list[str] = []
    for show in pending.list(source_name, collection.name):
        if show.identifier in entries:
            continue
        item = items.get(source_name, collection.name, show.identifier)
        if item is None or not has_archive_copy(item):
            gone.append(show.identifier)
            continue
        folder = folder_of.get(show.identifier)
        if folder is None:
            continue
        entries[show.identifier] = {
            "identifier": show.identifier,
            "archive_path": str(item.archive_path),
            "staging_path": str(folder),
        }
    pending.clear(source_name, collection.name, gone)
    if not entries:
        return None

    err = fire_complete(
        collection,
        SyncCompletePayload(
            source=source_name,
            collection=collection.name,
            staging_root=_staging_root(config, source_name, collection),
            run=run,
            staged=list(entries.values()),
        ),
    )
    if err is None:
        pending.clear(source_name, collection.name, entries)
        return None
    log.warning("[%s/%s] handoff not delivered: %s", source_name, collection.name, err)
    pending.mark_undelivered(source_name, collection.name, entries, err, datetime.now())
    return err


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
    a re-fetch. Only the selected files that are missing are downloaded (plan_files).
    This decision needs only *this* item's state, so it is safe to make the
    moment the item is discovered, before the rest of the enumeration
    (SPEC §spec:discovery-pipeline).
    """
    existing = by_identifier.get(desc.identifier)
    if desc.is_restricted:
        return PlannedItem(desc, existing, PlanAction.UNAVAILABLE)
    if not desc.manifest.files:
        # No audio listed (darked item, upstream glitch) is never "the item changed":
        # fetching it would swap an audio-less folder in over the archive copy. Record
        # it unavailable and keep the local copy.
        return PlannedItem(
            replace(desc, restriction_reason=NO_FILES_REASON), existing, PlanAction.UNAVAILABLE
        )
    if existing is None or existing.recorded_manifest is None:
        return PlannedItem(desc, existing, PlanAction.NEW)
    manifest, fetch = plan_files(desc.manifest, existing)
    desc = replace(desc, manifest=manifest)
    if fetch:
        return PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM, fetch)
    return PlannedItem(desc, existing, PlanAction.UNCHANGED)


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
        # Only a complete show may skip the lookup: any other status (a refusal, a
        # failure, a gap in the listing) is re-checked against the source.
        and existing.status == ItemStatus.COMPLETE
        and existing.change_signal is not None
        and existing.change_signal == signal
        and not _files_missing(existing)
    )


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
    enumeration returned counts as present even when its lookup failed this run,
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


def _process_fetch_result(
    fut: Future[_FetchOutcome],
    target: PlannedItem,
    source: SourceConfig,
    collection: CollectionConfig,
    items: ItemRepo,
    stats: CollectionSyncStats,
    placement: LibraryPlacement,
    *,
    refused_fails: bool = False,
) -> None:
    """Apply one completed fetch: record the row, link it into the library, accumulate
    stats. With ``refused_fails`` (a single-show run) a download the source refuses
    also fails the show in the run.

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

    if outcome.unavailable:
        stats.unavailable += 1
        log.info(
            "[%s/%s] %s: unavailable: %s",
            source.name, collection.name, desc.identifier, outcome.error,
        )
        _record_unavailable(
            items, source.name, collection.name, replace(desc, restriction_reason=outcome.error)
        )
        if refused_fails:
            _fail_refused(stats, desc.identifier, outcome.error)
        return

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

    with transaction(items.conn):
        _record_complete(items, source.name, collection, desc, outcome.archive_path)
        _record_download_tags(items, source.name, collection.name, target, outcome)
    stats.fetched[desc.identifier] = outcome.archive_path
    # Linked now, not at the end of the run, so a run that dies later keeps it staged.
    # Issues are reported by the run's full staging pass, which meets them again.
    placement.update(desc.identifier)


@dataclass
class _FetchOutcome:
    success: bool
    archive_path: Path
    bytes_downloaded: int = 0
    error: str | None = None
    unavailable: bool = False  # the source refuses to serve the item
    # What tagging the downloaded files recorded, per file; None when nothing was tagged.
    tag_records: dict[str, TagRecord | None] | None = None


# Bounded core-owned retries for transient fetch faults (checksum mismatch,
# truncated download, rate limits). SPEC §spec:failure-behavior.
_MAX_FETCH_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff for the Nth (1-based) attempt, capped."""
    return min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


def _fetch_with_retries(
    plugin: SourcePlugin,
    desc: ItemDescriptor,
    tmp_dir: Path,
    *,
    files: Sequence[ManifestFile] | None = None,
    max_attempts: int = _MAX_FETCH_ATTEMPTS,
) -> FetchResult:
    """Call plugin.fetch with bounded retries and backoff (SPEC §spec:failure-behavior).

    Retries are core-owned, not delegated to the plugin's library: a transient
    fault (checksum mismatch, truncated download, rate limit) is retried up to
    `max_attempts` times. When the source supplies a `Retry-After`, we honor it
    in place of exponential backoff.

    The temp dir is kept between attempts (and runs), so one late error does not
    re-download a whole show: plugins keep a leftover file only once it matches the
    manifest's size and md5, and fetch the rest. Anything in the temp dir that is not
    a file to fetch is removed before each attempt.

    ``files`` are the files to download (default: the whole manifest).
    """
    wanted = desc.manifest.files if files is None else tuple(files)
    for attempt in range(1, max_attempts + 1):
        _prepare_tmp_dir(wanted, tmp_dir)

        result = plugin.fetch(desc.identifier, wanted, tmp_dir)
        if result.success:
            short = _size_mismatches(wanted, tmp_dir)
            if short:
                # A plugin can report success over a short file (dropped connection,
                # truncated write). Sizes are a stat, not a hash; retry like any
                # truncated download.
                result = FetchResult(
                    success=False,
                    bytes_downloaded=result.bytes_downloaded,
                    error=f"size differs from the source after fetch: {short[:5]}",
                    retriable=True,
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


def _prepare_tmp_dir(files: Sequence[ManifestFile], tmp_dir: Path) -> None:
    """Create the temp dir, keeping files a previous attempt left for the names to
    fetch (the plugin re-checks them before reuse) and removing everything else."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    wanted = {os.path.normpath(mf.name) for mf in files}
    wanted_dirs = {os.path.dirname(name) for name in wanted}
    for dirpath, dirnames, filenames in os.walk(tmp_dir, topdown=False):
        rel_dir = os.path.relpath(dirpath, tmp_dir)
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if rel not in wanted:
                os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            path = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(path):
                os.unlink(path)
            elif not any(d == rel or d.startswith(rel + os.sep) for d in wanted_dirs):
                shutil.rmtree(path)


def _size_mismatches(files: Sequence[ManifestFile], tmp_dir: Path) -> list[str]:
    """Fetched files present in the temp dir whose size differs from the source's.
    Missing files are left to the plugin's verify() guard."""
    short: list[str] = []
    for mf in files:
        path = tmp_dir / mf.name
        if mf.size is not None and path.is_file() and path.stat().st_size != mf.size:
            short.append(mf.name)
    return short


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Flush every file and directory under ``root`` to disk, so a power cut after the
    show is recorded complete cannot leave a truncated or missing file behind."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            _fsync_path(Path(dirpath) / name)
        _fsync_path(Path(dirpath))


def _is_single_folder_name(name: str) -> bool:
    """One folder name a show may own. Names starting with a dot are shakedown's own
    under a collection folder (`.tmp-`, `.stale-`, `.tagging`; reconcile skips them)."""
    separators = {"/", os.sep} | ({os.altsep} if os.altsep else set())
    return bool(name) and not name.startswith(".") and not any(sep in name for sep in separators)


def _fetch_one_budgeted(
    budget: SourceBudget,
    plugin: SourcePlugin,
    target: PlannedItem,
    archive_root: Path,
    collection: CollectionConfig,
) -> _FetchOutcome:
    """Acquire a per-source budget slot around the whole fetch (SPEC §spec:source-budget).

    The slot is held for the entire ``_fetch_one`` call so it counts as one live
    upstream connection.
    """
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
    # os.rename / shutil.rmtree / plugin downloads below. It must be one folder name:
    # a separator or `..` could escape the archive tree, or (`x/..`) name the
    # collection folder itself, whose other shows the temp-dir sweep would delete.
    if not _is_single_folder_name(desc.identifier):
        log.warning("[%s] rejecting unsafe identifier: %r", desc.identifier, desc.identifier)
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            error=f"unsafe identifier is not a single folder name: {desc.identifier!r}",
        )

    # Trust boundary: manifest file names also come from the remote source and
    # are joined onto the temp dir by the plugin's fetch(). Reject any that would
    # escape the item directory *before* fetch runs — the write happens during
    # fetch, so the post-fetch verify() can't catch an escape (it resolves to the
    # same out-of-tree path). This guards every plugin, not just well-behaved ones.
    unsafe_names = [
        mf.name for mf in desc.manifest.files if not is_within(dest, dest / mf.name)
    ]
    if unsafe_names:
        log.warning("[%s] rejecting unsafe manifest file name(s): %r", desc.identifier, unsafe_names[:5])
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            error=f"unsafe file name(s) escape item directory: {unsafe_names[:5]}",
        )

    # Callers have already finished any swap a crashed run left half done
    # (`_recover_interrupted_swaps`) and routed shows the source refuses away from
    # the fetch. The temp dir is swept per-attempt inside _fetch_with_retries.
    tmp_dir = archive_root / f"{_TMP_PREFIX}{desc.identifier}"
    stale_dir = archive_root / f"{_STALE_PREFIX}{desc.identifier}"

    # Never promote an empty fetch: it would replace the archive copy with an empty
    # folder. Both callers route an empty manifest away first; this is the last line
    # in front of the archive copy.
    if not desc.manifest.files:
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            error=f"{NO_FILES_REASON}; archive copy left untouched",
        )

    fetch = desc.manifest.files if target.fetch is None else target.fetch
    fetch_result = _fetch_with_retries(plugin, desc, tmp_dir, files=fetch)
    if fetch_result.unavailable:
        # Nothing will resume a download the source refuses; the archive copy (if
        # any) is untouched.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        return _FetchOutcome(
            success=False, archive_path=dest, error=fetch_result.error, unavailable=True
        )
    if not fetch_result.success:
        # Leave tmp_dir for next-run cleanup; the archive copy (if any) is untouched.
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            bytes_downloaded=fetch_result.bytes_downloaded,
            error=fetch_result.error,
        )

    # The files the archive copy already holds join the download by hardlink: no
    # bytes are read or hashed, and the swap below keeps them (§spec:quality-selection).
    fetched = {mf.name for mf in fetch}
    _link_kept(dest, tmp_dir, [mf for mf in desc.manifest.files if mf.name not in fetched])

    # Core-owned completeness guard before promotion: route the existence-only
    # check through the plugin's own verify() contract (no byte hashing —
    # SPEC §spec:sync-identity) so a plugin can't commit partial state even if it
    # over-reports success.
    verified = plugin.verify(desc.manifest.files, tmp_dir)
    if not verified.ok:
        return _FetchOutcome(
            success=False,
            archive_path=dest,
            bytes_downloaded=fetch_result.bytes_downloaded,
            error=f"missing after fetch: {verified.missing_files[:5]}",
        )

    # Fill the downloads' missing tags while they are still in the temp dir, so the
    # tagged bytes enter the archive atomically with the show (§spec:tag-filling).
    # Kept files are links to the live archive copy and are never touched here.
    tag_records: dict[str, TagRecord | None] | None = None
    if target.tags is not None:
        tag_records = {}
        for name in sorted(fetched & target.tags.keys()):
            try:
                tag_records[name] = fill_file(tmp_dir / name, target.tags[name], None, None).record
            except TagWriteError as e:
                # The download may be torn: drop it, and the next run fetches it again.
                (tmp_dir / name).unlink(missing_ok=True)
                return _FetchOutcome(
                    success=False,
                    archive_path=dest,
                    bytes_downloaded=fetch_result.bytes_downloaded,
                    error=tag_failure(name, e),
                )

    # Preserve the raw source metadata as a sidecar inside the temp dir *before* the
    # atomic promotion, so it lands in the archive atomically with the media or not at
    # all (§spec:metadata-preservation). It is written by the core, not the plugin, and
    # is deliberately absent from the recorded manifest — change detection stays over
    # media only (§spec:sync-identity). A media file legitimately named metadata.json
    # takes precedence — writing the sidecar would clobber it and desync the manifest.
    if collection.preserve_source_metadata:
        if manifest_reserves_sidecar_name(desc.manifest):
            log.warning(
                "[%s] a manifest file is named %s; skipping metadata sidecar to keep the "
                "archive consistent with its manifest", desc.identifier, METADATA_SIDECAR,
            )
        else:
            write_sidecar(tmp_dir / METADATA_SIDECAR, desc.metadata)

    # Durable before recorded: the caller marks the show complete right after this,
    # so flush the bytes, then the rename itself (the parent directory entry).
    _fsync_tree(tmp_dir)
    _promote_atomically(tmp_dir, dest, stale_dir)
    return _FetchOutcome(
        success=True,
        archive_path=dest,
        bytes_downloaded=fetch_result.bytes_downloaded,
        tag_records=tag_records,
    )


def _link_kept(dest: Path, tmp_dir: Path, kept: Sequence[ManifestFile]) -> None:
    """Hardlink the archive copy's kept files into the temp dir before promotion. A
    kept file missing from the archive is left for verify() to report."""
    for mf in kept:
        src, link = dest / mf.name, tmp_dir / mf.name
        if not src.is_file():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        os.link(src, link)


_STALE_PREFIX = ".stale-"


_TMP_PREFIX = ".tmp-"


def _planned_identifier(planned: PlannedItem) -> str:
    if planned.descriptor is not None:
        return planned.descriptor.identifier
    assert planned.existing is not None  # signal-skipped items carry the DB row
    return planned.existing.identifier


def _sweep_abandoned_tmp_dirs(
    archive_root: Path, *, enumerated: set[str], settled: set[str]
) -> None:
    """Remove temp dirs nothing will resume: those of shows gone upstream, or settled
    this run as unchanged or unavailable. Run only after a complete enumeration. A
    show fetched this run left none; a show whose fetch or metadata lookup failed
    keeps its temp dir so the next run fetches only its missing files."""
    if not archive_root.is_dir():
        return
    for tmp_dir in archive_root.glob(f"{_TMP_PREFIX}*"):
        identifier = tmp_dir.name[len(_TMP_PREFIX):]
        if (identifier not in enumerated or identifier in settled) and tmp_dir.is_dir():
            shutil.rmtree(tmp_dir)


def _recover_swap(stale_dir: Path, dest: Path) -> None:
    """Finish a promotion a crash interrupted. With no live copy, the `.stale-` folder
    holds the only archive copy: move it back. With a live copy, the new copy was
    already installed: discard the old one."""
    if not stale_dir.exists():
        return
    if dest.exists():
        shutil.rmtree(stale_dir)
    else:
        log.warning("restoring %s from an interrupted swap", dest)
        os.rename(stale_dir, dest)


def _recover_interrupted_swaps(archive_root: Path) -> None:
    if not archive_root.is_dir():
        return
    for stale_dir in archive_root.glob(f"{_STALE_PREFIX}*"):
        if stale_dir.is_dir():
            _recover_swap(stale_dir, archive_root / stale_dir.name[len(_STALE_PREFIX):])


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
    # Make the new name durable before the old copy is deleted, so a power cut
    # leaves either the new copy in place or the old one to restore.
    _fsync_path(dest.parent)
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
        # Set when the copy is the best lossy one because lossless files are refused.
        restriction_reason=desc.restriction_reason,
        source_metadata=desc.metadata,
        recorded_manifest=desc.manifest,
        change_signal=desc.change_signal,
        not_playable_reason=desc.not_playable_reason,
    )
    items.upsert(item)
    return item


def _record_download_tags(
    items: ItemRepo,
    source_name: str,
    collection_name: str,
    target: PlannedItem,
    outcome: _FetchOutcome,
) -> None:
    """The downloaded files hold new bytes: their old tag records go, replaced by what
    fetch-time tagging wrote. The show's fingerprint is kept only when every taggable
    file was downloaded and tagged now; otherwise the run's pass over mirrored shows
    (or, with `fill_tags` off, a later opt-in) revisits it (§spec:tag-filling)."""
    assert target.descriptor is not None
    desc = target.descriptor
    repo = TagRepo(items.conn)
    downloaded = {mf.name for mf in (target.fetch if target.fetch is not None else desc.manifest.files)}
    repo.drop_files(source_name, collection_name, desc.identifier, sorted(downloaded))
    for name, record in (outcome.tag_records or {}).items():
        if record is not None:
            repo.put_file(source_name, collection_name, desc.identifier, name, record)
    if target.tags is not None and outcome.tag_records is not None and target.tags.keys() <= downloaded:
        repo.set_fingerprint(source_name, collection_name, desc.identifier, fingerprint(target.tags))
    else:
        repo.clear_fingerprint(source_name, collection_name, desc.identifier)


def _record_back_to_complete(items: ItemRepo, existing: Item, reason: str | None) -> None:
    existing.status = ItemStatus.COMPLETE
    existing.restriction_reason = reason
    existing.last_verified_at = datetime.now()
    # Listed again: no longer removed upstream (§spec:item-lifecycle).
    existing.disappeared_at = None
    items.upsert(existing)


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


def _note_lookup_failure(
    stats: CollectionSyncStats, source_name: str, collection_name: str, failure: LookupFailure
) -> None:
    """List a failed lookup in the run's item failures (§spec:per-item-isolation)."""
    log.warning("[%s/%s] %s: %s", source_name, collection_name, failure.identifier, failure.reason)
    stats.failed += 1
    stats.errors.append(f"{failure.identifier}: {failure.reason}")


def _record_lookup_failure(
    items: ItemRepo,
    source_name: str,
    collection_name: str,
    failure: LookupFailure,
    stats: CollectionSyncStats,
) -> None:
    """A listed item whose lookup failed this run (§spec:per-item-isolation).

    It is listed failed with its reason and retried next run. A new item is recorded
    `failed`; an item already recorded is left exactly as it was — status, manifest,
    and files — so a mirrored show is never touched by a bad lookup
    (§spec:prune-safety). It stays in the run's listing, so it is never `disappeared`.
    """
    _note_lookup_failure(stats, source_name, collection_name, failure)
    if items.get(source_name, collection_name, failure.identifier) is not None:
        return
    now = datetime.now()
    items.upsert(
        Item(
            source_name=source_name,
            collection_name=collection_name,
            identifier=failure.identifier,
            status=ItemStatus.FAILED,
            discovered_at=now,
            last_verified_at=now,
            restriction_reason=failure.reason,
        )
    )


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
    source_name: str,
    collection: CollectionConfig,
    existing: Item,
) -> None:
    """Handle an item that vanished from the source's enumeration (§spec:item-lifecycle).

    Default: retain the local files and flag the item `disappeared` — an upstream
    takedown never deletes the user's copy. With `prune_disappeared: true`: delete
    the archive files but *retain the DB record* marked `pruned`, so `shakedown status`
    can still report the takedown; the run's staging pass then removes its library
    links, so its bytes are freed (§spec:library-cleanup).

    Either way the record keeps the date the show was first found missing: set now
    unless it was already `disappeared`, whose date (or lack of one, for a show gone
    before dates were recorded) stands (§spec:item-lifecycle).
    """
    now = datetime.now()
    if existing.status != ItemStatus.DISAPPEARED:
        existing.disappeared_at = now
    existing.last_verified_at = now
    if collection.prune_disappeared:
        if existing.archive_path and existing.archive_path.exists():
            shutil.rmtree(existing.archive_path)
        existing.status = ItemStatus.PRUNED
        existing.archive_path = None
        items.upsert(existing)
        # The tagged bytes are gone with the archive copy (§spec:tag-filling).
        TagRepo(items.conn).delete_show(source_name, collection.name, existing.identifier)
        return
    existing.status = ItemStatus.DISAPPEARED
    items.upsert(existing)


# --- single-show runs: `item refetch` and `verify --deep --reconform` ---------------


class UnknownShow(Exception):
    """`item refetch` named an identifier that no configured collection records."""


def refetch_item(config: Config, identifier: str) -> int:
    """`item refetch`: a single-show run of ``identifier`` in each configured collection
    that records it, under the writer lock (§spec:single-show-runs). Raises UnknownShow
    when none does."""
    return run_exclusive(config, lambda: _refetch_item(config, identifier))


def _refetch_item(config: Config, identifier: str) -> int:
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        items = ItemRepo(conn)
        targets: dict[tuple[str, str], SingleShows] = {
            (source.name, collection.name): {identifier: None}
            for source in config.sources
            for collection in source.collections
            if items.get(source.name, collection.name, identifier) is not None
        }
    if not targets:
        raise UnknownShow(identifier)
    return run_single_shows(config, targets, RunKind.REFETCH)


# The shows a single-show run acts on: identifier -> the archive files to repair, or
# None to download every selected file again (a refetch).
SingleShows = dict[str, frozenset[str] | None]


def run_single_shows(
    config: Config, targets: dict[tuple[str, str], SingleShows], kind: RunKind
) -> int:
    """Run the sync pipeline for exactly these shows, keyed by (source, collection):
    one run per collection, marked ``kind`` (§spec:single-show-runs). Each show is
    looked up on its own — the collection is never listed — and every lookup and
    download draws from the shared source budget (§spec:source-budget). The caller
    holds the writer lock. Returns 0 when every show was fetched, else 1."""
    work = [
        (source, collection, registry.for_source(source), targets[key])
        for source in config.sources
        for collection in source.collections
        if (key := (source.name, collection.name)) in targets and targets[key]
    ]
    if not work:
        return 0
    budgets = source_budgets(config, [(s, p) for s, _, p, _ in work])
    failed = 0
    for source, collection, plugin, shows in work:
        try:
            stats = _single_show_collection(
                config, source, collection, plugin, shows, kind, budgets[source.name]
            )
        except Exception:
            log.exception("%s failed for %s/%s", kind.value, source.name, collection.name)
            failed += 1
            continue
        log.info(
            "%s/%s: %s new=%d updated=%d failed=%d unavailable=%d bytes=%d",
            source.name, collection.name, kind.value, stats.new, stats.updated,
            stats.failed, stats.unavailable, stats.bytes_downloaded,
        )
        log_separations(source.name, collection.name, stats.separated)
        if stats.failed > 0:
            failed += 1
    return 0 if failed == 0 else 1


def _plan_repair(
    identifier: str,
    existing: Item | None,
    details: ItemDetails,
    drifted: frozenset[str],
    items: ItemRepo,
    stats: CollectionSyncStats,
) -> tuple[Manifest, tuple[ManifestFile, ...]] | None:
    """A reconform's manifest and downloads for one show: each drifted archive file the
    source still serves — a picked copy or an archive-only one outside the current
    selection — and nothing else (§spec:verify-drift). The rest of the archive copy is
    kept as recorded.

    A drifted file the source no longer serves (no longer listed, or refused) cannot
    be repaired: it is listed in the run's errors and never causes a download. One the
    archive no longer holds leaves the record, as any archive-only file gone from disk
    does (§spec:quality-selection). Returns None when there is nothing to download;
    the caller counts the show failed once if any file was not repaired."""
    if existing is None or existing.recorded_manifest is None or existing.archive_path is None:
        return None
    listed = {f.name: f for f in details.files}
    folder = existing.archive_path
    files: list[ManifestFile] = []
    fetch: list[ManifestFile] = []
    for mf in existing.recorded_manifest.files:
        if mf.name not in drifted:
            files.append(mf)
            continue
        source_file = listed.get(mf.name)
        if source_file is None or source_file.refused:
            stats.errors.append(
                f"{identifier}: {mf.name} drifted and the source no longer serves it;"
                " not repaired"
            )
            if (folder / mf.name).is_file():
                files.append(mf)
            continue
        current = replace(
            mf, size=source_file.size, md5=source_file.md5,
            title=source_file.title, track=source_file.track,
        )
        files.append(current)
        fetch.append(current)
    manifest = Manifest(files=tuple(files))
    if not fetch:
        if manifest != existing.recorded_manifest:
            existing.recorded_manifest = manifest
            items.upsert(existing)
        return None
    return manifest, tuple(fetch)


def _fail_refused(stats: CollectionSyncStats, identifier: str, reason: str | None) -> None:
    """A show the source refuses in a single-show run: recorded unavailable, as a sync
    would, and failed in the run — it was asked for and nothing was fetched
    (operator decision, 2026-11-10)."""
    stats.failed += 1
    stats.errors.append(f"{identifier}: unavailable: {reason}")


def _single_show_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    shows: SingleShows,
    kind: RunKind,
    budget: SourceBudget,
) -> CollectionSyncStats:
    """One collection's single-show run, recorded like a sync's: a run record and an
    outcome, closed even if the run crashes."""
    started = datetime.now()
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)
    outcomes = OperationOutcomeRepo(conn)
    run = runs.start(source.name, collection.name, started, kind=kind)
    outcome = outcomes.start(
        OperationType(kind.value),
        source.name,
        collection.name,
        started,
        phase="fetch",
        safe_next_action=f"restore source access and rerun {_RERUN[kind]}",
    )
    stats = CollectionSyncStats()
    try:
        _single_show_phases(
            config, source, collection, plugin, shows, kind, budget,
            items, runs, run, outcomes, outcome, stats, started,
        )
    except Exception as e:
        _close_crashed(runs, run, outcomes, outcome, stats, collection, e, kind)
        raise
    return stats


def _single_show_phases(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    shows: SingleShows,
    kind: RunKind,
    budget: SourceBudget,
    items: ItemRepo,
    runs: RunRepo,
    run: Run,
    outcomes: OperationOutcomeRepo,
    outcome: OperationOutcome,
    stats: CollectionSyncStats,
    started: datetime,
) -> None:
    """Look up, fetch, stage, hand off, and record — the sync's phases for just these
    shows, downloading even what the manifest says is unchanged: that is what a
    refetch or reconform asks for. A refetch downloads every selected file; a
    reconform only the drifted files (§spec:verify-drift). A show whose lookup fails,
    or that the source's answer does not hold, is failed with the reason and left
    exactly as it was — record, archive copy, and library placement."""
    archive_root = config.archive_root / source.name / collection.name
    # Finish any swap or tag write a crashed run left half done, so each show's
    # archive copy is back in place even when this run does not fetch it.
    _recover(config, source.name, collection)
    stats.discovered = len(shows)
    placement = LibraryPlacement(config, collection, source.name, items)
    placement.begin()

    planner = (
        TagPlanner(items, source.name, collection.name) if collection.fill_tags else None
    )
    unrepaired: set[str] = set()
    with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as fetch_pool:
        fetches: dict[Future[_FetchOutcome], PlannedItem] = {}
        for identifier, details in look_up_rows(plugin, list(shows), budget):
            if isinstance(details, LookupFailure):
                _note_lookup_failure(stats, source.name, collection.name, details)
                continue
            found = describe(identifier, details)
            if found.is_restricted:
                # The source refuses the show: recorded unavailable, as a sync would.
                log.info(
                    "[%s/%s] %s: unavailable: %s",
                    source.name, collection.name, found.identifier, found.restriction_reason,
                )
                _record_unavailable(items, source.name, collection.name, found)
                stats.unavailable += 1
                _fail_refused(stats, found.identifier, found.restriction_reason)
                continue
            existing = items.get(source.name, collection.name, identifier)
            drifted = shows[identifier]
            if drifted is None:
                if not found.manifest.files:
                    _note_lookup_failure(
                        stats, source.name, collection.name,
                        LookupFailure(
                            identifier, f"{NO_FILES_REASON}; archive copy left untouched"
                        ),
                    )
                    continue
                # Every selected file is downloaded again; archive-only copies the
                # rule no longer selects stay (§spec:quality-selection).
                manifest, fetch = plan_files(found.manifest, existing, refetch=True)
            else:
                errors_before = len(stats.errors)
                repair = _plan_repair(identifier, existing, details, drifted, items, stats)
                if len(stats.errors) > errors_before:
                    unrepaired.add(identifier)
                if repair is None:
                    continue
                manifest, fetch = repair
            action = (
                PlanAction.CHANGED_UPSTREAM
                if existing is not None and has_archive_copy(existing)
                else PlanAction.NEW
            )
            planned = PlannedItem(replace(found, manifest=manifest), existing, action, fetch)
            if planner is not None:
                # A reconform restores the drifted files' source bytes, then fills their
                # gaps again (§spec:verify-drift).
                planned.tags = planner.plan(identifier, found.metadata, manifest)
            archive_root.mkdir(parents=True, exist_ok=True)
            fut = fetch_pool.submit(
                _fetch_one_budgeted, budget, plugin, planned, archive_root, collection
            )
            fetches[fut] = planned
        for fut in as_completed(fetches):
            # The sync's own fetch handling: record, link into the library, count.
            failed_before = stats.failed
            _process_fetch_result(
                fut, fetches[fut], source, collection, items, stats, placement,
                refused_fails=True,
            )
            if stats.failed > failed_before:
                unrepaired.discard(_planned_identifier(fetches[fut]))
    # A show with a drifted file left unrepaired is failed once, unless its download
    # already failed it.
    stats.failed += len(unrepaired)

    # Each show fetched here went through the sync's fetch, record, and link path
    # above, and is staged with the same ownership and cleanup pass below
    # (§spec:library-ownership, §spec:library-cleanup). On a `fill_tags` collection the
    # downloads were tagged before entering the archive (a reconform refills the gaps
    # of the source bytes it restored), and the pass below follows any clash change
    # the fetched shows brought (§spec:tag-filling).
    tag_issues = _fill_tags(config, source.name, collection, items)
    report = placement.finish()
    _apply_placement_report(stats, report, tag_issues)

    # The collection's handoff, exactly as a sync sends it, including shows still
    # pending from earlier runs (§spec:durable-handoff).
    finished = datetime.now()
    _fire_notifications(
        config, items, source, collection, stats, started, finished, report.folder_of
    )
    _finish_run(runs, run, stats, finished)
    _finish_outcome(outcomes, outcome, stats, collection, finished, kind, shows)
