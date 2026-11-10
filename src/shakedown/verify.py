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
from shakedown.lock import run_exclusive
from shakedown.models import Item, ItemStatus, RunKind
from shakedown.plugins import registry
from shakedown.state import DriftRepo, ItemRepo, TagRepo
from shakedown.sync import run_single_shows
from shakedown.tagging import recover_interrupted_tagging
from shakedown.trigger import announce_started

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


def _scan_drift(item: Item, post_tag: dict[str, str]) -> list[DriftRecord]:
    """Hash every recorded-manifest file; report mismatches and missing files. A file
    shakedown tagged is compared against its recorded post-tag checksum, so its own
    tagging is not drift while any later change is (§spec:tag-filling)."""
    drifts: list[DriftRecord] = []
    if item.recorded_manifest is None or item.archive_path is None:
        return drifts
    for mf in item.recorded_manifest.files:
        expected = post_tag.get(mf.name, mf.md5)
        on_disk = item.archive_path / mf.name
        if not on_disk.is_file():
            drifts.append(
                DriftRecord(
                    source=item.source_name,
                    collection=item.collection_name,
                    identifier=item.identifier,
                    file_name=mf.name,
                    expected_md5=expected,
                    observed_md5=None,
                )
            )
            continue
        if expected is None:
            continue  # source didn't publish a checksum; nothing to compare
        observed = _md5_file(on_disk)
        if observed != expected:
            drifts.append(
                DriftRecord(
                    source=item.source_name,
                    collection=item.collection_name,
                    identifier=item.identifier,
                    file_name=mf.name,
                    expected_md5=expected,
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
    def command() -> int:
        return _run_verify(
            config, source_filter, collection_filter, deep, reconform, list_drift, assume_yes
        )

    # --reconform re-fetches into the archive, so it holds the writer lock from the
    # start: a busy lock fails fast instead of after a long --deep hash pass.
    return run_exclusive(config, command) if reconform else command()


def _run_verify(
    config: Config,
    source_filter: str | None,
    collection_filter: str | None,
    deep: bool,
    reconform: bool,
    list_drift: bool,
    assume_yes: bool,
) -> int:
    # A verify opens no run record up front; serve learns only that it started (and,
    # for a reconform, that it holds the writer lock) (§spec:api-sync-runs). A
    # reconform's runs are opened once the hash pass knows what drifted.
    announce_started([])
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    drift_repo = DriftRepo(conn)
    tags = TagRepo(conn)

    all_drift: list[DriftRecord] = []
    missing_summary: dict[tuple[str, str], int] = {}

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue
            if reconform:
                # Holding the writer lock, undo a tag write a crash cut short before
                # hashing: the file is its original again, not drift to re-download
                # (§spec:tag-filling). A plain verify holds no lock, so a backup may
                # belong to a sync still writing; it only reports what it finds.
                recover_interrupted_tagging(config.archive_root / source.name / collection.name)

            for item in items.list_for_collection(source.name, collection.name):
                if item.status != ItemStatus.COMPLETE:
                    continue
                if not deep:
                    # cheap check: existence only, via plugin
                    plugin = registry.for_source(source)
                    files = item.recorded_manifest.files if item.recorded_manifest else ()
                    result = plugin.verify(files, item.archive_path)  # type: ignore[arg-type]
                    if not result.ok:
                        missing_summary[(source.name, collection.name)] = (
                            missing_summary.get((source.name, collection.name), 0)
                            + len(result.missing_files)
                        )
                else:
                    post_tag = {
                        name: record.post_tag_md5
                        for name, record in tags.file_records(
                            item.source_name, item.collection_name, item.identifier
                        ).items()
                    }
                    drifts = _scan_drift(item, post_tag)
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

    return _reconform(config, all_drift)


def _reconform(config: Config, drifts: list[DriftRecord]) -> int:
    """Re-fetch the drifted files the source still lists, as one single-show run per
    collection (§spec:verify-drift, §spec:single-show-runs). On a
    `fill_tags` collection, tags are refilled on the single-show run's shared fetch
    and staging path (§spec:tag-filling)."""
    drifted: dict[tuple[str, str], dict[str, set[str]]] = {}
    for d in drifts:
        shows = drifted.setdefault((d.source, d.collection), {})
        shows.setdefault(d.identifier, set()).add(d.file_name)
    for (source, collection), shows in drifted.items():
        log.info("reconform: repairing %d shows in %s/%s", len(shows), source, collection)
    return run_single_shows(
        config,
        {
            key: {identifier: frozenset(files) for identifier, files in shows.items()}
            for key, shows in drifted.items()
        },
        RunKind.RECONFORM,
    )
