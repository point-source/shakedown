"""One writer at a time: an exclusive lock file shared by every command that writes
to the archive, library, or state DB (sync, restage, reconcile, item refetch,
verify --reconform, trim-history, purge), whether started from the CLI, the
scheduler, or `serve`.

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
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from shakedown import activity, retention, stopping
from shakedown.config import Config
from shakedown.db import connect
from shakedown.growth import GrowthRepo
from shakedown.trigger import announce_busy

log = logging.getLogger(__name__)


class LockHeld(Exception):
    """Another shakedown command already holds the writer lock."""


def lock_path(config: Config) -> Path:
    assert config.state_db is not None  # filled in by Config validation
    return lock_path_of(config.state_db)


def lock_path_of(state_db: Path) -> Path:
    """Beside the database file itself: two spellings of one database (a link, a
    relative path) share one lock."""
    real = state_db.resolve()
    return real.with_name(real.name + ".lock")


@contextlib.contextmanager
def writer_lock(config: Config) -> Iterator[None]:
    """Hold the exclusive writer lock for the duration of the block, or raise
    LockHeld at once if another command holds it."""
    assert config.state_db is not None  # filled in by Config validation
    with writer_lock_at(config.state_db):
        yield


@contextlib.contextmanager
def writer_lock_at(state_db: Path, *, retired_ok: bool = False) -> Iterator[None]:
    """`writer_lock` for the state database at ``state_db``. A lock retired after
    this process started raises LockHeld too, unless ``retired_ok``."""
    path = lock_path_of(state_db)
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
        if not retired_ok and _retired_since_start(os.pread(fd, 64, 0)):
            raise LockHeld(
                "the config file now names another state database; run again"
            )
        yield
    finally:
        os.close(fd)


# Written, with the time, into a database's lock file when a save points the config
# at another database (§spec:edit-lock "Where the state lives"). A run reads the
# config as it starts: one started before the save that takes this lock after it
# read the old config, and stops instead of writing beside the run that took the
# new database's lock. One started after it read the file as it is now, which a
# hand edit may have pointed back here.
RETIRED = b"retired "
_STARTED = time.time()


def _retired_since_start(content: bytes) -> bool:
    if not content.startswith(RETIRED):
        return False
    try:
        return float(content[len(RETIRED):]) >= _STARTED
    except ValueError:
        return False


def retire(state_db: Path) -> None:
    """Mark ``state_db``'s lock retired now; the caller holds it."""
    lock_path_of(state_db).write_bytes(RETIRED + f"{time.time()}".encode())


def unretire(state_db: Path) -> None:
    """Clear a retired mark on ``state_db``'s lock (a save moving back to it); the
    caller holds it."""
    with open(lock_path_of(state_db), "r+b") as f:
        f.truncate(0)


def run_exclusive(
    config: Config,
    command: Callable[[], int],
    *,
    on_busy: Callable[[], None] | None = None,
) -> int:
    """Run a writing command under the writer lock; return 1 with a logged error
    when another run holds it, after calling ``on_busy`` if given. Taking the lock
    first closes what killed runs left open (§spec:run-activity); its end records
    the library's growth (§spec:library-stats) and drops error lines older than
    `keep_run_errors_days` (§spec:run-error-retention).

    While the command holds the lock, SIGTERM and SIGINT ask it to stop; a command
    that was asked returns the stop's exit code, 143 or 130 (§spec:graceful-stop)."""
    try:
        with writer_lock(config), stopping.handling_signals():
            _close_abandoned(config)
            try:
                code = command()
            except stopping.Stopped:
                code = 1  # replaced below: a stop was requested
            finally:
                _record_growth(config)
                _trim_run_errors(config)
            if stopping.requested():
                log.warning("stopped on request; the next run continues from here")
                return stopping.exit_code()
            return code
    except LockHeld as e:
        log.error("%s", e)
        announce_busy()
        if on_busy is not None:
            on_busy()
        return 1


def _close_abandoned(config: Config) -> None:
    """Close the records of runs that were killed. Best effort: the command opens the
    database itself next and reports any real problem with it."""
    try:
        with contextlib.closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
            activity.close_abandoned(conn, datetime.now())
    except Exception as e:
        log.warning("could not close runs that ended unexpectedly: %s", e)


def _record_growth(config: Config) -> None:
    """Snapshot every configured collection's figures and keep 30 days of them. Best
    effort: a run's result never depends on it."""
    try:
        with contextlib.closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
            growth = GrowthRepo(conn)
            now = datetime.now()
            growth.record(
                now,
                scopes=[(s.name, c.name) for s in config.sources for c in s.collections],
            )
            growth.trim(now)
    except Exception as e:
        log.warning("could not record library growth: %s", e)


def _trim_run_errors(config: Config) -> None:
    """Drop the error lines of runs older than the setting. Best effort: a run's
    result never depends on it."""
    try:
        with contextlib.closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
            retention.trim_run_errors(conn, datetime.now(), config.keep_run_errors_days)
    except Exception as e:
        log.warning("could not trim old run errors: %s", e)
