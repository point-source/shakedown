"""`GET /api/activity`: the live view of runs (§spec:run-activity)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shakedown.activity import ActivityRepo
from shakedown.dashboard import DashboardContext, iso

_TIMES = ("started_at", "finished_at", "last_heartbeat_at", "last_progress_at")


def activity_answer(ctx: DashboardContext) -> dict[str, Any]:
    """Running entries first, then entries that ended in the last ten minutes, newest
    first. `network` is filled by §spec:network-activity."""
    runs = ActivityRepo(ctx.conn).live(datetime.now())
    for entry in runs:
        for key in _TIMES:
            entry[key] = iso(entry[key])
    return {"runs": runs, "network": []}
