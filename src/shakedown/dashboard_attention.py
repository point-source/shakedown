"""`GET /api/attention`: what needs the operator, and library health
(§spec:needs-attention).

Every entry and health figure comes from `status`'s own per-collection summary
(`status.collection_summaries`), so the two never disagree. Entries carry codes and
facts; the page words the next action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shakedown.dashboard import DashboardContext, iso
from shakedown.models import ItemStatus
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


def _entries(summary: dict[str, Any], items: ItemRepo) -> list[dict[str, Any]]:
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


def _health(summary: dict[str, Any]) -> dict[str, Any]:
    """The collection's library facts: not to-dos, so apart from the entries."""
    return {
        "source": summary["source"],
        "collection": summary["collection"],
        "restricted": summary["restricted"],
        "removed_upstream": summary["removed_upstream"],
        "drift_files": summary["drift_files"],
        "not_playable": summary["not_playable"],
        "playable_copies": [],
    }


def attention_answer(ctx: DashboardContext) -> dict[str, Any]:
    """Every configured collection's entries (empty when nothing needs attention) and
    its health, in config order. Lists are complete; the page decides how many to
    show."""
    items = ItemRepo(ctx.conn)
    summaries = collection_summaries(ctx.config, ctx.conn)
    return {
        "entries": [entry for s in summaries for entry in _entries(s, items)],
        "health": [_health(s) for s in summaries],
    }
