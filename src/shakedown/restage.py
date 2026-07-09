"""Rebuild the library staging tree from the DB. PRD §10.

Used after a `library_layout` change or after manually wiping `/data/library/`.
Never downloads anything — the archive layer is the source of truth.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import ItemStatus, OperationStatus, OperationType
from shakedown.staging import stage_item
from shakedown.state import ItemRepo, OperationOutcomeRepo

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
    outcomes = OperationOutcomeRepo(conn)
    failed = 0

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue

            started = datetime.now()
            outcome = outcomes.start(
                OperationType.RESTAGE,
                source.name,
                collection.name,
                started,
                phase="clear-library",
                safe_next_action="fix storage/path/layout and rerun restage",
            )
            scope = config.library_root / source.name / collection.name
            if scope.exists():
                try:
                    shutil.rmtree(scope)
                except OSError as e:
                    failed += 1
                    finished = datetime.now()
                    outcomes.finish(
                        outcome,
                        OperationStatus.FAILED_BEFORE_COMPLETION,
                        finished,
                        phase="clear-library",
                        affected_path=str(scope),
                        completed_work={"items_staged": 0, "links_created": 0, "collisions": 0},
                        preservation_context="archive recordings retained",
                        deletion_context="library staging tree is disposable",
                        safe_next_action="fix storage/path/layout and rerun restage",
                        errors=[str(e)],
                    )
                    log.warning("[%s/%s] restage failed clearing %s: %s", source.name, collection.name, scope, e)
                    continue

            linked = 0
            collisions = 0
            staged_paths: set = set()
            stage_failed = False
            for item in items.list_for_collection(source.name, collection.name):
                if item.status != ItemStatus.COMPLETE or item.recorded_manifest is None:
                    continue
                try:
                    result = stage_item(
                        config, collection, source.name, item, item.recorded_manifest,
                        staged_paths=staged_paths,
                    )
                except OSError as e:
                    failed += 1
                    stage_failed = True
                    finished = datetime.now()
                    outcomes.finish(
                        outcome,
                        OperationStatus.FAILED_BEFORE_COMPLETION,
                        finished,
                        phase="stage",
                        affected_item=item.identifier,
                        affected_path=str(item.archive_path) if item.archive_path else None,
                        completed_work={
                            "items_staged": len(staged_paths),
                            "links_created": linked,
                            "collisions": collisions,
                        },
                        preservation_context="archive recordings retained",
                        deletion_context="library staging tree is disposable",
                        safe_next_action="fix storage/path/layout and rerun restage",
                        errors=[str(e)],
                    )
                    log.warning(
                        "[%s/%s] restage failed staging %s: %s",
                        source.name, collection.name, item.identifier, e,
                    )
                    break
                linked += result.linked
                collisions += len(result.collisions)
                for c in result.collisions:
                    log.warning("[%s/%s] %s", source.name, collection.name, c)
            if stage_failed:
                continue
            log.info(
                "[%s/%s] restage: linked=%d collisions=%d",
                source.name, collection.name, linked, collisions,
            )
            if collisions:
                failed += 1
                outcomes.finish(
                    outcome,
                    OperationStatus.COMPLETED_WITH_ITEM_ISSUES,
                    datetime.now(),
                    phase="stage",
                    completed_work={
                        "items_staged": len(staged_paths),
                        "links_created": linked,
                        "collisions": collisions,
                    },
                    preservation_context="archive recordings retained",
                    deletion_context="library staging tree is disposable",
                    safe_next_action="fix library layout and rerun restage",
                )
            else:
                finished = datetime.now()
                outcomes.finish(
                    outcome,
                    OperationStatus.COMPLETED,
                    finished,
                    phase="stage",
                    completed_work={
                        "items_staged": len(staged_paths),
                        "links_created": linked,
                        "collisions": collisions,
                    },
                    deletion_context="library staging tree is disposable",
                )
                outcomes.resolve_actionable(
                    OperationType.RESTAGE, source.name, collection.name, finished
                )
    return 0 if failed == 0 else 1
