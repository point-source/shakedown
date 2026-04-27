from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from shakedown.filesystem import FilesystemError, ensure_same_filesystem, hardlink, same_inode


def test_same_filesystem_passes_when_identical(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    ensure_same_filesystem(a, b)  # creates dirs; same FS by construction
    assert a.is_dir() and b.is_dir()


def test_same_filesystem_raises_when_different(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    a_resolved = str(a.resolve())
    b_resolved = str(b.resolve())

    real_stat = os.stat
    def fake_stat(p, *args, **kwargs):
        s = real_stat(p, *args, **kwargs)
        if str(p) in (b_resolved, str(b)):
            class FakeStat:
                def __getattr__(self, name):
                    if name == "st_dev":
                        return s.st_dev + 1
                    return getattr(s, name)
            return FakeStat()
        return s

    _ = a_resolved  # only b's stat is faked
    with patch("shakedown.filesystem.os.stat", side_effect=fake_stat), pytest.raises(
        FilesystemError, match="same filesystem"
    ):
        ensure_same_filesystem(a, b)


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
