"""Runs that `serve` starts as separate processes (§spec:api-sync-runs).

A triggered sync or verify is the ordinary one-shot CLI run — the same command the
scheduler launches — so it reads the config file at its start and a crash never
takes `serve` down. `serve` still has to answer the HTTP request at once with the
run's id, or refuse it when another run holds the writer lock. The child tells it
which, over a readiness fd in the style of s6's `notification-fd`: serve passes the
write end of a pipe and names it in SHAKEDOWN_READY_FD, and the child writes exactly
one JSON line there, then closes it:

- ``{"state": "busy"}`` — another run holds the writer lock; the child exits 1.
- ``{"state": "failed", "error": "..."}`` — the child could not load its config.
- ``{"state": "started", "runs": [...]}`` — the child holds the lock and has opened
  every run record it will use; each entry names its run and outcome ids and scope.

Without SHAKEDOWN_READY_FD (CLI and scheduler runs) every announce is a no-op.
sd_notify was not used: it needs a datagram socket and its own message format, and
all we carry is one line.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

READY_FD_ENV = "SHAKEDOWN_READY_FD"


def _announce(message: dict[str, Any]) -> None:
    # Pop, so only the first announce counts and child processes of this run (a
    # handoff command) never see the variable.
    raw = os.environ.pop(READY_FD_ENV, None)
    if raw is None:
        return
    fd = int(raw)  # only serve sets it, always to the pipe's fd number
    try:
        os.write(fd, (json.dumps(message) + "\n").encode())
    except OSError as e:
        log.warning("could not tell serve the run's state: %s", e)
    finally:
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


def announce_started(runs: list[dict[str, Any]]) -> None:
    _announce({"state": "started", "runs": runs})


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

    Returns once the child has started (it holds the writer lock and has opened its
    run records), refused (busy), or failed before starting. A started child is
    watched on a daemon thread: when it exits, any of its run records it left open
    are closed, then ``on_exit`` gets its return code.
    """
    r, w = os.pipe()
    child_env = {**os.environ, **(env or {}), READY_FD_ENV: str(w)}
    try:
        process = subprocess.Popen(
            [*argv_prefix, "--config", str(config_path), *args],
            pass_fds=(w,),
            env=child_env,
        )
    finally:
        os.close(w)  # only the child holds the write end, so its exit means EOF
    with os.fdopen(r, "rb") as reader:
        line = reader.readline()
    message: dict[str, Any] = json.loads(line) if line else {}
    state = message.get("state")
    if state != "started":
        returncode = process.wait()
        if state == "busy":
            return Busy()
        if state == "failed":
            return Failed(str(message.get("error", "")))
        return ExitedEarly(returncode)

    triggered = TriggeredRun(process=process, runs=list(message.get("runs", [])))

    def watch() -> None:
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


def _ended_reason(returncode: int) -> str:
    if returncode < 0:
        try:
            how = f"killed by {signal.Signals(-returncode).name}"
        except ValueError:
            how = f"killed by signal {-returncode}"
    else:
        how = f"exit {returncode}"
    return f"run ended unexpectedly ({how})"


def close_abandoned(state_db: Path, runs: list[dict[str, Any]], returncode: int) -> None:
    """Close the run records and outcomes, by exact id, that an exited run left open.

    A run that ends normally closes its own records, so this changes nothing then. A
    killed run cannot, and its collections' `status` would show it running forever
    (§spec:failure-behavior: the run record always closes). Exact ids, never "the
    open run of this collection": the next run may already hold the lock.
    """
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
