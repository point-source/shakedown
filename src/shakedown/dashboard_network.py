"""`GET /api/network` and the `network` part of `GET /api/activity`: per-source
network activity (§spec:network-activity)."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from shakedown.dashboard import Builder, DashboardContext, iso
from shakedown.network import MinuteTotals, NetworkEvent, NetworkRepo


def event_answer(event: NetworkEvent) -> dict[str, Any]:
    """A network event as the API answers it; absent values are null."""
    return {
        "at": iso(event.at),
        "kind": event.kind.value,
        "http_status": event.http_status,
        "collection": event.collection,
        "identifier": event.identifier,
        "wait_seconds": event.wait_seconds,
        "detail": event.detail,
    }


# A series holds at most about this many points; one day keeps every minute
# (parent answer Q-3-1: the spec's "60 for one day, 3600 for 30" win over its "~720").
_MAX_POINTS = 720
_WIDER_BUCKETS = (120, 300, 600, 900, 1800, 3600)
EVENTS_SHOWN = 500


def bucket_seconds(days: int) -> int:
    """The series' bucket for a window of ``days``: 60 s for one day, else the
    narrowest of 2, 5, 10, 15, 30, or 60 minutes keeping a series ≤ 720 points.
    Every bucket divides an hour."""
    if days <= 1:
        return 60
    for bucket in _WIDER_BUCKETS:
        if days * 86400 / bucket <= _MAX_POINTS:
            return bucket
    return _WIDER_BUCKETS[-1]


def _bucket_start(minute: datetime, bucket: int) -> datetime:
    step = bucket // 60
    return minute.replace(minute=minute.minute - minute.minute % step)


def network_answer(days: int) -> Builder:
    """The builder for the last ``days`` days: per configured source, in config order,
    the series in buckets (only buckets with recorded activity), the newest events
    newest first, and how many events the window holds. Empty history before schema
    v10."""

    def build(ctx: DashboardContext) -> dict[str, Any]:
        since = datetime.now() - timedelta(days=days)
        bucket = bucket_seconds(days)
        repo = NetworkRepo(ctx.conn)
        has_history = repo.has_tables()
        sources: list[dict[str, Any]] = []
        for name in dict.fromkeys(source.name for source in ctx.config.sources):
            series: dict[datetime, MinuteTotals] = {}
            events: list[NetworkEvent] = []
            total = 0
            if has_history:
                for minute, totals in repo.minutes(name, since):
                    start = _bucket_start(minute, bucket)
                    series.setdefault(start, MinuteTotals()).merge(totals)
                events = repo.events(name, since, EVENTS_SHOWN)
                total = repo.events_total(name, since)
            sources.append({
                "source": name,
                "series": [{"t": iso(t), **asdict(totals)} for t, totals in series.items()],
                "events": [event_answer(e) for e in events],
                "events_total": total,
            })
        return {"bucket_seconds": bucket, "sources": sources}

    return build
