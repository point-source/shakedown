"""shakedown reconcile: rebuild state DB from archive tree + source manifests.

Disaster recovery / migration from a manual setup. PRD §10, §13. Never re-downloads —
falls back to source for the manifest, but trusts on-disk presence as the source of
truth for "do I have this?". Tolerates an unreachable source: items on disk are still
recorded, just without a recorded_manifest.

Listing and lookups follow the same rules as sync (SPEC §spec:per-item-isolation): only
a complete listing that omits an item marks it `disappeared`; an item whose lookup
fails keeps what it had and is reported.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect, transaction
from shakedown.discovery import (
    ItemDescriptor,
    LookupFailure,
    SourceBudget,
    source_budgets,
    stream_items,
)
from shakedown.lock import run_exclusive
from shakedown.models import (
    Item,
    ItemStatus,
    Manifest,
    ManifestFile,
    OperationStatus,
    OperationType,
)
from shakedown.plugins import registry
from shakedown.plugins.base import SourcePlugin
from shakedown.staging import METADATA_SIDECAR
from shakedown.state import ItemRepo, OperationOutcomeRepo
from shakedown.sync import NO_FILES_REASON, plan_files

log = logging.getLogger(__name__)


def run_reconcile(config: Config) -> int:
    """For every (source, collection), walk the archive tree and rebuild item rows."""
    return run_exclusive(config, lambda: _run_reconcile(config))


def _run_reconcile(config: Config) -> int:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    outcomes = OperationOutcomeRepo(conn)
    failed = 0

    for source in config.sources:
        plugin = registry.for_source(source)
        budget = source_budgets(config, [(source, plugin)])[source.name]
        for collection in source.collections:
            failed += _reconcile_collection(
                config, source, collection, plugin, budget, items, outcomes, conn
            )
    return 0 if failed == 0 else 1


def _reconcile_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    plugin: SourcePlugin,
    budget: SourceBudget,
    items: ItemRepo,
    outcomes: OperationOutcomeRepo,
    conn,
) -> int:
    started = datetime.now()
    outcome = outcomes.start(
        OperationType.RECONCILE,
        source.name,
        collection.name,
        started,
        phase="walk-archive",
        safe_next_action="restore source access and rerun reconcile",
    )
    archive_root = config.archive_root / source.name / collection.name
    on_disk_ids: set[str] = set()
    if archive_root.is_dir():
        on_disk_ids = {
            p.name for p in archive_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }

    # Look up only what is on disk; the listing alone says what is still upstream.
    listed: set[str] = set()
    descriptors_by_id: dict[str, ItemDescriptor] = {}
    lookup_failures: list[LookupFailure] = []
    source_error: Exception | None = None
    try:
        for found in stream_items(
            plugin, collection, budget, listed,
            look_up=lambda row: row.identifier in on_disk_ids,
        ):
            if isinstance(found, LookupFailure):
                lookup_failures.append(found)
                log.warning(
                    "[%s/%s] %s: %s", source.name, collection.name, found.identifier, found.reason
                )
            else:
                descriptors_by_id[found.identifier] = found
    except Exception as e:
        source_error = e
        log.exception(
            "[%s/%s] source listing failed; reconciling on-disk items without manifests",
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

            # What the archive copy holds when the source gives no files to go by:
            # the record's files, or with no record every file on disk.
            held = (
                existing.recorded_manifest
                if existing is not None and existing.recorded_manifest is not None
                else Manifest(files=_files_on_disk(archive_path, set(), library=True))
            )
            if desc is not None and (desc.is_restricted or not desc.manifest.files):
                # Refused or no audio listed: unavailable with the copy kept, as sync
                # records it (§spec:failure-behavior).
                item = Item(
                    source_name=source.name,
                    collection_name=collection.name,
                    identifier=identifier,
                    status=ItemStatus.UNAVAILABLE,
                    archive_path=archive_path,
                    discovered_at=existing.discovered_at if existing else now,
                    downloaded_at=existing.downloaded_at if existing else now,
                    last_verified_at=now,
                    restriction_reason=desc.restriction_reason or NO_FILES_REASON,
                    source_metadata=desc.metadata,
                    recorded_manifest=held,
                )
            elif desc is not None:
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
                    # Set for a lossless-refused copy, so `status` keeps listing it.
                    restriction_reason=desc.restriction_reason,
                    source_metadata=desc.metadata,
                    recorded_manifest=_with_files_on_disk(desc.manifest, archive_path, existing),
                    not_playable_reason=desc.not_playable_reason,
                )
            else:
                # On disk, but no description this run. Prior manifest/metadata are
                # kept either way, so later syncs still classify correctly.
                #   (a) listing failed, or listed but its lookup failed → keep the
                #       prior status: neither says the item left upstream;
                #   (b) a complete listing omits it → DISAPPEARED (PRD §6) when we had
                #       a row. With no row there is no prior knowledge → COMPLETE.
                if existing is None:
                    status = ItemStatus.COMPLETE
                elif source_error is not None or identifier in listed:
                    status = existing.status
                else:
                    status = ItemStatus.DISAPPEARED
                item = Item(
                    source_name=source.name,
                    collection_name=collection.name,
                    identifier=identifier,
                    status=status,
                    archive_path=archive_path,
                    discovered_at=existing.discovered_at if existing else now,
                    downloaded_at=existing.downloaded_at if existing else now,
                    last_verified_at=now,
                    restriction_reason=existing.restriction_reason if existing else None,
                    source_metadata=existing.source_metadata if existing else {},
                    recorded_manifest=held,
                    change_signal=existing.change_signal if existing else None,
                    not_playable_reason=existing.not_playable_reason if existing else None,
                )
            items.upsert(item)

    log.info(
        "[%s/%s] reconcile recorded %d items", source.name, collection.name, len(on_disk_ids)
    )
    finished = datetime.now()
    completed_work = {
        "items_on_disk": len(on_disk_ids),
        "items_with_source_manifest": len(on_disk_ids & descriptors_by_id.keys()),
    }
    if source_error is not None:
        outcomes.finish(
            outcome,
            OperationStatus.STALE_ENUMERATION,
            finished,
            phase="fetch-manifests",
            completed_work=completed_work,
            preservation_context="local recordings retained",
            deletion_context="no deletion requested",
            safe_next_action="restore source access and rerun reconcile",
            errors=[str(source_error)],
        )
        return 1
    if lookup_failures:
        outcomes.finish(
            outcome,
            OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
            finished,
            phase="fetch-manifests",
            affected_item=lookup_failures[0].identifier,
            completed_work=completed_work,
            preservation_context="local recordings retained",
            deletion_context="no deletion requested",
            safe_next_action="restore source access and rerun reconcile",
            errors=[f"{f.identifier}: {f.reason}" for f in lookup_failures],
        )
        return 1
    outcomes.finish(
        outcome,
        OperationStatus.COMPLETED,
        finished,
        phase="write-state",
        completed_work=completed_work,
        preservation_context="local recordings retained",
        deletion_context="no deletion requested",
    )
    outcomes.resolve_actionable(
        OperationType.RECONCILE, source.name, collection.name, finished
    )
    return 0


def _with_files_on_disk(selected: Manifest, archive_path: Path, existing: Item | None) -> Manifest:
    """The selection plus every other file in the show's archive copy, recorded as
    archive-only so a later download keeps it: a rebuild never turns files the rule
    no longer selects into files the next fetch deletes (§spec:quality-selection).
    Files the record already knows keep their recorded size and checksum; others get
    their size from disk and no checksum (reading the bytes is `verify --deep`'s job).
    """
    merged, _ = plan_files(selected, existing)
    extra = _files_on_disk(archive_path, {f.name for f in merged.files}, library=False)
    return Manifest(files=tuple(sorted(merged.files + extra, key=lambda f: f.name)))


def _files_on_disk(
    archive_path: Path, known: set[str], *, library: bool
) -> tuple[ManifestFile, ...]:
    """Every file in the archive copy not named in ``known`` (nor the metadata
    sidecar), with its size from disk and no checksum."""
    skip = known | {METADATA_SIDECAR}
    return tuple(
        ManifestFile(rel, path.stat().st_size, None, library=library)
        for path in sorted(archive_path.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and (rel := path.relative_to(archive_path).as_posix()) not in skip
    )
