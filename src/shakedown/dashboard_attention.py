"""`GET /api/attention`: what needs the operator, and library health
(§spec:needs-attention).

Every entry and health figure comes from `status`'s own per-collection summary
(`status.collection_summaries`), so the two never disagree. Entries carry codes and
facts; the page words the next action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shakedown.activity import HISTORY_DAYS, STOPPED_REPORTING
from shakedown.dashboard import DashboardContext, iso
from shakedown.history import GOOD_RESULTS, run_history
from shakedown.models import ItemStatus, RunKind
from shakedown.selection import keeps_unplayable_original
from shakedown.state import ItemRepo
from shakedown.status import collection_summaries

_RECOVERY_TIMES = ("started_at", "updated_at", "finished_at")


def _api_time(value: str | None) -> str | None:
    """A time from `status`'s summary (naive, serve-local) as the API answers times."""
    return iso(datetime.fromisoformat(value)) if value else None


def _entry(
    kind: str,
    summary: dict[str, Any],
    *,
    since: str | None = None,
    identifiers: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": summary["source"],
        "collection": summary["collection"],
        "since": _api_time(since),
        "identifiers": identifiers or [],
        "detail": detail or {},
    }


def _kind_group(kind: str) -> str:
    """The kind whose good run clears an unexpected end: a catch-up is a sync."""
    return RunKind.SYNC.value if kind == RunKind.CATCH_UP.value else kind


def _unexpected_ends(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One collection's runs (the run history, newest first) that ended
    `stopped_reporting` and were not followed by a good run of their kind
    (§spec:needs-attention): a later run that ended `completed` or
    `completed_with_item_issues` (§spec:run-history "Last good sync")."""
    ends: list[dict[str, Any]] = []
    cleared: set[str] = set()  # kinds with a good run later than the rows still to come
    for run in runs:
        group = _kind_group(run["kind"])
        if run["result"] in GOOD_RESULTS:
            cleared.add(group)
        elif run["end_reason"] == STOPPED_REPORTING and group not in cleared:
            ends.append(
                {"run_id": run["run_id"], "activity_id": run["activity_id"],
                 "kind": run["kind"], "finished_at": iso(run["finished_at"])}
            )
    return ends


def _entries(
    summary: dict[str, Any], items: ItemRepo, runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The collection's entries, in the kind order of §spec:needs-attention."""
    source, collection = summary["source"], summary["collection"]
    entries: list[dict[str, Any]] = []
    if summary["counts"][ItemStatus.FAILED.value]:
        failed = items.list_identifiers(source, collection, ItemStatus.FAILED)
        entries.append(_entry("failed_shows", summary, identifiers=failed))
    recovery = summary["recovery"]
    if recovery:
        detail = {
            **recovery,
            **{key: _api_time(recovery[key]) for key in _RECOVERY_TIMES},
        }
        entries.append(
            _entry(
                "recovery",
                summary,
                since=recovery["finished_at"] or recovery["updated_at"],
                identifiers=[recovery["affected_item"]] if recovery["affected_item"] else [],
                detail=detail,
            )
        )
    ended = _unexpected_ends(runs)
    if ended:
        times = [run["finished_at"] for run in ended if run["finished_at"]]
        entries.append(
            {
                **_entry("ended_unexpectedly", summary, detail={"runs": ended}),
                "since": min(times, key=datetime.fromisoformat) if times else None,
            }
        )
    if summary["stale"]:
        entries.append(_entry("stale", summary))
    if summary["rescheduled"]:
        entries.append(_entry("rescheduled", summary, since=summary["rescheduled"]["since"]))
    handoff = summary["handoff_pending"]
    if handoff:
        entries.append(
            _entry(
                "handoff_pending",
                summary,
                since=handoff["since"],
                identifiers=handoff["identifiers"],
                detail={"shows": handoff["shows"], "last_reason": handoff["last_reason"]},
            )
        )
    last_run = summary["last_run"]
    if last_run and last_run["failure_notice_error"]:
        entries.append(
            _entry(
                "failure_notice_undelivered",
                summary,
                since=last_run["finished_at"],
                detail={
                    "failure_notice_error": last_run["failure_notice_error"],
                    "run_id": last_run["run_id"],
                },
            )
        )
    if summary["separations"]:
        entries.append(
            _entry(
                "separations",
                summary,
                identifiers=[s["identifier"] for s in summary["separations"]],
                detail={"separations": summary["separations"]},
            )
        )
    if summary["unplaced_files"]:
        entries.append(
            _entry(
                "unplaced_files", summary, detail={"unplaced_files": summary["unplaced_files"]}
            )
        )
    return entries


def _health(summary: dict[str, Any], items: ItemRepo) -> dict[str, Any]:
    """The collection's library facts: not to-dos, so apart from the entries."""
    kept_twice = [
        identifier
        for identifier, manifest in items.list_archive_only_manifests(
            summary["source"], summary["collection"]
        )
        if keeps_unplayable_original(manifest)
    ]
    return {
        "source": summary["source"],
        "collection": summary["collection"],
        "restricted": summary["restricted"],
        "removed_upstream": summary["removed_upstream"],
        "drift_files": summary["drift_files"],
        "not_playable": summary["not_playable"],
        "playable_copies": kept_twice,
    }


def attention_answer(ctx: DashboardContext) -> dict[str, Any]:
    """Every configured collection's entries (empty when nothing needs attention) and
    its health, in config order. Lists are complete; the page decides how many to
    show."""
    items = ItemRepo(ctx.conn)
    history = run_history(ctx.conn, datetime.now(), HISTORY_DAYS)
    summaries = collection_summaries(ctx.config, ctx.conn)

    def runs_of(summary: dict[str, Any]) -> list[dict[str, Any]]:
        scope = (summary["source"], summary["collection"])
        return [r for r in history if (r["source"], r["collection"]) == scope]

    return {
        "entries": [entry for s in summaries for entry in _entries(s, items, runs_of(s))],
        "health": [_health(s, items) for s in summaries],
    }
