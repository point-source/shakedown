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

import logging
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

log = logging.getLogger(__name__)

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

    def file_version(self) -> Hashable:
        """Which file is in place now, by its stat: a dashboard save replaces the
        file and a hand edit rewrites it, and either changes this. None while the
        file can't be read."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

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

    def last_good(self) -> Config | None:
        """The file as it is now, else the last config this serve loaded; None when
        it never loaded one."""
        try:
            return self.load_now()
        except ConfigError:
            with self._lock:
                return self._last_good

    def for_dashboard(self) -> tuple[Config, ConfigState]:
        """The config to answer from: the file as it is now, else the last config
        this serve loaded. A serve that never loaded one answers 503 with the
        error, as the other endpoints do."""
        try:
            config = self.load_now()
        except ConfigError as e:
            with self._lock:
                if self._last_good is None:
                    raise HTTPException(503, str(e)) from e
                return self._last_good, self._state
        # What this request read, not `_last_good`: another request may publish an
        # older read in between, and the answer is cached under this file version.
        with self._lock:
            return config, self._state


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
    # Held while the key's answer is computed: one computation per key at a time.
    computing: threading.Lock = field(default_factory=threading.Lock)
    answer: dict[str, Any] | None = None
    computed_at: float = 0.0
    version: Hashable = None  # the config file the answer was built from
    refreshing: bool = False  # a refresh runs behind the answer being served
    failed: bool = False  # the last refresh failed: the answer is not served again


Spawn = Callable[[Callable[[], None]], None]


def in_thread(work: Callable[[], None]) -> None:
    """Run ``work`` on a thread of its own: how `serve` refreshes an answer."""
    threading.Thread(target=work, name="dashboard-refresh", daemon=True).start()


class SharedAnswers:
    """One cached answer per key, refreshed behind the viewers once its interval has
    passed (§spec:dashboard "Live where it matters, light everywhere").

    Within its interval an answer is served as it is. For one more interval it is
    still served at once, while one refresh (started by ``spawn``, a thread by
    default) computes the next. Older than that — or once a refresh has failed —
    it is no longer served: the viewer computes the answer, or waits for the
    computation already running, so the page's freshness rule (no answer for two
    intervals means stale) stays true. An answer built from another ``version`` of
    the config file is never served either, so a saved change shows at once. A
    failure (an `HTTPException`) is raised to the viewer that computed it and never
    cached."""

    def __init__(self, clock: Callable[[], float], spawn: Spawn = in_thread) -> None:
        self._clock = clock
        self._spawn = spawn
        self._lock = threading.Lock()
        self._slots: dict[Hashable, _Slot] = {}

    def get(
        self,
        key: Hashable,
        ttl: float,
        compute: Callable[[], dict[str, Any]],
        version: Hashable = None,
    ) -> dict[str, Any]:
        with self._lock:
            slot = self._slots.setdefault(key, _Slot())
            answer, age = slot.answer, self._clock() - slot.computed_at
            servable = (answer is not None and not slot.failed and age < 2 * ttl
                        and slot.version == version)
            refresh = servable and age >= ttl and not slot.refreshing
            if refresh:
                slot.refreshing = True
            seen = slot.computed_at
        if refresh:
            self._spawn(lambda: self._refresh(slot, compute, seen, version))
        if servable:
            # The latest answer: one a synchronous spawn just refreshed is in place.
            assert slot.answer is not None
            return slot.answer
        return self._compute_now(slot, ttl, compute, version)

    def _compute_now(
        self,
        slot: _Slot,
        ttl: float,
        compute: Callable[[], dict[str, Any]],
        version: Hashable,
    ) -> dict[str, Any]:
        seen = slot.computed_at
        with slot.computing:
            if slot.answer is not None and not slot.failed and slot.version == version and (
                slot.computed_at != seen or self._clock() - slot.computed_at < ttl
            ):
                return slot.answer  # computed while this viewer waited
            answer = compute()
            with self._lock:
                # Dated when ready, as generated_at is: a computation slower than the
                # interval still yields an answer viewers are served.
                slot.answer, slot.computed_at, slot.failed = answer, self._clock(), False
                slot.version = version
            return answer

    def _refresh(
        self,
        slot: _Slot,
        compute: Callable[[], dict[str, Any]],
        seen: float,
        version: Hashable,
    ) -> None:
        with slot.computing:
            if slot.computed_at != seen:
                # A viewer computed a newer answer before this refresh started.
                with self._lock:
                    slot.refreshing = False
                return
            try:
                answer = compute()
            except Exception:
                log.warning("dashboard answer could not be refreshed", exc_info=True)
                with self._lock:
                    slot.failed, slot.refreshing = True, False
                return
            with self._lock:
                slot.answer, slot.computed_at, slot.version = answer, self._clock(), version
                slot.failed = slot.refreshing = False


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
    configs: ConfigTracker = app.state.configs
    return answers.get(key, ttl, compute, configs.file_version())


def register_api(app: FastAPI, path: str, ttl: float, build: Builder) -> None:
    """Serve ``build``'s shared answer at `GET path`. `GET` only: any other method
    is a `405`."""

    @app.get(path)
    def view() -> dict[str, Any]:
        return shared_answer(app, path, ttl, build)
