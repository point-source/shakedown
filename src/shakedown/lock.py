"""One writer at a time: an exclusive lock file shared by every command that writes
to the archive, library, or state DB (sync, restage, reconcile, item refetch,
verify --reconform), whether started from the CLI, the scheduler, or `serve`.

Two writers would share per-item temp folders and wipe each other's half-written
files, so a second writer exits with a clear error instead of waiting. The lock is
a kernel `flock` on a file beside the state DB: it is released when the holder
exits, even on a crash or SIGKILL, so a stale lock file never blocks the next run.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from shakedown.config import Config
from shakedown.trigger import announce_busy

log = logging.getLogger(__name__)


class LockHeld(Exception):
    """Another shakedown command already holds the writer lock."""


def lock_path(config: Config) -> Path:
    assert config.state_db is not None  # filled in by Config validation
    return config.state_db.with_name(config.state_db.name + ".lock")


@contextlib.contextmanager
def writer_lock(config: Config) -> Iterator[None]:
    """Hold the exclusive writer lock for the duration of the block, or raise
    LockHeld at once if another command holds it."""
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld(
                f"another shakedown run is still writing (lock held on {path}); "
                f"wait for it to finish, then rerun"
            ) from None
        yield
    finally:
        os.close(fd)


def run_exclusive(
    config: Config,
    command: Callable[[], int],
    *,
    on_busy: Callable[[], None] | None = None,
) -> int:
    """Run a writing command under the writer lock; return 1 with a logged error
    when another run holds it, after calling ``on_busy`` if given."""
    try:
        with writer_lock(config):
            return command()
    except LockHeld as e:
        log.error("%s", e)
        announce_busy()
        if on_busy is not None:
            on_busy()
        return 1
