"""Durable recovery-reporting helpers for user-visible operations."""
from __future__ import annotations

from datetime import datetime

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import OperationOutcome
from shakedown.state import OperationOutcomeRepo


def record_issue(
    config: Config,
    *,
    source: str,
    collection: str,
    operation: str,
    phase: str,
    message: str,
    next_action: str,
    item_identifier: str | None = None,
) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    OperationOutcomeRepo(conn).record(
        OperationOutcome(
            source_name=source,
            collection_name=collection,
            operation=operation,
            status="failed",
            phase=phase,
            message=message,
            next_action=next_action,
            item_identifier=item_identifier,
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )
    )
    conn.close()


def clear_issue(config: Config, *, source: str, collection: str, operation: str) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    OperationOutcomeRepo(conn).clear(source, collection, operation)
    conn.close()
