"""Filesystem helpers: hardlink probe, hardlink utilities. Enforces PRD §4."""
from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from pathlib import Path


class FilesystemError(Exception):
    pass


def ensure_hardlinkable(archive_root: Path, library_root: Path) -> None:
    """Hard error unless a file in archive_root can be hardlinked into library_root.

    Makes a real test link (PRD §4) instead of comparing device IDs: Linux refuses
    links across separate mounts of one filesystem (EXDEV), e.g. archive and library
    bind-mounted into a container separately, while both still report the same
    st_dev. Both directories are created if missing, since first use would anyway.
    The probe file and its link are always removed.
    """
    probe: Path | None = None
    link: Path | None = None
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        library_root.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(prefix=".shakedown-link-probe-", dir=archive_root)
        os.close(fd)
        probe = Path(probe_name)
        target = library_root / probe.name
        os.link(probe, target)
        link = target
    except OSError as e:
        hint = (
            "They are on different filesystems, or reach the container through separate "
            "volume mounts; Linux refuses hardlinks across either, even on the same disk. "
            "Put both on one filesystem, mount their shared parent folder once "
            "(e.g. /share/data:/data), and try again."
            if e.errno == errno.EXDEV
            else "Fix the configured path, permissions, or volume mount and try again."
        )
        raise FilesystemError(
            f"cannot hardlink files from archive_root into library_root: {e}\n"
            f"  archive_root={archive_root}\n"
            f"  library_root={library_root}\n"
            f"{hint}"
        ) from e
    finally:
        for path in (link, probe):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()


def check_writable(path: Path) -> None:
    """Raise FilesystemError unless a probe file can be created and removed under ``path``.

    Used by readiness validation (§spec:setup-readiness-validation) to prove archive,
    library, and state directories are writable *before* a multi-hour mirror begins.
    Creates ``path`` (and parents) if absent, since first use would anyway. The probe
    file is always removed, so a passing check leaves nothing behind — a broken setup
    can never masquerade as a clean no-op.
    """
    probe: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(prefix=".shakedown-write-probe-", dir=path)
        os.close(fd)
        probe = Path(probe_name)
    except OSError as e:
        raise FilesystemError(f"{path} is not writable: {e}") from e
    finally:
        with contextlib.suppress(OSError, TypeError):
            if probe is not None:
                probe.unlink()


def same_inode(a: Path, b: Path) -> bool:
    """True if a and b refer to the same inode on the same device."""
    sa = a.stat()
    sb = b.stat()
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def hardlink(src: Path, dst: Path) -> None:
    """Create a hardlink at dst pointing to src's inode.

    If dst exists and points to the same inode, this is a no-op.
    If dst exists pointing to a different inode, raises FilesystemError (collision).
    Parent directories are created as needed.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if same_inode(src, dst):
            return
        raise FilesystemError(
            f"hardlink collision: {dst} already exists and points to a different inode"
        )
    os.link(src, dst)


def disk_usage_bytes(path: Path) -> int:
    """Sum of file sizes under path. O(n) walk; do not call in hot loops."""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            with contextlib.suppress(FileNotFoundError):
                total += entry.stat().st_size
    return total
