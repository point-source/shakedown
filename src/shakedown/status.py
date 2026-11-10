"""shakedown status / shakedown item show. PRD §10."""
from __future__ import annotations

import json
from typing import Any

import click

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import ItemStatus, OperationOutcome
from shakedown.state import (
    DriftRepo,
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    PlacementRepo,
    RunRepo,
)


def _collection_summary(
    config: Config,
    source_name: str,
    collection_name: str,
    items: ItemRepo,
    runs: RunRepo,
    drift: DriftRepo,
    outcomes: OperationOutcomeRepo,
) -> dict[str, Any]:
    counts = items.count_by_status(source_name, collection_name)
    last = runs.latest(source_name, collection_name)
    recovery = outcomes.latest_actionable(source_name, collection_name)
    bytes_on_disk = items.archived_bytes(source_name, collection_name)

    restricted = [
        {"identifier": identifier, "reason": reason}
        for identifier, reason in items.list_restricted(source_name, collection_name)
    ]
    placements = PlacementRepo(items.conn)
    library_dir = config.library_root / source_name / collection_name

    return {
        "source": source_name,
        "collection": collection_name,
        "counts": {s.value: counts.get(s, 0) for s in ItemStatus},
        "bytes_on_disk": bytes_on_disk,
        "drift_files": drift.count(source_name, collection_name),
        "last_run": _summarize_run(last) if last else None,
        "recovery": _summarize_recovery(recovery) if recovery else None,
        "restricted": restricted,
        # Complete shows with no copy a common player reads (§spec:quality-selection).
        "not_playable": [
            {"identifier": identifier, "reason": reason}
            for identifier, reason in items.list_not_playable(source_name, collection_name)
        ],
        # Shows that render the same library folder, each staged into its own folder
        # labelled with its identifier (§spec:library-ownership).
        "separations": [
            {"identifier": identifier, "folder": str(library_dir / folder)}
            for identifier, folder in placements.list_separated(source_name, collection_name)
        ],
        # Library files shakedown did not place, left in place (§spec:library-cleanup).
        "unplaced_files": [
            str(library_dir / path)
            for path in placements.list_unplaced(source_name, collection_name)
        ],
        # Shows whose handoff notice was not delivered yet (§spec:durable-handoff).
        "handoff_pending": _handoff_pending(config, source_name, collection_name, items),
        # Stale = the most recent run failed to enumerate the source; existing
        # items are retained (§spec:failure-behavior).
        "stale": bool(last.stale) if last else False,
    }


def _handoff_pending(
    config: Config, source_name: str, collection_name: str, items: ItemRepo
) -> dict[str, Any] | None:
    """The collection's pending handoff, or None. A collection whose `on_complete` was
    removed has dropped its list, even before the next run deletes the rows."""
    has_handoff = any(
        c.name == collection_name and c.on_complete is not None
        for s in config.sources if s.name == source_name
        for c in s.collections
    )
    pending = PendingHandoffRepo(items.conn).list(source_name, collection_name)
    if not has_handoff or not pending:
        return None
    return {
        "shows": len(pending),
        "since": min(p.since for p in pending).isoformat(),
        "last_reason": pending[0].last_reason,
        "identifiers": [p.identifier for p in pending],
    }


def _summarize_run(run: Any) -> dict[str, Any]:
    return {
        # The id a `serve` trigger answers with, so a caller can find its run here
        # (§spec:api-sync-runs).
        "run_id": run.id,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "items_discovered": run.items_discovered,
        "items_new": run.items_new,
        "items_updated": run.items_updated,
        "items_failed": run.items_failed,
        "bytes_downloaded": run.bytes_downloaded,
        "errors": run.errors,
        # Why this run's failure alert was not delivered (§spec:durable-handoff).
        "failure_notice_error": run.failure_notice_error,
    }


def _summarize_recovery(outcome: OperationOutcome) -> dict[str, Any]:
    return {
        "operation": outcome.operation.value,
        "status": outcome.status.value,
        "started_at": outcome.started_at.isoformat(),
        "updated_at": outcome.updated_at.isoformat(),
        "finished_at": outcome.finished_at.isoformat() if outcome.finished_at else None,
        "phase": outcome.phase,
        "affected_item": outcome.affected_item,
        "affected_path": outcome.affected_path,
        "completed_work": outcome.completed_work,
        "preservation_context": outcome.preservation_context,
        "deletion_context": outcome.deletion_context,
        "safe_next_action": outcome.safe_next_action,
        "errors": outcome.errors,
    }


