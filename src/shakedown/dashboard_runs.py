"""`GET /api/runs`: every run of the last N days (§spec:run-history)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from shakedown.dashboard import Builder, DashboardContext, iso
from shakedown.history import run_history


def runs_answer(days: int) -> Builder:
    """The builder for the runs started in the last ``days`` days, newest first."""

    def build(ctx: DashboardContext) -> dict[str, Any]:
        runs = run_history(ctx.conn, datetime.now(), days)
        for run in runs:
            run["started_at"] = iso(run["started_at"])
            run["finished_at"] = iso(run["finished_at"])
        return {"runs": runs}

    return build
