"""Disk figures and the disk-full estimate (§spec:library-stats "Disk", "Disk-full
estimate")."""
from __future__ import annotations

import os
import shutil
from datetime import date, timedelta
from pathlib import Path

from shakedown.dashboard_library import disk_usage, full_in_days


def test_disk_usage_is_the_filesystems_size_and_free_space(tmp_path: Path) -> None:
    total, free = disk_usage(tmp_path)
    st = os.statvfs(tmp_path)
    assert total == st.f_blocks * st.f_frsize == shutil.disk_usage(tmp_path).total
    # Free space as `df` reports it (available to non-root), give or take writes
    # happening elsewhere between the two reads.
    assert abs(free - st.f_bavail * st.f_frsize) < 64 * 2**20


def _growth(days: int, step: int, *, start: int = 1000) -> list[dict]:
    today = date(2026, 11, 12)
    rows = []
    for n in range(days):
        day = today - timedelta(days=days - 1 - n)
        # Two collections: the estimate sums them per day.
        rows.append({"date": day.isoformat(), "source": "s", "collection": "a",
                     "complete": n, "archive_bytes": start + n * step})
        rows.append({"date": day.isoformat(), "source": "s", "collection": "b",
                     "complete": 1, "archive_bytes": 500})
    return rows


def test_eight_days_of_growth_give_an_estimate() -> None:
    # 7 days spanned, 700 bytes grown: 100 a day; 10_050 free → 100 days.
    assert full_in_days(_growth(8, 100), 10_050) == 100


def test_too_little_history_or_no_growth_gives_none() -> None:
    assert full_in_days(_growth(6, 100), 10_000) is None
    assert full_in_days(_growth(7, 100), 10_000) is None
    assert full_in_days(_growth(8, 0), 10_000) is None
    assert full_in_days(_growth(8, -50), 10_000) is None
    assert full_in_days([], 10_000) is None


def test_a_collection_that_starts_later_counts_as_growth() -> None:
    rows = _growth(8, 0)
    rows.append({"date": "2026-11-12", "source": "s", "collection": "new",
                 "complete": 3, "archive_bytes": 700})
    assert full_in_days(rows, 1_000) == 10
