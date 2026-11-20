"""Runs that `serve` starts as separate processes (§spec:api-sync-runs).

A triggered sync or verify is the ordinary one-shot CLI run — the same command the
scheduler launches — so it reads the config file at its start and a crash never
takes `serve` down. `serve` still has to answer the HTTP request at once, or refuse
it when another run holds the writer lock. The child tells it which, over a
readiness fd in the style of s6's `notification-fd`: serve passes the write end of a
pipe and names it in SHAKEDOWN_READY_FD, and the child writes one JSON line there:

- ``{"state": "busy"}`` — another run holds the writer lock; the child exits 1.
- ``{"state": "failed", "error": "..."}`` — the child could not load its config.
- ``{"state": "started"}`` — the child holds the lock.

After ``started`` the child keeps the fd and writes one more line per run record it
opens, as each collection starts — ``{"state": "opened", "run_id": …,
"outcome_id": …, "source": …, "collection": …}`` — so serve can close, by exact id,
the records a killed run left open. The fd closes when the child exits.

Without SHAKEDOWN_READY_FD (CLI and scheduler runs) every announce is a no-op.
sd_notify was not used: it needs a datagram socket and its own message format, and
all we carry is a few lines.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from shakedown.models import ENDED_UNEXPECTEDLY

log = logging.getLogger(__name__)

READY_FD_ENV = "SHAKEDOWN_READY_FD"


# The readiness fd, kept after ``started`` for the ``opened`` lines; None otherwise.
_ready_fd: int | None = None
_fd_lock = threading.Lock()


def _write(fd: int, message: dict[str, Any]) -> bool:
    try:
        os.write(fd, (json.dumps(message) + "\n").encode())
        return True
    except OSError as e:
        log.warning("could not tell serve the run's state: %s", e)
        return False


def _announce(message: dict[str, Any], *, keep: bool = False) -> None:
    """The run's one state line. Pop the variable, so only the first announce counts
    and child processes of this run (a handoff command) never see it; with ``keep``,
    hold the fd for the ``opened`` lines that follow."""
    global _ready_fd
    raw = os.environ.pop(READY_FD_ENV, None)
    if raw is None:
        return
    fd = int(raw)  # only serve sets it, always to the pipe's fd number
    if _write(fd, message) and keep:
        _ready_fd = fd
        return
    with contextlib.suppress(OSError):
        os.close(fd)


def launched_by_serve() -> bool:
    """Whether `serve` started this run. Ask before the first announce, which
    consumes the variable."""
    return READY_FD_ENV in os.environ


def announce_busy() -> None:
    _announce({"state": "busy"})


def announce_failed(error: str) -> None:
    _announce({"state": "failed", "error": error})


def announce_started() -> None:
    _announce({"state": "started"}, keep=True)


def announce_opened(run_id: int | None, outcome_id: int | None, source: str,
                    collection: str) -> None:
    """Tell serve, if it started this run, about a run record just opened."""
    global _ready_fd
    with _fd_lock:
        if _ready_fd is None:
            return
        if not _write(_ready_fd, {"state": "opened", "run_id": run_id,
                                  "outcome_id": outcome_id, "source": source,
                                  "collection": collection}):
            with contextlib.suppress(OSError):
                os.close(_ready_fd)
            _ready_fd = None


# --- serve side -------------------------------------------------------------------

DEFAULT_ARGV_PREFIX: tuple[str, ...] = (sys.executable, "-m", "shakedown")


@dataclass
class TriggeredRun:
    """A run serve started and is watching until it exits."""

    process: subprocess.Popen[bytes]
    runs: list[dict[str, Any]]
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class Started:
    run: TriggeredRun


@dataclass
class Busy:
    """Another run holds the writer lock; nothing was started or queued."""


@dataclass
class Failed:
    error: str


@dataclass
class ExitedEarly:
    """The child exited without saying whether it started."""

    returncode: int


Launch = Started | Busy | Failed | ExitedEarly


def launch(
    config_path: Path,
    args: list[str],
    *,
    state_db: Path,
    on_exit: Callable[[int], None] | None = None,
    argv_prefix: tuple[str, ...] = DEFAULT_ARGV_PREFIX,
    env: dict[str, str] | None = None,
) -> Launch:
    """Start `shakedown --config <config_path> <args>` and wait for its one line.

    Returns once the child has started (it holds the writer lock), refused (busy),
    or failed before starting. A started child is watched on a daemon thread, which
    collects the run records it opens as it announces them; when it exits, any of
    them it left open are closed, then ``on_exit`` gets its return code.
    """
    r, w = os.pipe()
    child_env = {**os.environ, **(env or {}), READY_FD_ENV: str(w)}
    try:
        process = subprocess.Popen(
            [*argv_prefix, "--config", str(config_path), *args],
            pass_fds=(w,),
            env=child_env,
            # Its own process group: Ctrl-C in serve's terminal reaches serve only,
            # and serve's SIGTERM is the run's first signal (§spec:graceful-stop).
            process_group=0,
        )
    finally:
        os.close(w)  # only the child holds the write end, so its exit means EOF
    reader = os.fdopen(r, "rb")
    line = reader.readline()
    message: dict[str, Any] = json.loads(line) if line else {}
    state = message.get("state")
    if state != "started":
        reader.close()
        returncode = process.wait()
        if state == "busy":
            return Busy()
        if state == "failed":
            return Failed(str(message.get("error", "")))
        return ExitedEarly(returncode)

    triggered = TriggeredRun(process=process, runs=[])

    def watch() -> None:
        with reader:
            for raw in reader:  # until the child exits and the pipe closes
                with contextlib.suppress(ValueError):
                    opened = json.loads(raw)
                    if opened.get("state") == "opened":
                        triggered.runs.append(opened)
        returncode = process.wait()
        try:
            close_abandoned(state_db, triggered.runs, returncode)
        except Exception:
            log.exception("could not close the run records of a run that ended")
        try:
            if on_exit is not None:
                on_exit(returncode)
        finally:
            triggered.done.set()

    threading.Thread(target=watch, name=f"watch-{process.pid}", daemon=True).start()
    return Started(triggered)


# How long serve's shutdown waits for the runs it started to stop: inside the 120 s
# stop timeout the README recommends, with room for serve's own exit.
STOP_WAIT_S = 90.0


def stop_runs(triggered: list[TriggeredRun], timeout: float = STOP_WAIT_S) -> None:
    """Ask every run still going to stop (SIGTERM) and wait up to ``timeout`` for each
    to exit and be closed (§spec:graceful-stop). serve calls this as it shuts down:
    it is PID 1 in its container, and when it exits the kernel kills every process
    left there, so a run it started would otherwise die mid-step."""
    live = [t for t in triggered if not t.done.is_set()]
    for t in live:
        with contextlib.suppress(ProcessLookupError):
            t.process.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + timeout
    for t in live:
        if not t.done.wait(max(0.0, deadline - time.monotonic())):
            log.warning("run %d did not stop within %.0f s", t.process.pid, timeout)


def _ended_reason(returncode: int) -> str:
    if returncode < 0:
        try:
            how = f"killed by {signal.Signals(-returncode).name}"
        except ValueError:
            how = f"killed by signal {-returncode}"
    else:
        how = f"exit {returncode}"
    return f"{ENDED_UNEXPECTEDLY} ({how})"


def close_abandoned(state_db: Path, runs: list[dict[str, Any]], returncode: int) -> None:
    """Close the run records and outcomes, by exact id, that an exited run left open.

    A run that ends normally closes its own records, so this changes nothing then. A
    killed run cannot, and its collections' `status` would show it running forever
    (§spec:failure-behavior: the run record always closes). Exact ids, never "the
    open run of this collection": the next run may already hold the lock. A run that
    announced no records (a verify without `--reconform`) leaves nothing to close, and
    the database is not opened: that run held no lock, so nothing may migrate it.
    """
    if not runs:
        return
    from shakedown.db import connect
    from shakedown.state import OperationOutcomeRepo, RunRepo

    reason = _ended_reason(returncode)
    now = datetime.now()
    with contextlib.closing(connect(state_db)) as conn:
        run_repo = RunRepo(conn)
        outcome_repo = OperationOutcomeRepo(conn)
        for entry in runs:
            run_repo.close_abandoned(entry["run_id"], reason, now)
            if entry.get("outcome_id") is not None:
                outcome_repo.close_abandoned(entry["outcome_id"], reason, now)
