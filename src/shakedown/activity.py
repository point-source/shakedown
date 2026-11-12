"""Live run progress (§spec:run-activity): each run records, per collection it works
on, an activity entry — its kind, phase, counters, heartbeat, and result — into the
state database, so the dashboard shows it whether or not `serve` was running when the
run started."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from shakedown.db import ACTIVITY_SINCE_VERSION, transaction
from shakedown.models import ENDED_UNEXPECTEDLY, OperationStatus
from shakedown.network import EventKind, NetworkEvent, NetworkMeter, NetworkRepo
from shakedown.plugins.base import ListingRetry, failure_cause, http_status, listing_retries_to
from shakedown.state import OperationOutcomeRepo, RunRepo

log = logging.getLogger(__name__)

# History is kept this long; trimming never touches item, run, or placement records
# (§spec:state).
HISTORY_DAYS = 30
# The live view keeps an ended entry this long, so its result and reason are seen.
LIVE_ENDED_S = 600


class ActivityKind(StrEnum):
    SYNC = "sync"
    CATCH_UP = "catch_up"
    REFETCH = "refetch"
    RECONFORM = "reconform"
    RESTAGE = "restage"
    VERIFY = "verify"
    RECONCILE = "reconcile"


class Phase(StrEnum):
    STARTING = "starting"
    LISTING = "listing"
    DISCOVERING = "discovering"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    STAGING = "staging"
    NOTIFYING = "notifying"
    VERIFYING = "verifying"
    FINISHING = "finishing"


RUNNING = "running"
ENDED = "ended"
# The one end reason: the run's heartbeat stopped — killed, crashed, or the host
# went down (§spec:run-activity).
STOPPED_REPORTING = "stopped_reporting"
ABANDONED_ERROR = f"{ENDED_UNEXPECTEDLY} (stopped reporting)"


@dataclass(frozen=True)
class RunCounts:
    """A run record's figures, written mid-run at the activity entry's cadence so
    `status` and the dashboard agree (§spec:run-activity)."""
    items_discovered: int
    items_new: int
    items_updated: int
    items_failed: int
    bytes_downloaded: int


@dataclass
class Snapshot:
    """An entry's progress at one moment: cumulative, so a skipped write loses
    nothing the next one does not carry."""
    phase: str | None = Phase.STARTING
    last_progress_at: datetime | None = None
    items_listed: int | None = None
    items_to_look_up: int | None = None
    items_looked_up: int | None = None
    items_to_fetch: int | None = None
    items_done: int | None = None
    items_failed: int | None = None
    current_items: list[str] = field(default_factory=list)
    bytes_downloaded: int | None = None
    bytes_to_fetch: int | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: int | None = None
    requests_in_flight: int | None = None
    run_counts: RunCounts | None = None
    # The operation outcome of a run with no run record (restage, reconcile), whose
    # errors the run history shows (§spec:run-history); None until it opens one.
    outcome_id: int | None = None


_COUNTER_COLUMNS = (
    "items_listed", "items_to_look_up", "items_looked_up", "items_to_fetch",
    "items_done", "items_failed",
)
_PROGRESS_COLUMNS = (
    "phase", "items_listed", "items_to_look_up", "items_looked_up", "items_to_fetch",
    "items_done", "items_failed", "bytes_downloaded", "bytes_to_fetch",
    "speed_bytes_per_second", "eta_seconds", "requests_in_flight",
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _live_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "activity_id": row["id"],
        "run_id": row["run_id"],
        "source": row["source_name"],
        "collection": row["collection_name"],
        "kind": row["kind"],
        "started_by_serve": bool(row["started_by_serve"]),
        "started_at": _parse(row["started_at"]),
        "finished_at": _parse(row["finished_at"]),
        "last_heartbeat_at": _parse(row["last_heartbeat_at"]),
        "last_progress_at": _parse(row["last_progress_at"]),
        "state": row["state"],
        "result": row["result"],
        "end_reason": row["end_reason"],
        "phase": row["phase"],
        **{column: row[column] for column in _COUNTER_COLUMNS},
        "current_items": json.loads(row["current_items"]),
        "bytes_downloaded": row["bytes_downloaded"],
        "bytes_to_fetch": row["bytes_to_fetch"],
        "speed_bytes_per_second": row["speed_bytes_per_second"],
        "eta_seconds": row["eta_seconds"],
    }


def _as_seen(entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A running entry that stopped reporting reads as ended at its last heartbeat,
    with ``failed_before_completion`` and ``stopped_reporting`` — before any closer
    has written that end (§spec:run-activity "Ends are never missed")."""
    if entry["state"] == RUNNING and stopped_reporting(entry["last_heartbeat_at"], now):
        entry.update(
            state=ENDED,
            result=OperationStatus.FAILED_BEFORE_COMPLETION.value,
            end_reason=STOPPED_REPORTING,
            finished_at=entry["last_heartbeat_at"],
            current_items=[],
            speed_bytes_per_second=None,
            eta_seconds=None,
        )
    return entry


