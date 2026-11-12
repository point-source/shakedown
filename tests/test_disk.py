"""Disk figures (§spec:library-stats "Disk")."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from shakedown.dashboard_library import disk_usage


def test_disk_usage_is_the_filesystems_size_and_free_space(tmp_path: Path) -> None:
    total, free = disk_usage(tmp_path)
    st = os.statvfs(tmp_path)
    assert total == st.f_blocks * st.f_frsize == shutil.disk_usage(tmp_path).total
    # Free space as `df` reports it (available to non-root), give or take writes
    # happening elsewhere between the two reads.
    assert abs(free - st.f_bavail * st.f_frsize) < 64 * 2**20
