"""Rebuild the library staging tree from the DB. PRD §10.

Used after a `library_layout` change or after manually wiping `/data/library/`.
Never downloads anything — the archive layer is the source of truth. Removes only
files shakedown placed (§spec:library-cleanup).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from shakedown.activity import ActivityKind, ActivityRecorder, Phase, Progress
from shakedown.config import CollectionConfig, Config
from shakedown.db import connect
from shakedown.lock import run_exclusive
from shakedown.models import OperationOutcome, OperationStatus, OperationType
from shakedown.placement import LibraryPlacement, log_separations
from shakedown.state import ItemRepo, OperationOutcomeRepo
from shakedown.tagging import recover_interrupted_tagging

log = logging.getLogger(__name__)

# Next step shown in `status` after a restage that left per-show issues.
RESTAGE_ISSUES_ACTION = "fix the listed show issues and rerun restage"


def run_restage(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
) -> int:
    """Put every show with an archive copy at its current library folder, per
    (source, collection) scope, removing only what shakedown placed."""
    return run_exclusive(
        config, lambda: _run_restage(config, source_filter, collection_filter)
    )


def _run_restage(
    config: Config, source_filter: str | None, collection_filter: str | None
) -> int:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    with ActivityRecorder(config.state_db) as recorder:  # type: ignore[arg-type]
        return _restage_collections(config, source_filter, collection_filter, conn, recorder)


def _restage_collections(
    config: Config,
    source_filter: str | None,
    collection_filter: str | None,
    conn: sqlite3.Connection,
    recorder: ActivityRecorder,
) -> int:
    items = ItemRepo(conn)
    outcomes = OperationOutcomeRepo(conn)
    failed = 0

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue

            # A restage lists nothing: it reports the shows it stages (§spec:run-activity).
            progress = recorder.open_entry(source.name, collection.name, ActivityKind.RESTAGE)
            outcome: OperationOutcome | None = None
            try:
                outcome = _restage_collection(
                    config, source.name, collection, items, outcomes, progress
                )
            finally:
                recorder.end(
                    progress,
                    outcome.status if outcome is not None
                    else OperationStatus.FAILED_BEFORE_COMPLETION,
                )
            if outcome.status != OperationStatus.COMPLETED:
                failed += 1
    return 0 if failed == 0 else 1


def _restage_collection(
    config: Config,
    source_name: str,
    collection: CollectionConfig,
    items: ItemRepo,
    outcomes: OperationOutcomeRepo,
    progress: Progress,
) -> OperationOutcome:
    """Restage one collection and close its outcome."""
    started = datetime.now()
    outcome = outcomes.start(
        OperationType.RESTAGE,
        source_name,
        collection.name,
        started,
        phase="plan",
        safe_next_action="fix storage/path/layout and rerun restage",
    )
    try:
        # A tag write a crash cut short is undone before anything is linked
        # (§spec:tag-filling).
        progress.phase(Phase.TAGGING)
        recover_interrupted_tagging(config.archive_root / source_name / collection.name)
        progress.phase(Phase.STAGING)
        report = LibraryPlacement(
            config, collection, source_name, items, progress=progress
        ).finish()
        progress.failed(len(report.issues))
        progress.phase(Phase.FINISHING)
    except OSError as e:
        outcomes.finish(
            outcome,
            OperationStatus.FAILED_BEFORE_COMPLETION,
            datetime.now(),
            phase="stage",
            preservation_context="archive recordings retained",
            deletion_context="removed only files shakedown placed",
            safe_next_action="fix storage/path/layout and rerun restage",
            errors=[str(e)],
        )
        log.warning("[%s/%s] restage failed: %s", source_name, collection.name, e)
        return outcome

    if report.render_error is not None:
        identifier, message = report.render_error
        outcomes.finish(
            outcome,
            OperationStatus.FAILED_BEFORE_COMPLETION,
            datetime.now(),
            phase="plan",
            affected_item=identifier,
            completed_work={"items_staged": 0, "links_created": 0, "files_removed": 0},
            preservation_context="archive recordings and library tree retained",
            deletion_context="nothing deleted",
            safe_next_action="fix library layout and rerun restage",
            errors=[message],
        )
        log.warning(
            "[%s/%s] restage stopped before changing the library: %s: %s",
            source_name, collection.name, identifier, message,
        )
        return outcome

    completed_work = {
        "items_staged": len(report.folder_of),
        "links_created": report.links_created,
        "files_removed": report.files_removed,
        "separations": report.separations,
    }
    log.info(
        "[%s/%s] restage: staged=%d linked=%d removed=%d separated=%d issues=%d",
        source_name, collection.name, len(report.folder_of), report.links_created,
        report.files_removed, report.separations, len(report.issues),
    )
    log_separations(source_name, collection.name, report.separations)
    finished = datetime.now()
    if report.issues:
        errors = [line for lines in report.issues.values() for line in lines]
        outcomes.finish(
            outcome,
            OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
            finished,
            phase="stage",
            affected_item=next(iter(report.issues)),
            completed_work=completed_work,
            preservation_context="archive recordings retained",
            deletion_context="removed only files shakedown placed",
            safe_next_action=RESTAGE_ISSUES_ACTION,
            errors=errors,
        )
    else:
        outcomes.finish(
            outcome,
            OperationStatus.COMPLETED,
            finished,
            phase="stage",
            completed_work=completed_work,
            deletion_context="removed only files shakedown placed",
        )
        outcomes.resolve_actionable(
            OperationType.RESTAGE, source_name, collection.name, finished
        )
    return outcome
