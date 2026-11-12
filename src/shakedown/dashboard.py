"""Shared machinery behind the dashboard's read endpoints (`GET /api/*`,
§spec:dashboard).

Every view is a `GET` that answers from the state database (opened with
`connect_reader`: no migration, no writer lock) and the config file. `serve`
computes each view's answer at most once per refresh interval and hands the same
answer, with the same `generated_at`, to every viewer, so ten open pages cost the
NAS what one does.

A view with no query parameters registers with one line in `build_app`::

    register_api(app, "/api/health", VIEW_TTL_S, health_answer)

A view that takes query parameters validates them in its own route (so a bad
value is a `422` that never becomes a cache entry) and calls `shared_answer` with
a key holding the validated values.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Hashable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from shakedown.config import Config, ConfigError, load
from shakedown.db import connect_empty, connect_reader

# Refresh intervals (§spec:dashboard): the live view every few seconds, every other
# view about every 30 seconds. An answer is computed at most once per interval.
LIVE_TTL_S = 5.0
VIEW_TTL_S = 30.0


def iso(dt: datetime | None) -> str | None:
    """An `/api/*` timestamp: ISO 8601 with its UTC offset, so a browser in another
    time zone reads the same instant. Stored times are naive serve-local times."""
    return dt.astimezone().isoformat() if dt else None


@dataclass(frozen=True)
class ConfigState:
    """The config file as of the latest load: ``error`` is the reason it does not
    load now (None when it does), ``last_loaded_at`` the time of the last load that
    succeeded, whose config the views answer from."""
    error: str | None
    last_loaded_at: datetime | None


class ConfigTracker:
    """Loads the config file at each request and remembers the last config that
    loaded, so the dashboard keeps answering while the file is broken
    (§spec:dashboard "When the config is broken")."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_good: Config | None = None
        self._state = ConfigState(error=None, last_loaded_at=None)

    def load_now(self) -> Config:
        """The config as the file is now; raises ConfigError when it does not load."""
        try:
            config = load(self.path)
        except ConfigError as e:
            with self._lock:
                self._state = ConfigState(str(e), self._state.last_loaded_at)
            raise
        with self._lock:
            self._last_good = config
            self._state = ConfigState(None, datetime.now())
        return config

    def for_dashboard(self) -> tuple[Config, ConfigState]:
        """The config to answer from: the file as it is now, else the last config
        this serve loaded. A serve that never loaded one answers 503 with the
        error, as the other endpoints do."""
        try:
            self.load_now()
        except ConfigError as e:
            if self._last_good is None:
                raise HTTPException(503, str(e)) from e
        with self._lock:
            assert self._last_good is not None
            return self._last_good, self._state


@dataclass
class DashboardContext:
    """What a view's builder reads: the config in use, the state of the config file,
    and a reader connection."""
    config: Config
    config_state: ConfigState
    conn: sqlite3.Connection


Builder = Callable[[DashboardContext], dict[str, Any]]


@dataclass
class _Slot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    answer: dict[str, Any] | None = None
    computed_at: float = 0.0


class SharedAnswers:
    """One cached answer per key, recomputed once its interval has passed.

    Routes run in a threadpool, so concurrent viewers of an expired answer wait on
    the key's lock and all receive the one answer computed for them. A failure
    (an `HTTPException`) is raised to the viewer and never cached."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._slots: dict[Hashable, _Slot] = {}

    def get(
        self, key: Hashable, ttl: float, compute: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        with self._lock:
            slot = self._slots.setdefault(key, _Slot())
        with slot.lock:
            now = self._clock()
            if slot.answer is not None and now - slot.computed_at < ttl:
                return slot.answer
            slot.answer = compute()
            slot.computed_at = now
            return slot.answer


def shared_answer(app: FastAPI, key: Hashable, ttl: float, build: Builder) -> dict[str, Any]:
    """The view's shared answer for ``key``: computed by ``build`` over the current
    config and a reader connection, stamped with `generated_at`."""

    def compute() -> dict[str, Any]:
        configs: ConfigTracker = app.state.configs
        config, config_state = configs.for_dashboard()
        assert config.state_db is not None  # filled in by Config validation
        # Answering never writes, so a fresh install's first GET must not create the
        # database: before any run it answers from an empty one.
        exists = config.state_db.exists()
        with closing(connect_reader(config.state_db) if exists else connect_empty()) as conn:
            body = build(DashboardContext(config, config_state, conn))
        return {"generated_at": iso(datetime.now()), **body}

    answers: SharedAnswers = app.state.answers
    return answers.get(key, ttl, compute)


def register_api(app: FastAPI, path: str, ttl: float, build: Builder) -> None:
    """Serve ``build``'s shared answer at `GET path`. `GET` only: any other method
    is a `405`."""

    @app.get(path)
    def view() -> dict[str, Any]:
        return shared_answer(app, path, ttl, build)