class ActivityRepo:
    """The `run_activity` table (schema v10). A reader of an older schema has no
    table and sees no entries."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def has_table(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_activity'"
        ).fetchone() is not None

    def create(
        self,
        source: str,
        collection: str,
        kind: ActivityKind,
        *,
        run_id: int | None,
        started_by_serve: bool,
        at: datetime,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO run_activity (
                source_name, collection_name, kind, started_by_serve, run_id,
                started_at, last_heartbeat_at, last_progress_at, state, phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, collection, kind.value, int(started_by_serve), run_id,
             _iso(at), _iso(at), _iso(at), RUNNING, Phase.STARTING.value),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def restart(self, activity_id: int, at: datetime) -> None:
        """Move an entry opened ahead of time to when its work actually began."""
        self.conn.execute(
            "UPDATE run_activity SET started_at=? WHERE id=?", (_iso(at), activity_id)
        )

    def write(self, activity_id: int, snap: Snapshot, heartbeat_at: datetime,
              run_id: int | None = None) -> None:
        """Record a running entry's progress and heartbeat; with ``run_id``, move the
        open run record's figures to the same moment."""
        values = [getattr(snap, column) for column in _PROGRESS_COLUMNS]
        self.conn.execute(
            f"""
            UPDATE run_activity SET
                {", ".join(f"{column}=?" for column in _PROGRESS_COLUMNS)},
                current_items=?, last_heartbeat_at=?,
                last_progress_at=COALESCE(?, last_progress_at),
                outcome_id=COALESCE(?, outcome_id)
            WHERE id=? AND state=?
            """,
            (*values, json.dumps(snap.current_items), _iso(heartbeat_at),
             snap.last_progress_at and _iso(snap.last_progress_at), snap.outcome_id,
             activity_id, RUNNING),
        )
        if run_id is not None and snap.run_counts is not None:
            counts = snap.run_counts
            self.conn.execute(
                """
                UPDATE runs SET items_discovered=?, items_new=?, items_updated=?,
                    items_failed=?, bytes_downloaded=?
                WHERE id=? AND finished_at IS NULL
                """,
                (counts.items_discovered, counts.items_new, counts.items_updated,
                 counts.items_failed, counts.bytes_downloaded, run_id),
            )

    def end(
        self,
        activity_id: int,
        result: str,
        finished_at: datetime,
        snap: Snapshot | None = None,
    ) -> None:
        """Record an entry's result. Its last progress stays; nothing is in flight
        any more, so speed, time left, and current items clear."""
        if snap is not None:
            self.write(activity_id, snap, finished_at)
        self.conn.execute(
            """
            UPDATE run_activity SET state=?, result=?, finished_at=?,
                last_heartbeat_at=?, current_items='[]', speed_bytes_per_second=NULL,
                eta_seconds=NULL, requests_in_flight=NULL
            WHERE id=? AND state=?
            """,
            (ENDED, result, _iso(finished_at), _iso(finished_at), activity_id, RUNNING),
        )

    def last_heartbeat_for_run(self, run_id: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(last_heartbeat_at) AS hb FROM run_activity WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return datetime.fromisoformat(row["hb"]) if row and row["hb"] else None

    def close_stale(self, now: datetime) -> None:
        """End every running entry that stopped reporting, at its last heartbeat."""
        cutoff = now - timedelta(seconds=STALE_AFTER_S)
        self.conn.execute(
            """
            UPDATE run_activity SET state=?, result=?, end_reason=?,
                finished_at=last_heartbeat_at, current_items='[]',
                speed_bytes_per_second=NULL, eta_seconds=NULL, requests_in_flight=NULL
            WHERE state=? AND last_heartbeat_at < ?
            """,
            (ENDED, OperationStatus.FAILED_BEFORE_COMPLETION.value, STOPPED_REPORTING,
             RUNNING, _iso(cutoff)),
        )

    def live(self, now: datetime) -> list[dict[str, Any]]:
        """The live view (§spec:run-activity): running entries, then entries that
        ended in the last ten minutes, newest first. A running entry that stopped
        reporting reads as ended at its last heartbeat. Times are datetimes; every
        other value is as the API answers it. Empty before schema v10."""
        if not self.has_table():
            return []
        cutoff = now - timedelta(seconds=LIVE_ENDED_S)
        rows = self.conn.execute(
            "SELECT * FROM run_activity WHERE state=? OR finished_at >= ?",
            (RUNNING, _iso(cutoff)),
        ).fetchall()
        running: list[dict[str, Any]] = []
        ended: list[dict[str, Any]] = []
        for row in rows:
            entry = _as_seen(_live_entry(row), now)
            if entry["state"] == ENDED and entry["finished_at"] < cutoff:
                continue  # stopped reporting before the live view's horizon
            (running if entry["state"] == RUNNING else ended).append(entry)
        running.sort(key=lambda e: (e["started_at"], e["activity_id"]), reverse=True)
        ended.sort(key=lambda e: (e["finished_at"], e["activity_id"]), reverse=True)
        return running + ended

    def for_run(self, run_id: int, now: datetime) -> dict[str, Any] | None:
        """The entry of run record ``run_id`` as the live view reads it, or None (no
        entry, or a database before schema v10)."""
        if not self.has_table():
            return None
        row = self.conn.execute(
            "SELECT * FROM run_activity WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        return _as_seen(_live_entry(row), now) if row else None

    def since(self, started_at: datetime, now: datetime) -> list[dict[str, Any]]:
        """Every entry started at or after ``started_at``, as the live view reads it
        (a running entry that stopped reporting reads as ended), plus the
        ``outcome_id`` it names. Empty before schema v10."""
        if not self.has_table():
            return []
        rows = self.conn.execute(
            "SELECT * FROM run_activity WHERE started_at >= ?", (_iso(started_at),)
        ).fetchall()
        return [
            {**_as_seen(_live_entry(row), now), "outcome_id": row["outcome_id"]}
            for row in rows
        ]

    def trim(self, now: datetime) -> None:
        """Drop ended entries, and network history, older than the history window."""
        cutoff = now - timedelta(days=HISTORY_DAYS)
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM run_activity WHERE state=? AND finished_at < ?",
                (ENDED, _iso(cutoff)),
            )
            NetworkRepo(self.conn).trim(cutoff)


# How often a run writes its progress and heartbeat (§spec:run-activity: "every few
# seconds"). An entry whose heartbeat is older than STALE_AFTER_S stopped reporting.
TICK_S = 3.0
STALE_AFTER_S = 20.0
# A progress write waits this long for another writer before it is skipped: the next
# one carries cumulative totals, so nothing drifts.
_TICK_BUSY_TIMEOUT_MS = 250
# A run's last write — its result — waits as long as any other connection does.
_END_BUSY_TIMEOUT_MS = 5000
# Speed is reported over the last ~10 seconds; time left uses the last ten minutes.
SPEED_WINDOW_S = 10.0
ETA_WINDOW_S = 600.0

_LISTING = ("items_listed", "items_to_look_up", "items_looked_up")
_FETCHING = ("items_to_fetch", "items_done", "items_failed")

# Which counters each kind reports; the rest stay null (§spec:run-activity: "Kinds
# without a phase or counter leave it null (a restage lists nothing)").
_KIND_COUNTERS: dict[ActivityKind, tuple[str, ...]] = {
    ActivityKind.SYNC: _LISTING + _FETCHING,
    ActivityKind.CATCH_UP: _LISTING + _FETCHING,
    ActivityKind.REFETCH: ("items_to_look_up", "items_looked_up", *_FETCHING),
    ActivityKind.RECONFORM: ("items_to_look_up", "items_looked_up", *_FETCHING),
    ActivityKind.RESTAGE: _FETCHING,
    ActivityKind.VERIFY: _FETCHING,
    ActivityKind.RECONCILE: _LISTING,
}
_DOWNLOADING_KINDS = frozenset(
    {ActivityKind.SYNC, ActivityKind.CATCH_UP, ActivityKind.REFETCH, ActivityKind.RECONFORM}
)


def estimate_eta(
    *,
    bytes_to_fetch: int | None,
    bytes_downloaded: int,
    eta_speed: float,
    current_speed: float,
    items_remaining: int,
    pace: float,
) -> int | None:
    """Seconds left (§spec:run-activity "Honest totals"): remaining bytes over the
    speed of the last ten minutes, or — when the source lists no sizes — remaining
    shows over the pace so far. Null while speed is zero: never a guess."""
    if items_remaining <= 0:
        return 0
    if current_speed <= 0:
        return None
    if bytes_to_fetch is not None:
        if eta_speed <= 0:
            return None
        return round(max(bytes_to_fetch - bytes_downloaded, 0) / eta_speed)
    if pace <= 0:
        return None
    return round(items_remaining / pace)


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):  # a file the fetch just moved or replaced
                total += os.stat(os.path.join(root, name)).st_size
    return total


