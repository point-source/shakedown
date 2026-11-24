"""First-run setup (§spec:first-run-setup): the defaults setup offers.

Setup writes the same `shakedown.yaml` Settings edits, and only while no file exists.
The defaults are the paths of the compose example's single `/data` mount
(§spec:deployment), so a new user can accept them as they are.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shakedown.config import default_state_db
from shakedown.dashboard import iso
from shakedown.dashboard_config import source_types
from shakedown.schedule import staggered_default

DEFAULT_ARCHIVE_ROOT = Path("/data/archive")
DEFAULT_LIBRARY_ROOT = Path("/data/library")


def setup_defaults(now: datetime | None = None) -> dict[str, Any]:
    """`GET /api/setup`: the paths, the source types, and the first collection's
    schedule setup offers. The page fills `timezone` from the browser."""
    return {
        "generated_at": iso(now or datetime.now(UTC)),
        "archive_root": str(DEFAULT_ARCHIVE_ROOT),
        "library_root": str(DEFAULT_LIBRARY_ROOT),
        "state_db": str(default_state_db(DEFAULT_ARCHIVE_ROOT)),
        "source_types": source_types(),
        # No other collection exists yet, so the staggered default's first hour.
        "schedule": staggered_default(None),
    }
