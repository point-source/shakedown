"""`GET /api/activity`: the live view of runs (§spec:run-activity) and of each
source's network activity now (§spec:network-activity)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from shakedown.activity import RUNNING, ActivityRepo
from shakedown.dashboard import DashboardContext, iso
from shakedown.dashboard_network import event_answer
from shakedown.network import NetworkRepo

_TIMES = ("started_at", "finished_at", "last_heartbeat_at", "last_progress_at")


def activity_answer(ctx: DashboardContext) -> dict[str, Any]:
    """Running entries first, then entries that ended in the last ten minutes, newest
    first; and per source with a run in progress, its network activity now."""
    runs = ActivityRepo(ctx.conn).live(datetime.now())
    network = _network_now(ctx.conn, [r for r in runs if r["state"] == RUNNING])
    for entry in runs:
        del entry["requests_in_flight"]
        for key in _TIMES:
            entry[key] = iso(entry[key])
    return {"runs": runs, "network": network}


def _network_now(conn: sqlite3.Connection, running: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per source with a running entry (one that still reports): the sum of its
    entries' speed and requests in flight, and the newest network event since the
    earliest of them started — the run in progress, not an earlier one."""
    sources: dict[str, dict[str, Any]] = {}
    for entry in running:
        now = sources.setdefault(entry["source"], {
            "speed": 0.0, "in_flight": 0, "since": entry["started_at"],
        })
        now["speed"] += entry["speed_bytes_per_second"] or 0.0
        now["in_flight"] += entry["requests_in_flight"] or 0
        now["since"] = min(now["since"], entry["started_at"])
    repo = NetworkRepo(conn)
    answer: list[dict[str, Any]] = []
    for source in sorted(sources):
        now = sources[source]
        last = repo.last_event(source, now["since"])
        answer.append({
            "source": source,
            "speed_bytes_per_second": now["speed"],
            "requests_in_flight": now["in_flight"],
            "last_event": event_answer(last) if last is not None else None,
        })
    return answer
