"""shakedown verify [--deep] [--reconform]. PRD §5, §10.

This is the *only* place in the codebase that hashes on-disk bytes. Sync never
does. `--deep` is operator-invoked; never scheduled.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click

from shakedown.config import Config
from shakedown.db import connect, transaction
from shakedown.models import Item, ItemStatus, Manifest
from shakedown.plugins import registry
from shakedown.plugins.base import ItemDescriptor
from shakedown.state import DriftRepo, ItemRepo
from shakedown.sync import CollectionSyncStats, PlanAction, PlannedItem, _execute_fetch_and_stage

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


@dataclass
class DriftRecord:
    source: str
    collection: str
    identifier: str
    file_name: str
    expected_md5: str | None
    observed_md5: str | None


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _scan_drift(item: Item) -> list[DriftRecord]:
    """Hash every recorded-manifest file; report mismatches and missing files."""
    drifts: list[DriftRecord] = []
    if item.recorded_manifest is None or item.archive_path is None:
        return drifts
    for mf in item.recorded_manifest.files:
        on_disk = item.archive_path / mf.name
        if not on_disk.is_file():
            drifts.append(
                DriftRecord(
                    source=item.source_name,
                    collection=item.collection_name,
                    identifier=item.identifier,
                    file_name=mf.name,
                    expected_md5=mf.md5,
                    observed_md5=None,
                )
            )
            continue
        if mf.md5 is None:
            continue  # source didn't publish a checksum; nothing to compare
        observed = _md5_file(on_disk)
        if observed != mf.md5:
            drifts.append(
                DriftRecord(
                    source=item.source_name,
                    collection=item.collection_name,
                    identifier=item.identifier,
                    file_name=mf.name,
                    expected_md5=mf.md5,
                    observed_md5=observed,
                )
            )
    return drifts


def run_verify(
    config: Config,
    *,
    source_filter: str | None,
    collection_filter: str | None,
    deep: bool,
    reconform: bool,
    list_drift: bool,
    assume_yes: bool,
) -> int:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    drift_repo = DriftRepo(conn)

    all_drift: list[DriftRecord] = []
    missing_summary: dict[tuple[str, str], int] = {}

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue

            for item in items.list_for_collection(source.name, collection.name):
                if item.status != ItemStatus.COMPLETE:
                    continue
                if not deep:
                    # cheap check: existence only, via plugin
                    plugin = registry.for_source(source)
                    desc = ItemDescriptor(
                        identifier=item.identifier,
                        manifest=item.recorded_manifest or Manifest(files=()),
                        metadata=item.source_metadata,
                    )
                    result = plugin.verify(desc, item.archive_path)  # type: ignore[arg-type]
                    if not result.ok:
                        missing_summary[(source.name, collection.name)] = (
                            missing_summary.get((source.name, collection.name), 0)
                            + len(result.missing_files)
                        )
                else:
                    drifts = _scan_drift(item)
                    all_drift.extend(drifts)
                    with transaction(conn):
                        # Refresh drift table for this item.
                        if item.recorded_manifest is not None:
                            for mf in item.recorded_manifest.files:
                                drift_repo.clear(
                                    item.source_name, item.collection_name,
                                    item.identifier, mf.name,
                                )
                        for d in drifts:
                            drift_repo.record(
                                d.source, d.collection, d.identifier, d.file_name,
                                d.observed_md5, d.expected_md5, datetime.now(),
                            )

    if not deep:
        if missing_summary:
            for (s, c), n in missing_summary.items():
                click.echo(f"{s}/{c}: {n} expected files missing on disk")
            return 1
        click.echo("verify: all expected files present")
        return 0

    # --deep summary
    if not all_drift:
        click.echo("verify --deep: 0 files drifted")
        return 0

    click.echo(f"verify --deep: {len(all_drift)} files drifted")
    if list_drift:
        for d in all_drift:
            click.echo(f"  {d.source}/{d.collection}/{d.identifier}: {d.file_name}")
            click.echo(f"     expected md5={d.expected_md5} observed={d.observed_md5}")

    if not reconform:
        return 0

    if not assume_yes:
        click.echo(
            f"--reconform will overwrite {len(all_drift)} drifted files with upstream bytes."
        )
        if not click.confirm("Proceed?", default=False):
            click.echo("aborted")
            return 0

    return _reconform(config, items, all_drift)


def _reconform(config: Config, items: ItemRepo, drifts: list[DriftRecord]) -> int:
    """Re-fetch items that have any drifted file. Whole item, not per-file."""
    by_collection: dict[tuple[str, str], set[str]] = {}
    for d in drifts:
        by_collection.setdefault((d.source, d.collection), set()).add(d.identifier)

    failed = 0
    for source in config.sources:
        plugin = registry.for_source(source)
        for collection in source.collections:
            ids = by_collection.get((source.name, collection.name))
            if not ids:
                continue
            log.info(
                "reconform: re-fetching %d items in %s/%s",
                len(ids), source.name, collection.name,
            )
            descriptors_by_id: dict[str, ItemDescriptor] = {}
            for desc in plugin.discover(collection):
                if desc.identifier in ids:
                    descriptors_by_id[desc.identifier] = desc

            targets: list[PlannedItem] = []
            for identifier in ids:
                desc = descriptors_by_id.get(identifier)
                existing = items.get(source.name, collection.name, identifier)
                if desc is None or existing is None:
                    log.warning("reconform: %s no longer in source — skipped", identifier)
                    continue
                targets.append(PlannedItem(desc, existing, PlanAction.CHANGED_UPSTREAM))

            stats = CollectionSyncStats()
            _execute_fetch_and_stage(
                config, source, collection, plugin, items, targets, stats
            )
            if stats.failed:
                failed += stats.failed
    return 0 if failed == 0 else 1
