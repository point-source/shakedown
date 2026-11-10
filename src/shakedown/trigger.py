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
from typing import Any

log = logging.getLogger(__name__)

READY_FD_ENV = "SHAKEDOWN_READY_FD"


def _announce(message: dict[str, Any]) -> None:
    # Pop, so only the first announce counts and child processes of this run (a
    # handoff command) never see the variable.
    raw = os.environ.pop(READY_FD_ENV, None)
    if raw is None:
        return
    try:
        fd = int(raw)
    except ValueError:
        log.warning("ignoring %s=%r: not a file descriptor", READY_FD_ENV, raw)
        return
    try:
        os.write(fd, (json.dumps(message) + "\n").encode())
    except OSError as e:
        log.warning("could not tell serve the run's state: %s", e)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def announce_busy() -> None:
    _announce({"state": "busy"})


def announce_failed(error: str) -> None:
    _announce({"state": "failed", "error": error})


def announce_started(runs: list[dict[str, Any]]) -> None:
    _announce({"state": "started", "runs": runs})
