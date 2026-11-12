"""`GET /api/health`: release, config validity, last validation
(§spec:dashboard-health)."""
from __future__ import annotations

from typing import Any

from shakedown import __version__
from shakedown.dashboard import DashboardContext, iso
from shakedown.models import OperationType
from shakedown.state import OperationOutcomeRepo


def health_answer(ctx: DashboardContext) -> dict[str, Any]:
    """The release of this serve, whether the config file loads now, and per
    configured collection the latest recorded readiness validation (null when none).
    It reports the validation already recorded: answering never runs one."""
    outcomes = OperationOutcomeRepo(ctx.conn)
    validation: list[dict[str, Any]] = []
    for source in ctx.config.sources:
        for collection in source.collections:
            latest = outcomes.latest(OperationType.VALIDATE, source.name, collection.name)
            validation.append(
                {
                    "source": source.name,
                    "collection": collection.name,
                    "status": latest.status.value if latest else None,
                    "at": iso(latest.finished_at or latest.updated_at) if latest else None,
                }
            )
    return {
        "release": __version__,
        "config": {
            "valid": ctx.config_state.error is None,
            "error": ctx.config_state.error,
            "last_loaded_at": iso(ctx.config_state.last_loaded_at),
        },
        "validation": validation,
    }
