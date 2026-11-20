"""A viewer never waits on a recompute (§spec:dashboard "Live where it matters, light
everywhere"): once an answer's interval passes, viewers get it at once while one
refresh runs behind it, for up to a second interval. Older than that, or after a
refresh failed, the viewer waits on the recompute as before, so old data is never
passed off as current."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from shakedown.dashboard import SharedAnswers

TTL = 30.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Threads:
    """Starts each refresh on a real thread and remembers it, so a test can wait."""

    def __init__(self) -> None:
        self.started: list[threading.Thread] = []

    def __call__(self, work: Callable[[], None]) -> None:
        thread = threading.Thread(target=work, daemon=True)
        self.started.append(thread)
        thread.start()

    def join(self) -> None:
        for thread in self.started:
            thread.join(timeout=5)
            assert not thread.is_alive()


class _Builder:
    """Answers {"n": <build number>}; a build can be held until released."""

    def __init__(self) -> None:
        self.builds = 0
        self.hold: threading.Event | None = None
        self.entered = threading.Event()
        self.fail: Exception | None = None

    def __call__(self) -> dict[str, Any]:
        self.builds += 1
        self.entered.set()
        if self.hold is not None:
            assert self.hold.wait(timeout=5)
        if self.fail is not None:
            raise self.fail
        return {"n": self.builds}


def _answers() -> tuple[SharedAnswers, _Clock, _Threads, _Builder]:
    clock, threads = _Clock(), _Threads()
    return SharedAnswers(clock, spawn=threads), clock, threads, _Builder()


def test_an_expired_answer_is_served_at_once_while_one_refresh_runs() -> None:
    answers, clock, threads, build = _answers()
    assert answers.get("k", TTL, build) == {"n": 1}

    clock.now = TTL
    build.hold = threading.Event()
    build.entered.clear()
    # Both viewers get the old answer without waiting; only one refresh starts.
    assert answers.get("k", TTL, build) == {"n": 1}
    assert build.entered.wait(timeout=5)
    assert answers.get("k", TTL, build) == {"n": 1}
    assert len(threads.started) == 1 and build.builds == 2

    build.hold.set()
    threads.join()
    assert answers.get("k", TTL, build) == {"n": 2}
    assert build.builds == 2


def test_an_answer_two_intervals_old_is_recomputed_while_the_viewer_waits() -> None:
    answers, clock, threads, build = _answers()
    answers.get("k", TTL, build)
    clock.now = 2 * TTL
    assert answers.get("k", TTL, build) == {"n": 2}
    assert threads.started == []


def test_a_viewer_waits_on_a_refresh_still_running_once_its_answer_is_too_old() -> None:
    """The one recompute serves everybody: a viewer arriving after the second interval
    waits for the refresh already running rather than starting another."""
    answers, clock, threads, build = _answers()
    answers.get("k", TTL, build)
    clock.now = TTL
    build.hold = threading.Event()
    build.entered.clear()
    answers.get("k", TTL, build)
    assert build.entered.wait(timeout=5)

    clock.now = 2 * TTL + 1
    got: list[dict[str, Any]] = []
    waiter = threading.Thread(target=lambda: got.append(answers.get("k", TTL, build)))
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive()  # waiting on the running refresh
    build.hold.set()
    waiter.join(timeout=5)
    threads.join()
    assert got == [{"n": 2}] and build.builds == 2


def test_a_failed_refresh_is_never_served_as_current() -> None:
    """After a refresh fails, the old answer is no longer handed out: the next viewer
    recomputes and receives the error while it lasts, then the new answer."""
    answers, clock, threads, build = _answers()
    answers.get("k", TTL, build)
    clock.now = TTL
    build.fail = HTTPException(503, "config broken")
    assert answers.get("k", TTL, build) == {"n": 1}  # the refresh runs behind it
    threads.join()

    with pytest.raises(HTTPException):
        answers.get("k", TTL, build)
    build.fail = None
    assert answers.get("k", TTL, build) == {"n": 4}
    assert answers.get("k", TTL, build) == {"n": 4}


def test_an_answer_is_dated_when_its_computation_finished() -> None:
    """A computation slower than the interval still yields an answer viewers are
    served: its age counts from when it was ready, as generated_at does."""
    answers, clock, threads, build = _answers()

    def slow() -> dict[str, Any]:
        clock.now += 70  # longer than two intervals
        return build()

    answers.get("k", TTL, slow)  # ready at 70
    clock.now = 70 + TTL  # expired: served while one refresh runs
    build.hold = threading.Event()
    assert answers.get("k", TTL, slow) == {"n": 1}
    build.hold.set()
    threads.join()  # the refresh finished at 170
    clock.now = 171
    assert answers.get("k", TTL, slow) == {"n": 2}
    assert build.builds == 2 and len(threads.started) == 1
