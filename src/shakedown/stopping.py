"""Stopping a writing run on request: SIGTERM (`docker stop`) or SIGINT (Ctrl-C)
(§spec:graceful-stop).

A writing command installs the handler while it holds the writer lock. The first
signal sets one process-wide flag; the run starts no new work, lets the step in hand
finish where cutting it short would leave something half done (a show's swap into
the archive, one file's tag write, one show's library links), abandons downloads
at their next chunk, records each run it cut off as `stopped`, and exits 143
(SIGTERM) or 130 (SIGINT). A second signal exits at once with the same code; the
crash recovery every run starts with covers what it cut off.

Python runs signal handlers on the main thread only, so workers never see the
signal itself: they read the flag at their checkpoints (`check`, `wait`).

Why a handler and not an init process: in a container the command is PID 1, and
the kernel drops every signal a namespace's init has left at its default action —
an uncaught SIGTERM is ignored and `docker stop` ends in SIGKILL. A signal with a
handler installed is delivered to PID 1 like to any process.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
from collections.abc import Iterator
from types import FrameType

log = logging.getLogger(__name__)

SIGNALS = (signal.SIGTERM, signal.SIGINT)

_stop = threading.Event()
_signum: int | None = None


class Stopped(BaseException):
    """Raised at a checkpoint once a stop was requested. A BaseException, so the
    `except Exception` guards that turn a fault into a failed show or a crashed run
    let it pass: a stop is neither. Caught where a run records itself stopped."""


def requested() -> bool:
    return _stop.is_set()


def check() -> None:
    """Raise `Stopped` if a stop was requested."""
    if _stop.is_set():
        raise Stopped()


def wait(seconds: float) -> bool:
    """Sleep up to ``seconds``, waking at once on a stop; True if a stop was requested."""
    return _stop.wait(seconds)


def request(signum: int = signal.SIGTERM) -> None:
    """Ask the running command to stop, as if it had received ``signum``: the signal
    handler's first step, and the hook tests call."""
    global _signum
    if _signum is None:
        _signum = signum
    _stop.set()


def exit_code() -> int:
    """The exit code of a stopped run: 128 + the signal number (143 for SIGTERM, 130
    for SIGINT), as shells and Docker report a process ended by that signal."""
    return 128 + (_signum if _signum is not None else signal.SIGTERM)


def reset() -> None:
    global _signum
    _signum = None
    _stop.clear()


def _handle(signum: int, _frame: FrameType | None) -> None:
    if _stop.is_set():
        # Second signal: exit now. The next run's crash recovery covers the rest.
        os._exit(exit_code())
    request(signum)
    log.warning(
        "%s received: stopping after the work in hand; send it again to exit at once",
        signal.Signals(signum).name,
    )


@contextlib.contextmanager
def handling_signals() -> Iterator[None]:
    """Turn SIGTERM and SIGINT into a stop request for the duration of the block,
    then put the previous handlers back. Off the main thread (where Python cannot
    install a handler) the block runs without one."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    reset()
    previous = {signum: signal.signal(signum, _handle) for signum in SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