@dataclass
class _Fetch:
    """One fetch attempt's temp folder, measured by sampling: each growth since the
    last sample is fetched bytes; a shrink (a file the plugin dropped) only moves the
    baseline."""
    tmp_dir: Path
    last_size: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


class Progress:
    """One entry's live progress: the hooks a run calls. Every hook is an in-memory
    update under a lock — never a database write and never an exception — so
    recording costs the run nothing (§spec:run-activity). The recorder's timer reads
    it every few seconds.

    It also measures the entry's network activity (§spec:network-activity) into its
    source's ``meter``: bytes landing in fetch temp folders, requests in flight, and
    network events."""

    def __init__(
        self,
        kind: ActivityKind,
        *,
        source: str = "",
        collection: str = "",
        meter: NetworkMeter | None = None,
        run_id: int | None = None,
        now: Callable[[], datetime] = datetime.now,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kind = kind
        self.collection = collection
        # A detached entry (recording off) still measures; its meter is never written.
        self.meter = meter if meter is not None else NetworkMeter(source, now)
        # Set by the recorder once the entry is written; None while recording is off.
        self.activity_id: int | None = None
        self.run_id = run_id
        # A run with a run record supplies its figures; they move at this entry's
        # cadence (§spec:run-activity "status agrees").
        self.run_counts: Callable[[], RunCounts] | None = None
        # A run with no run record names the outcome that holds its errors.
        self.outcome_id: int | None = None
        self._now = now
        self._clock = clock
        self._lock = threading.Lock()
        self._phase: str | None = Phase.STARTING
        self._counts = dict.fromkeys(_KIND_COUNTERS[kind], 0)
        self._enumerated = False
        self._bytes_to_fetch = 0
        self._sizes_known = True
        self._working: dict[str, int] = {}
        self._fetches: list[_Fetch] = []
        self._fetched_bytes = 0
        self._requests = 0
        self._last_progress_at: datetime | None = None
        self._last_from_run: tuple[int, int] | None = None
        self._fetches_finished = 0
        self._first_fetch_at: float | None = None
        self._samples: deque[tuple[float, int]] = deque()

    # -- hooks the run calls --------------------------------------------------------

    def phase(self, phase: Phase) -> None:
        with self._lock:
            self._phase = phase.value

    def _bump(self, counter: str, by: int) -> None:
        if counter in self._counts and by:
            self._counts[counter] += by
            self._last_progress_at = self._now()

    def _set(self, counter: str, value: int) -> None:
        if counter in self._counts and self._counts[counter] != value:
            self._counts[counter] = value
            self._last_progress_at = self._now()

    def listed(self, n: int) -> None:
        with self._lock:
            self._set("items_listed", n)

    def to_look_up(self, n: int = 1) -> None:
        with self._lock:
            self._bump("items_to_look_up", n)

    def looked_up(self, n: int = 1) -> None:
        with self._lock:
            self._bump("items_looked_up", n)

    def to_fetch(self, size: int | None) -> None:
        """One more show to fetch, with its bytes to fetch (None when unknown)."""
        with self._lock:
            self._bump("items_to_fetch", 1)
            if size is None:
                self._sizes_known = False
            else:
                self._bytes_to_fetch += size

    def work_total(self, n: int) -> None:
        """The shows a non-fetching run works through, known up front (restage,
        verify)."""
        with self._lock:
            self._set("items_to_fetch", n)
            self._enumerated = True

    def enumeration_done(self) -> None:
        """Shows to fetch, and their bytes, are known from here on."""
        with self._lock:
            self._enumerated = True

    def done(self, n: int = 1) -> None:
        with self._lock:
            self._bump("items_done", n)

    def failed(self, n: int = 1) -> None:
        with self._lock:
            self._bump("items_failed", n)

    def working(self, identifier: str) -> None:
        with self._lock:
            self._working[identifier] = self._working.get(identifier, 0) + 1

    def idle(self, identifier: str) -> None:
        with self._lock:
            left = self._working.get(identifier, 0) - 1
            if left > 0:
                self._working[identifier] = left
            else:
                self._working.pop(identifier, None)

    @contextlib.contextmanager
    def fetch_attempt(self, identifier: str, tmp_dir: Path) -> Iterator[None]:
        """One plugin fetch attempt writing into its core-owned temp folder: its bytes
        are what lands there (§spec:network-activity), sampled by the timer and once
        more when the attempt returns. Files already there — kept from an earlier
        attempt — are not counted; neither is anything the core adds afterwards."""
        fetch = _Fetch(tmp_dir, _tree_size(tmp_dir) if tmp_dir.exists() else 0)
        with self._lock:
            self._fetches.append(fetch)
            if self._first_fetch_at is None:
                self._first_fetch_at = self._clock()
        try:
            yield
        finally:
            self._sample(fetch, close=True)
            with self._lock:
                self._fetches.remove(fetch)

    def _sample(self, fetch: _Fetch, *, close: bool = False) -> None:
        with fetch.lock:
            if fetch.closed:
                return
            size = _tree_size(fetch.tmp_dir) if fetch.tmp_dir.exists() else 0
            grown = size - fetch.last_size
            fetch.last_size = size
            fetch.closed = close
        if grown > 0:
            with self._lock:
                self._fetched_bytes += grown
                self._last_progress_at = self._now()
            self.meter.add_bytes(grown)

    def fetch_finished(self) -> None:
        """A show's fetch is done, however it ended (for the pace estimate)."""
        with self._lock:
            self._fetches_finished += 1

    def bytes_fetched(self) -> int:
        """Bytes this entry fetched from the source so far (§spec:network-activity
        "Bytes downloaded means bytes fetched")."""
        with self._lock:
            return self._fetched_bytes

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        """Work held under the source's request budget: one request in flight."""
        with self._lock:
            self._requests += 1
        self.meter.request_started()
        try:
            yield
        finally:
            self.meter.request_ended()
            with self._lock:
                self._requests -= 1

    def network_event(
        self,
        kind: EventKind,
        *,
        identifier: str | None = None,
        http_status: int | None = None,
        wait_seconds: float | None = None,
        detail: str | None = None,
    ) -> None:
        self.meter.event(NetworkEvent(
            at=self._now(), kind=kind, http_status=http_status,
            collection=self.collection or None, identifier=identifier,
            wait_seconds=wait_seconds, detail=detail,
        ))

    def _listing_retry(self, retry: ListingRetry) -> None:
        if retry.cause is not None:
            self.network_event(EventKind(retry.cause.value), http_status=retry.http_status,
                               detail=retry.detail)
        self.network_event(EventKind.RETRY, detail=retry.detail)
        if retry.wait_seconds:
            self.network_event(EventKind.BACKOFF, wait_seconds=retry.wait_seconds)

    @contextlib.contextmanager
    def listing(self) -> Iterator[None]:
        """The collection listing, running: one request in flight, hearing the
        plugin's own retries of it. A listing that fails records its cause."""
        with self.request(), listing_retries_to(self._listing_retry):
            try:
                yield
            except Exception as e:
                cause = failure_cause(e)
                if cause is not None:
                    self.network_event(EventKind(cause.value), http_status=http_status(e),
                                       detail=str(e))
                raise

    # -- the recorder's side --------------------------------------------------------

    def snapshot(self, *, final: bool = False) -> Snapshot:
        """Cumulative progress now. Samples in-flight temp folders, so it runs on
        the recorder's timer, outside the run's own work."""
        with self._lock:
            fetches = list(self._fetches)
        for fetch in fetches:
            self._sample(fetch)
        counts = self.run_counts() if self.run_counts is not None else None
        now_t = self._clock()
        with self._lock:
            downloading = self.kind in _DOWNLOADING_KINDS
            total = self._fetched_bytes
            self._samples.append((now_t, total))
            while self._samples and self._samples[0][0] < now_t - ETA_WINDOW_S:
                self._samples.popleft()
            speed = self._speed(now_t, SPEED_WINDOW_S)
            eta_speed = self._speed(now_t, ETA_WINDOW_S)
            values: dict[str, int | None] = {c: self._counts.get(c) for c in _COUNTER_COLUMNS}
            if counts is not None:
                # Done and failed are the run record's own figures, so the entry and
                # `status` always agree (§spec:run-activity "status agrees").
                from_run = (counts.items_new + counts.items_updated, counts.items_failed)
                values["items_done"], values["items_failed"] = from_run
                if from_run != self._last_from_run:
                    if self._last_from_run is not None:
                        self._last_progress_at = self._now()
                    self._last_from_run = from_run
            if not self._enumerated and self.kind in _DOWNLOADING_KINDS:
                values["items_to_fetch"] = None
            bytes_to_fetch = (
                self._bytes_to_fetch
                if downloading and self._enumerated and self._sizes_known else None
            )
            eta = None
            if downloading and self._enumerated and not final:
                handled = self._fetches_finished
                elapsed = now_t - self._first_fetch_at if self._first_fetch_at else 0.0
                eta = estimate_eta(
                    bytes_to_fetch=bytes_to_fetch,
                    bytes_downloaded=total,
                    eta_speed=eta_speed,
                    current_speed=speed,
                    items_remaining=self._counts["items_to_fetch"] - handled,
                    pace=handled / elapsed if elapsed > 0 else 0.0,
                )
            if counts is not None:
                counts = RunCounts(
                    items_discovered=counts.items_discovered,
                    items_new=counts.items_new,
                    items_updated=counts.items_updated,
                    items_failed=counts.items_failed,
                    bytes_downloaded=total,
                )
            return Snapshot(
                phase=self._phase,
                last_progress_at=self._last_progress_at,
                current_items=list(self._working),
                bytes_downloaded=total if downloading else None,
                bytes_to_fetch=bytes_to_fetch,
                speed_bytes_per_second=(None if final else speed) if downloading else None,
                eta_seconds=eta,
                requests_in_flight=None if final else self._requests,
                run_counts=counts,
                outcome_id=self.outcome_id,
                **values,
            )

    def _speed(self, now_t: float, window: float) -> float:
        """Bytes per second over the samples within ``window`` of now."""
        if len(self._samples) < 2:
            return 0.0
        start_t, start_b = self._samples[0]
        for t, b in self._samples:
            if t >= now_t - window:
                break
            start_t, start_b = t, b
        end_t, end_b = self._samples[-1]
        if end_t <= start_t:
            return 0.0
        return (end_b - start_b) / (end_t - start_t)


_started_by_serve: bool | None = None


def note_started_by_serve(started: bool) -> None:
    """Remember, at process start, whether `serve` launched this run: the first
    announce to serve consumes the evidence."""
    global _started_by_serve
    _started_by_serve = started


def _launched_by_serve() -> bool:
    if _started_by_serve is not None:
        return _started_by_serve
    from shakedown.trigger import launched_by_serve

    return launched_by_serve()


class ActivityRecorder:
    """Writes a run's activity entries: one per collection it works on, updated by a
    timer every few seconds on a connection of its own (§spec:run-activity).

    Recording never costs the run anything: a write that cannot land (database busy)
    is skipped and the next carries cumulative totals; any failure is logged once and
    never raised. The recorder never migrates: on a database older than schema v10
    (a lock-free `verify` right after an upgrade) it records nothing."""

    def __init__(
        self,
        db_path: Path,
        *,
        now: Callable[[], datetime] = datetime.now,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float | None = None,
        start_timer: bool = True,
    ) -> None:
        self._now = now
        self._clock = clock
        self._tick_s = TICK_S if tick_s is None else tick_s
        self._lock = threading.Lock()
        self._entries: list[Progress] = []
        # One meter per configured source, shared by its collections' entries.
        self._meters: dict[str, NetworkMeter] = {}
        self._warned = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_by_serve = _launched_by_serve()
        self._conn = self._open(db_path)
        if self._conn is not None and start_timer:
            self._thread = threading.Thread(
                target=self._run_timer, name="shakedown-activity", daemon=True
            )
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def _open(self, db_path: Path) -> sqlite3.Connection | None:
        try:
            conn = sqlite3.connect(
                db_path.resolve().as_uri() + "?mode=rw", uri=True,
                isolation_level=None, check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_TICK_BUSY_TIMEOUT_MS}")
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None or row["version"] < ACTIVITY_SINCE_VERSION:
                conn.close()
                return None
            return conn
        except Exception as e:  # recording is never worth failing a run over
            self._warn(e)
            return None

    def _warn(self, e: BaseException) -> None:
        if not self._warned:
            self._warned = True
            log.warning("activity: could not record progress: %s", e)

    def open_entry(
        self,
        source: str,
        collection: str,
        kind: ActivityKind,
        *,
        run_id: int | None = None,
    ) -> Progress:
        """Start an entry for one collection; returns its hooks. With recording off
        the hooks still work and simply go nowhere."""
        if self._conn is None:
            return Progress(kind, source=source, collection=collection, run_id=run_id,
                            now=self._now, clock=self._clock)
        with self._lock:
            meter = self._meters.setdefault(source, NetworkMeter(source, self._now))
        progress = Progress(kind, source=source, collection=collection, meter=meter,
                            run_id=run_id, now=self._now, clock=self._clock)
        try:
            with self._lock:
                repo = ActivityRepo(self._conn)
                with transaction(self._conn):
                    progress.activity_id = repo.create(
                        source, collection, kind, run_id=run_id,
                        started_by_serve=self._started_by_serve, at=self._now(),
                    )
                    repo.write(progress.activity_id, progress.snapshot(), self._now())
                self._entries.append(progress)
        except Exception as e:
            self._warn(e)
        return progress

    def restart(self, progress: Progress) -> None:
        """The entry's work begins now (it was opened ahead of time)."""
        if self._conn is None or progress.activity_id is None:
            return
        try:
            with self._lock:
                ActivityRepo(self._conn).restart(progress.activity_id, self._now())
        except Exception as e:
            self._warn(e)

    def tick(self) -> None:
        """Write every open entry's progress and heartbeat in one small transaction."""
        if self._conn is None:
            return
        try:
            with self._lock:
                entries = [p for p in self._entries if p.activity_id is not None]
                if not entries:
                    return
                snaps = [(p, p.snapshot()) for p in entries]
                now = self._now()
                repo = ActivityRepo(self._conn)
                with self._writing_network(self._conn):
                    for progress, snap in snaps:
                        assert progress.activity_id is not None
                        repo.write(progress.activity_id, snap, now, progress.run_id)
        except Exception as e:
            self._warn(e)

    @contextlib.contextmanager
    def _writing_network(self, conn: sqlite3.Connection) -> Iterator[None]:
        """One transaction holding the caller's writes and every source's pending
        network totals and events. When it cannot land, what it took goes back to the
        meters, so the next write carries it and nothing drifts."""
        taken = [(meter, *meter.take()) for meter in self._meters.values()]
        try:
            with transaction(conn):
                yield
                repo = NetworkRepo(conn)
                for meter, minutes, events in taken:
                    repo.add_minutes(meter.source, minutes)
                    repo.add_events(meter.source, events)
        except BaseException:
            for meter, minutes, events in taken:
                meter.give_back(minutes, events)
            raise

    def end(self, progress: Progress, result: str) -> None:
        """Record the entry's result with its final figures."""
        if self._conn is None or progress.activity_id is None:
            return
        try:
            with self._lock:
                if progress in self._entries:
                    self._entries.remove(progress)
                snap = progress.snapshot(final=True)
                self._conn.execute(f"PRAGMA busy_timeout={_END_BUSY_TIMEOUT_MS}")
                try:
                    with self._writing_network(self._conn):
                        ActivityRepo(self._conn).end(
                            progress.activity_id, result, self._now(), snap
                        )
                finally:
                    self._conn.execute(f"PRAGMA busy_timeout={_TICK_BUSY_TIMEOUT_MS}")
        except Exception as e:
            self._warn(e)

    def close(self) -> None:
        """Stop the timer, write any network activity still pending, and trim history
        older than 30 days. An entry still open here was never ended; it reads as
        stopped reporting once its heartbeat ages."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(f"PRAGMA busy_timeout={_END_BUSY_TIMEOUT_MS}")
                with self._writing_network(self._conn):
                    pass
                ActivityRepo(self._conn).trim(self._now())
        except Exception as e:
            self._warn(e)
        finally:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def __enter__(self) -> ActivityRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run_timer(self) -> None:
        while not self._stop.wait(self._tick_s):
            self.tick()


def stopped_reporting(last_heartbeat_at: datetime, now: datetime) -> bool:
    """Whether a running entry last heard from at ``last_heartbeat_at`` stopped
    reporting: more than about 20 seconds without a heartbeat."""
    return now - last_heartbeat_at > timedelta(seconds=STALE_AFTER_S)


def close_abandoned(conn: sqlite3.Connection, now: datetime) -> None:
    """Close what killed runs left open (§spec:run-activity "Ends are never missed").
    Only a run holding the writer lock calls this, before it opens its own records:
    every other locked run has then ended, so each open run record and in-progress
    outcome was abandoned. A run record closes at its last heartbeat (at its start
    when it has none — it predates heartbeats). Running entries close only once they
    stopped reporting: a lock-free `verify` may still be alive."""
    activity = ActivityRepo(conn)
    has_activity = activity.has_table()
    runs = RunRepo(conn)
    outcomes = OperationOutcomeRepo(conn)
    with transaction(conn):
        for run_id, started_at in runs.open_runs():
            heartbeat = activity.last_heartbeat_for_run(run_id) if has_activity else None
            runs.close_abandoned(run_id, ABANDONED_ERROR, heartbeat or started_at)
            log.warning("closed run %s: %s", run_id, ABANDONED_ERROR)
        for outcome in outcomes.in_progress():
            assert outcome.id is not None
            outcomes.close_abandoned(outcome.id, ABANDONED_ERROR, outcome.updated_at)
        if has_activity:
            activity.close_stale(now)