def print_status(config: Config, *, as_json: bool) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    runs = RunRepo(conn)
    drift = DriftRepo(conn)
    outcomes = OperationOutcomeRepo(conn)

    summaries = []
    for source in config.sources:
        for collection in source.collections:
            summaries.append(
                _collection_summary(
                    config, source.name, collection.name, items, runs, drift, outcomes
                )
            )

    if as_json:
        click.echo(json.dumps(summaries, indent=2, default=str))
        return

    for s in summaries:
        click.echo(f"=== {s['source']}/{s['collection']} ===")
        recovery = s["recovery"]
        if recovery:
            click.echo(
                f"  RECOVERY: {recovery['operation']} {recovery['status']} "
                f"during {recovery['phase'] or 'unknown phase'}"
            )
            if recovery["affected_item"]:
                click.echo(f"    item: {recovery['affected_item']}")
            if recovery["affected_path"]:
                click.echo(f"    path: {recovery['affected_path']!r}")
            if recovery["completed_work"]:
                completed = ", ".join(
                    f"{k}={v}" for k, v in recovery["completed_work"].items()
                )
                click.echo(f"    completed: {completed}")
            if recovery["preservation_context"]:
                click.echo(f"    preserved: {recovery['preservation_context']}")
            if recovery["deletion_context"]:
                click.echo(f"    deletion: {recovery['deletion_context']}")
            if recovery["safe_next_action"]:
                click.echo(f"    action: {recovery['safe_next_action']}")
        if s["stale"]:
            click.echo("  STALE: source enumeration failed last run; existing items retained")
        last = s["last_run"]
        if last:
            click.echo(
                f"  last run: {last['started_at']} → "
                f"new={last['items_new']} updated={last['items_updated']} "
                f"failed={last['items_failed']} bytes={last['bytes_downloaded']}"
            )
            if last["failure_notice_error"]:
                click.echo(f"  failure notice not delivered: {last['failure_notice_error']}")
        else:
            click.echo("  last run: never")
        handoff = s["handoff_pending"]
        if handoff:
            shows = "1 show" if handoff["shows"] == 1 else f"{handoff['shows']} shows"
            click.echo(f"  handoff not delivered: {shows} since {handoff['since']}")
            click.echo(f"    last reason: {handoff['last_reason']}")
        click.echo("  items: " + ", ".join(f"{k}={v}" for k, v in s["counts"].items() if v))
        click.echo(f"  disk:  {s['bytes_on_disk'] / 1e9:.2f} GiB")
        separations = s["separations"]
        if separations:
            click.echo(
                f"  shared folder names ({len(separations)} shows, each labelled by identifier):"
            )
            for sep in separations[:10]:
                # `!r`: folder names come from source metadata via the layout template and
                # may carry control characters — repr neutralizes terminal escapes.
                click.echo(f"    - {sep['identifier']} → {sep['folder']!r}")
            if len(separations) > 10:
                click.echo(f"    ... and {len(separations) - 10} more")
        if s["unplaced_files"]:
            click.echo(
                f"  library files not placed by shakedown: {len(s['unplaced_files'])} "
                "(left in place; paths in status --json)"
            )
        if s["drift_files"]:
            click.echo(f"  drift: {s['drift_files']} files (run `verify --deep --list` for paths)")
        if s["restricted"]:
            click.echo(f"  restricted ({len(s['restricted'])}):")
            for r in s["restricted"][:10]:
                click.echo(f"    - {r['identifier']}: {r['reason']}")
            if len(s["restricted"]) > 10:
                click.echo(f"    ... and {len(s['restricted']) - 10} more")
        if s["not_playable"]:
            click.echo(f"  not playable ({len(s['not_playable'])}):")
            for r in s["not_playable"][:10]:
                click.echo(f"    - {r['identifier']}: {r['reason']}")
            if len(s["not_playable"]) > 10:
                click.echo(f"    ... and {len(s['not_playable']) - 10} more")
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
