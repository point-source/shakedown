"""Rebuild the library staging tree from the DB. PRD §10.

Used after a `library_layout` change or after manually wiping `/data/library/`.
Never downloads anything — the archive layer is the source of truth.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime

from shakedown.config import CollectionConfig, Config
from shakedown.db import connect
from shakedown.lock import run_exclusive
from shakedown.models import Item, OperationStatus, OperationType
from shakedown.staging import UnsafeLibraryPathError, stage_item, staging_dir_for
from shakedown.state import ItemRepo, OperationOutcomeRepo
from shakedown.utils.templates import TemplateError

log = logging.getLogger(__name__)


def run_restage(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
) -> int:
    """Wipe and rebuild the library tree under each (source, collection) scope."""
    return run_exclusive(
        config, lambda: _run_restage(config, source_filter, collection_filter)
    )


def _run_restage(
    config: Config, source_filter: str | None, collection_filter: str | None
) -> int:
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
                phase="plan",
                safe_next_action="fix storage/path/layout and rerun restage",
            )
            # Every show whose archive copy is on disk, whatever its status: a show
            # marked disappeared or unavailable keeps its files, so it keeps its
            # library folder, exactly as sync leaves it.
            to_stage = [
                item for item in items.list_for_collection(source.name, collection.name)
                if item.recorded_manifest is not None
                and item.archive_path is not None
                and item.archive_path.is_dir()
            ]
            render_error = _first_render_error(config, collection, source.name, to_stage)
            if render_error is not None:
                identifier, err = render_error
                failed += 1
                outcomes.finish(
                    outcome,
                    OperationStatus.FAILED_BEFORE_COMPLETION,
                    datetime.now(),
                    phase="plan",
                    affected_item=identifier,
                    completed_work={"items_staged": 0, "links_created": 0, "collisions": 0},
                    preservation_context="archive recordings and library tree retained",
                    deletion_context="nothing deleted",
                    safe_next_action="fix library layout and rerun restage",
                    errors=[str(err)],
                )
                log.warning(
                    "[%s/%s] restage stopped before clearing the library: %s: %s",
                    source.name, collection.name, identifier, err,
                )
                continue

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
            refused: list[str] = []
            staged_paths: set = set()
            stage_failed = False
            for item in to_stage:
                assert item.recorded_manifest is not None  # filtered into to_stage
                try:
                    result = stage_item(
                        config, collection, source.name, item, item.recorded_manifest,
                        staged_paths=staged_paths,
                    )
                except UnsafeLibraryPathError as e:
                    # One item's crafted metadata fails only that item.
                    refused.append(f"{item.identifier}: {e}")
                    log.warning("[%s/%s] %s: %s", source.name, collection.name, item.identifier, e)
                    continue
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
                "[%s/%s] restage: linked=%d collisions=%d refused=%d",
                source.name, collection.name, linked, collisions, len(refused),
            )
            if collisions or refused:
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
                        "refused_paths": len(refused),
                    },
                    preservation_context="archive recordings retained",
                    deletion_context="library staging tree is disposable",
                    safe_next_action="fix library layout and rerun restage",
                    errors=refused,
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


def _first_render_error(
    config: Config, collection: CollectionConfig, source_name: str, to_stage: list[Item]
) -> tuple[str, TemplateError] | None:
    """Render every item's library folder before the library is cleared, so a layout
    that cannot render leaves the existing tree in place. Returns the first failure.

    Config load already rejects unknown filters; this backs that up for any render
    failure it misses, because a failure after the clear would empty the library."""
    for item in to_stage:
        assert item.archive_path is not None  # filtered into to_stage
        try:
            staging_dir_for(
                config, collection, source_name, item.source_metadata, item.archive_path
            )
        except TemplateError as e:
            return item.identifier, e
        except UnsafeLibraryPathError:
            continue  # refused per item while staging; the other items still link
    return None
