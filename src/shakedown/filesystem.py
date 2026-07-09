"""Filesystem helpers: same-FS check, hardlink utilities. Enforces PRD §4."""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


class FilesystemError(Exception):
    pass


def ensure_same_filesystem(archive_root: Path, library_root: Path) -> None:
    """Hard error if archive_root and library_root live on different filesystems.

    Hardlinks require both ends on the same filesystem (PRD §4). Both directories
    are created if they don't exist, since we'd create them on first use anyway.
    """
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        library_root.mkdir(parents=True, exist_ok=True)
        a_dev = os.stat(archive_root).st_dev
        l_dev = os.stat(library_root).st_dev
    except OSError as e:
        raise FilesystemError(
            f"archive_root and library_root must be usable directories.\n"
            f"  archive_root={archive_root}\n"
            f"  library_root={library_root}\n"
            f"Fix the configured path or volume mount and try again: {e}"
        ) from e
    if a_dev != l_dev:
        raise FilesystemError(
            f"archive_root and library_root must live on the same filesystem "
            f"for hardlinks to work.\n"
            f"  archive_root={archive_root} (st_dev={a_dev})\n"
            f"  library_root={library_root} (st_dev={l_dev})\n"
            f"Move them under the same volume and try again."
        )


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
