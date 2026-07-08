"""shakedown status / shakedown item show. PRD §10."""
from __future__ import annotations

import json
from typing import Any

import click

from shakedown.config import Config
from shakedown.db import connect
from shakedown.filesystem import disk_usage_bytes
from shakedown.models import ItemStatus
from shakedown.state import DriftRepo, ItemRepo, RunRepo


def _collection_summary(
    config: Config,
    source_name: str,
    collection_name: str,
    items: ItemRepo,
    runs: RunRepo,
    drift: DriftRepo,
) -> dict[str, Any]:
    counts = items.count_by_status(source_name, collection_name)
    last = runs.latest(source_name, collection_name)
    archive_dir = config.archive_root / source_name / collection_name
    bytes_on_disk = disk_usage_bytes(archive_dir) if archive_dir.exists() else 0

    restricted = []
    if counts.get(ItemStatus.UNAVAILABLE, 0):
        for item in items.list_for_collection(source_name, collection_name):
            if item.status == ItemStatus.UNAVAILABLE:
                restricted.append(
                    {"identifier": item.identifier, "reason": item.restriction_reason}
                )

    return {
        "source": source_name,
        "collection": collection_name,
        "counts": {s.value: counts.get(s, 0) for s in ItemStatus},
        "bytes_on_disk": bytes_on_disk,
        "drift_files": drift.count(source_name, collection_name),
        "last_run": _summarize_run(last) if last else None,
        "restricted": restricted,
        # Stale = the most recent run failed to enumerate the source; existing
        # items are retained (§spec:failure-behavior).
        "stale": bool(last.stale) if last else False,
    }


def _summarize_run(run: Any) -> dict[str, Any]:
    return {
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "items_discovered": run.items_discovered,
        "items_new": run.items_new,
        "items_updated": run.items_updated,
        "items_failed": run.items_failed,
        "bytes_downloaded": run.bytes_downloaded,
        "errors": run.errors,
        # Recordings dropped to layout collisions last run, distinct from item_failed
        # (§spec:layout-collision-safety).
        "collisions_dropped": run.collisions_dropped,
        "collision_paths": run.collision_paths,
    }


def print_status(config: Config, *, as_json: bool) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)
    drift = DriftRepo(conn)

    summaries = []
    for source in config.sources:
        for collection in source.collections:
            summaries.append(
                _collection_summary(config, source.name, collection.name, items, runs, drift)
            )

    if as_json:
        click.echo(json.dumps(summaries, indent=2, default=str))
        return

    for s in summaries:
        click.echo(f"=== {s['source']}/{s['collection']} ===")
        if s["stale"]:
            click.echo("  STALE: source enumeration failed last run; existing items retained")
        last = s["last_run"]
        if last:
            click.echo(
                f"  last run: {last['started_at']} → "
                f"new={last['items_new']} updated={last['items_updated']} "
                f"failed={last['items_failed']} bytes={last['bytes_downloaded']}"
            )
        else:
            click.echo("  last run: never")
        click.echo("  items: " + ", ".join(f"{k}={v}" for k, v in s["counts"].items() if v))
        click.echo(f"  disk:  {s['bytes_on_disk'] / 1e9:.2f} GiB")
        if last and last["collisions_dropped"]:
            # Recordings dropped to layout collisions last run — a loud line separate
            # from ordinary fetch failures (§spec:layout-collision-safety).
            click.echo(
                f"  layout collisions: {last['collisions_dropped']} recording(s) "
                f"dropped to a shared path last run"
            )
            for path in last["collision_paths"][:10]:
                # `!r`: paths derive from remote metadata via the layout template and
                # may carry control characters — repr neutralizes terminal-escape
                # injection into `status` output (§spec:layout-collision-safety).
                click.echo(f"    - {path!r}")
            if len(last["collision_paths"]) > 10:
                click.echo(f"    ... and {len(last['collision_paths']) - 10} more")
        if s["drift_files"]:
            click.echo(f"  drift: {s['drift_files']} files (run `verify --deep --list` for paths)")
        if s["restricted"]:
            click.echo(f"  restricted ({len(s['restricted'])}):")
            for r in s["restricted"][:10]:
                click.echo(f"    - {r['identifier']}: {r['reason']}")
            if len(s["restricted"]) > 10:
                click.echo(f"    ... and {len(s['restricted']) - 10} more")
        click.echo("")


def show_item(config: Config, identifier: str) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    found = False
    for source in config.sources:
        for collection in source.collections:
            item = items.get(source.name, collection.name, identifier)
            if item is None:
                continue
            found = True
            click.echo(f"=== {source.name}/{collection.name}/{identifier} ===")
            click.echo(f"  status:           {item.status.value}")
            click.echo(f"  archive_path:     {item.archive_path}")
            click.echo(f"  discovered_at:    {item.discovered_at}")
            click.echo(f"  downloaded_at:    {item.downloaded_at}")
            click.echo(f"  last_verified_at: {item.last_verified_at}")
            if item.restriction_reason:
                click.echo(f"  restriction:      {item.restriction_reason}")
            if item.recorded_manifest:
                click.echo(f"  files: {len(item.recorded_manifest.files)}")
                for mf in item.recorded_manifest.files[:20]:
                    click.echo(f"    - {mf.name} ({mf.size}B md5={mf.md5})")
                if len(item.recorded_manifest.files) > 20:
                    click.echo(f"    ... and {len(item.recorded_manifest.files) - 20} more")
    if not found:
        click.echo(f"identifier {identifier!r} not found", err=True)
