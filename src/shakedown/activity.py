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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from shakedown.db import ACTIVITY_SINCE_VERSION, transaction

log = logging.getLogger(__name__)

# History is kept this long; trimming never touches item, run, or placement records
# (§spec:state).
HISTORY_DAYS = 30


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
    run_counts: RunCounts | None = None


_PROGRESS_COLUMNS = (
    "phase", "items_listed", "items_to_look_up", "items_looked_up", "items_to_fetch",
    "items_done", "items_failed", "bytes_downloaded", "bytes_to_fetch",
    "speed_bytes_per_second", "eta_seconds",
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


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
                last_progress_at=COALESCE(?, last_progress_at)
            WHERE id=? AND state=?
            """,
            (*values, json.dumps(snap.current_items), _iso(heartbeat_at),
             snap.last_progress_at and _iso(snap.last_progress_at), activity_id, RUNNING),
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
        *,
        end_reason: str | None = None,
    ) -> None:
        """Record an entry's result. Its last progress stays; nothing is in flight
        any more, so speed, time left, and current items clear."""
        if snap is not None:
            self.write(activity_id, snap, finished_at)
        self.conn.execute(
            """
            UPDATE run_activity SET state=?, result=?, end_reason=?, finished_at=?,
                last_heartbeat_at=?, current_items='[]', speed_bytes_per_second=NULL,
                eta_seconds=NULL
            WHERE id=? AND state=?
            """,
            (ENDED, result, end_reason, _iso(finished_at), _iso(finished_at),
             activity_id, RUNNING),
        )

    def trim(self, now: datetime) -> None:
        """Drop ended entries older than the history window."""
        cutoff = now - timedelta(days=HISTORY_DAYS)
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM run_activity WHERE state=? AND finished_at < ?",
                (ENDED, _iso(cutoff)),
            )


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

_COUNTERS = (
    "items_listed", "items_to_look_up", "items_looked_up", "items_to_fetch",
    "items_done", "items_failed",
)
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


class Progress:
    """One entry's live progress: the hooks a run calls. Every hook is an in-memory
    update under a lock — never a database write and never an exception — so
    recording costs the run nothing (§spec:run-activity). The recorder's timer reads
    it every few seconds."""

    def __init__(
        self,
        kind: ActivityKind,
        *,
        activity_id: int | None = None,
        run_id: int | None = None,
        now: Callable[[], datetime] = datetime.now,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kind = kind
        self.activity_id = activity_id
        self.run_id = run_id
        # A run with a run record supplies its figures; they move at this entry's
        # cadence (§spec:run-activity "status agrees").
        self.run_counts: Callable[[], RunCounts] | None = None
        self._now = now
        self._clock = clock
        self._lock = threading.Lock()
        self._phase: str | None = Phase.STARTING
        self._counts = dict.fromkeys(_KIND_COUNTERS[kind], 0)
        self._enumerated = False
        self._bytes_to_fetch = 0
        self._sizes_known = True
        self._working: dict[str, int] = {}
        self._in_flight: dict[str, tuple[Path, int]] = {}
        self._own_bytes = 0
        self._reported_bytes = 0
        self._last_progress_at: datetime | None = None
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

    def add_bytes(self, n: int) -> None:
        """Bytes of a finished fetch, for a run that supplies no run counts."""
        with self._lock:
            self._own_bytes += n

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

    def fetch_started(self, identifier: str, tmp_dir: Path) -> None:
        """A fetch begins writing into its core-owned temp folder: its bytes are
        measured from what lands there (§spec:network-activity), not counting files
        kept from an earlier attempt."""
        baseline = _tree_size(tmp_dir)
        with self._lock:
            self._in_flight[identifier] = (tmp_dir, baseline)
            if self._first_fetch_at is None:
                self._first_fetch_at = self._clock()

    def fetch_finished(self, identifier: str) -> None:
        with self._lock:
            self._in_flight.pop(identifier, None)

    # -- the recorder's side --------------------------------------------------------

    def snapshot(self, *, final: bool = False) -> Snapshot:
        """Cumulative progress now. Measures in-flight temp folders, so it runs on
        the recorder's timer, outside the run's own work."""
        with self._lock:
            in_flight = list(self._in_flight.values())
        measured = 0 if final else sum(
            max(_tree_size(tmp_dir) - baseline, 0) for tmp_dir, baseline in in_flight
        )
        counts = self.run_counts() if self.run_counts is not None else None
        now_t = self._clock()
        with self._lock:
            downloading = self.kind in _DOWNLOADING_KINDS
            completed = counts.bytes_downloaded if counts is not None else self._own_bytes
            total = completed + measured
            if not final:
                # No dip in the instant between a fetch returning and its bytes
                # landing in the run's counts.
                total = max(total, self._reported_bytes)
            if total > self._reported_bytes:
                self._last_progress_at = self._now()
            self._reported_bytes = total
            self._samples.append((now_t, total))
            while self._samples and self._samples[0][0] < now_t - ETA_WINDOW_S:
                self._samples.popleft()
            speed = self._speed(now_t, SPEED_WINDOW_S)
            eta_speed = self._speed(now_t, ETA_WINDOW_S)
            values: dict[str, int | None] = {c: self._counts.get(c) for c in _COUNTERS}
            if not self._enumerated and self.kind in _DOWNLOADING_KINDS:
                values["items_to_fetch"] = None
            bytes_to_fetch = (
                self._bytes_to_fetch
                if downloading and self._enumerated and self._sizes_known else None
            )
            eta = None
            if downloading and self._enumerated and not final:
                handled = self._counts["items_done"] + self._counts["items_failed"]
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
                run_counts=counts,
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
        tick_s: float = TICK_S,
        start_timer: bool = True,
    ) -> None:
        self._now = now
        self._clock = clock
        self._tick_s = tick_s
        self._lock = threading.Lock()
        self._entries: list[Progress] = []
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
        progress = Progress(kind, run_id=run_id, now=self._now, clock=self._clock)
        if self._conn is None:
            return progress
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
                with transaction(self._conn):
                    for progress, snap in snaps:
                        assert progress.activity_id is not None
                        repo.write(progress.activity_id, snap, now, progress.run_id)
        except Exception as e:
            self._warn(e)

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
                    with transaction(self._conn):
                        ActivityRepo(self._conn).end(
                            progress.activity_id, result, self._now(), snap
                        )
                finally:
                    self._conn.execute(f"PRAGMA busy_timeout={_TICK_BUSY_TIMEOUT_MS}")
        except Exception as e:
            self._warn(e)

    def close(self) -> None:
        """Stop the timer and trim history older than 30 days. An entry still open
        here was never ended; it reads as stopped reporting once its heartbeat ages."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._conn is None:
            return
        try:
            with self._lock:
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
