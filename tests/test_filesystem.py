from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from shakedown.filesystem import (
    FilesystemError,
    check_writable,
    ensure_hardlinkable,
    hardlink,
    same_inode,
)


def test_hardlinkable_passes_and_leaves_nothing_behind(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    ensure_hardlinkable(a, b)  # creates dirs; one filesystem by construction
    assert a.is_dir() and b.is_dir()
    assert list(a.iterdir()) == []
    assert list(b.iterdir()) == []


def test_hardlinkable_fails_across_mounts(tmp_path: Path) -> None:
    """Separate bind mounts share st_dev but refuse links with EXDEV."""
    a = tmp_path / "a"
    b = tmp_path / "b"

    def exdev(src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src), None, str(dst))

    with patch("shakedown.filesystem.os.link", side_effect=exdev), pytest.raises(
        FilesystemError, match="separate volume mounts"
    ):
        ensure_hardlinkable(a, b)
    assert list(a.iterdir()) == []
    assert list(b.iterdir()) == []


def test_check_writable_preserves_existing_probe_name(tmp_path: Path) -> None:
    existing = tmp_path / ".shakedown-write-probe"
    existing.write_text("operator data")

    check_writable(tmp_path)

    assert existing.read_text() == "operator data"


def test_hardlink_creates_link(tmp_path: Path) -> None:
    src = tmp_path / "src.flac"
    src.write_bytes(b"audio")
    dst = tmp_path / "library" / "dst.flac"
    hardlink(src, dst)
    assert same_inode(src, dst)


def test_hardlink_idempotent_when_already_linked(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"x")
    dst = tmp_path / "dst"
    hardlink(src, dst)
    hardlink(src, dst)  # no error, no-op
    assert same_inode(src, dst)


def test_hardlink_collision_raises(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"x")
    other = tmp_path / "other"
    other.write_bytes(b"y")
    dst = tmp_path / "dst"
    os_link_target = other  # noqa: F841
    import os as os_mod
    os_mod.link(other, dst)
    with pytest.raises(FilesystemError, match="collision"):
        hardlink(src, dst)
