"""`shakedown schedule`: the long-running scheduler (§spec:schedules).

It does nothing but start scheduled syncs. Once a minute it:

1. checks the config file's modification time, and replans from a changed file. While
   the file is invalid it keeps the schedules of the last valid file it read; one that
   started on an invalid file has none until the file is fixed;
2. starts each collection whose tick is due, in file order, as the same one-shot
   `sync --collection` run a scheduler tick has always launched. It waits for each
   child to say whether it took the writer lock before starting the next, so the
   later ones of a minute find the lock held and are rescheduled
   (§spec:rescheduled-syncs). It watches each child and closes the run records one
   that died left open;
3. replaces its heartbeat (shakedown.heartbeat).

It listens on no port and never writes the config file. A fault in its own planning
ends the process with a non-zero exit, so the container's restart policy starts it
again; it never keeps running with its planning stopped.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from shakedown import __version__, db, heartbeat, trigger
from shakedown.config import Config, ConfigError, load
from shakedown.heartbeat import Heartbeat
from shakedown.schedule import next_tick, schedule_zone, scheduled

log = logging.getLogger(__name__)

# How long a tick waits for its child to say whether it took the writer lock before
# starting the next collection: a child stuck on a hung mount must not stop planning.
FIRST_LINE_TIMEOUT_S = 30.0


@dataclass
class Planned:
    expr: str
    next_at: datetime
    last_fired: datetime | None = None


def state_db_hint(config_path: Path) -> Path | None:
    """Where the state database is when the file does not validate: its `state_db`
    key, else the default beside `archive_root`. None when even that is unreadable."""
    try:
        raw = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    for key, suffix in (("state_db", ()), ("archive_root", (".shakedown", "state.db"))):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return Path(value, *suffix)
    return None


def _minute_start(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


class Scheduler:
    def __init__(
        self,
        config_path: Path,
        *,
        argv_prefix: tuple[str, ...] = trigger.DEFAULT_ARGV_PREFIX,
        child_env: dict[str, str] | None = None,
        first_line_timeout: float = FIRST_LINE_TIMEOUT_S,
    ) -> None:
        self.config_path = config_path
        self.argv_prefix = argv_prefix
        self.child_env = child_env
        self.first_line_timeout = first_line_timeout
        self.config: Config | None = None  # the last valid config read
        self.zone: ZoneInfo = ZoneInfo("UTC")
        self.plan: dict[tuple[str, str], Planned] = {}
        self.children: list[trigger.TriggeredRun] = []
        self._mtime: int | None = None
        self._checked = False
        self._stop = threading.Event()

    # --- one minute -----------------------------------------------------------------

    def step(self, now: datetime) -> None:
        """One check: replan from a changed file, start the due ticks, beat. ``now`` is
        an aware datetime."""
        self._reload_if_changed(now)
        self._fire_due(now)
        self._beat(now)

    def _reload_if_changed(self, now: datetime) -> None:
        try:
            mtime: int | None = self.config_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self._checked and mtime == self._mtime:
            return
        self._checked, self._mtime = True, mtime
        try:
            config = load(self.config_path)
        except ConfigError as e:
            if self.config is None:
                log.error("config is invalid; no schedules until it is fixed: %s", e)
            else:
                log.error("config is invalid; keeping the last valid schedules: %s", e)
            return
        self._replan(config, now)

    def _replan(self, config: Config, now: datetime) -> None:
        self.config = config
        self.zone = schedule_zone(config)
        # A tick due this minute still fires from the new file, but never twice.
        start = _minute_start(now) - timedelta(seconds=1)
        plan: dict[tuple[str, str], Planned] = {}
        for source, collection, expr in scheduled(config):
            last = self.plan.get((source, collection))
            last_fired = last.last_fired if last else None
            after = max(start, last_fired) if last_fired else start
            plan[(source, collection)] = Planned(
                expr, next_tick(expr, self.zone, after), last_fired
            )
        self.plan = plan
        upcoming = self._next_tick_at()
        log.info(
            "planned %d scheduled collection(s) in %s; next tick %s",
            len(plan), self.zone.key, upcoming.isoformat() if upcoming else "none",
        )

    def _fire_due(self, now: datetime) -> None:
        for (source, collection), planned in self.plan.items():  # file order
            if planned.next_at > now:
                continue
            planned.last_fired = planned.next_at
            # From now, not from the tick: after a clock jump a tick fires once.
            planned.next_at = next_tick(planned.expr, self.zone, now)
            self._launch(source, collection)

    def _launch(self, source: str, collection: str) -> None:
        assert self.config is not None and self.config.state_db is not None
        log.info("%s/%s: starting its scheduled sync", source, collection)
        result = trigger.launch(
            self.config_path,
            ["sync", "--source", source, "--collection", collection],
            state_db=self.config.state_db,
            on_exit=lambda code: log.info(
                "%s/%s: scheduled sync exited %d", source, collection, code
            ),
            argv_prefix=self.argv_prefix,
            env=self.child_env,
            started_by=trigger.SCHEDULER,
            first_line_timeout=self.first_line_timeout,
        )
        if isinstance(result, trigger.Busy):
            log.warning("%s/%s: another run holds the writer lock; the sync is "
                        "rescheduled", source, collection)
        elif isinstance(result, trigger.Failed):
            log.error("%s/%s: scheduled sync could not start: %s",
                      source, collection, result.error)
        elif isinstance(result, trigger.ExitedEarly):
            log.error("%s/%s: scheduled sync exited before starting (exit %d)",
                      source, collection, result.returncode)
        else:
            if isinstance(result, trigger.Unconfirmed):
                log.warning("%s/%s: scheduled sync has not said whether it started "
                            "after %.0f s; watching it", source, collection,
                            self.first_line_timeout)
            self.children = [c for c in self.children if not c.done.is_set()]
            self.children.append(result.run)

    def _next_tick_at(self) -> datetime | None:
        return min((p.next_at for p in self.plan.values()), default=None)

    def _beat(self, now: datetime) -> None:
        state_db = self.config.state_db if self.config else state_db_hint(self.config_path)
        if state_db is None:
            log.warning("scheduler heartbeat not recorded: the config names no state "
                        "database")
            return
        heartbeat.record_heartbeat(
            state_db,
            Heartbeat(at=now, release=__version__, schedule_valid=self.config is not None,
                      next_tick_at=self._next_tick_at()),
            create=True,
        )

    # --- the process ------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        """Stop the runs this scheduler started (§spec:graceful-stop)."""
        trigger.stop_runs(self.children)

    def run_forever(self) -> int:
        """Check once a minute, just after each minute starts, until asked to stop.
        Returns the exit code: 0 when stopped on request, 1 on a planning fault."""
        log.info("scheduler %s started; config %s", __version__, self.config_path)
        code = 0
        try:
            while not self._stop.is_set():
                self.step(datetime.now(UTC))
                self._stop.wait(60.0 - time.time() % 60.0 + 0.2)
        except Exception:
            log.exception("scheduler planning failed; exiting so it restarts")
            code = 1
        finally:
            self.stop()
        return code


def check(config_path: Path, now: datetime) -> tuple[bool, str]:
    """`shakedown schedule --check`: whether a heartbeat younger than three minutes is
    recorded, and why not. Never creates or migrates the database."""
    try:
        state_db = load(config_path).state_db
    except ConfigError:
        state_db = state_db_hint(config_path)
    if state_db is None:
        return False, f"cannot find the state database from {config_path}"
    if not state_db.exists():
        return False, "no scheduler heartbeat recorded: the state database does not exist"
    try:
        with closing(db.open_unmigrated(state_db)) as conn:
            beat = heartbeat.read_heartbeat(conn)
    except sqlite3.Error as e:
        return False, f"cannot read the state database: {e}"
    if beat is None:
        return False, "no scheduler heartbeat recorded"
    if heartbeat.state_of(beat, now) == "stale":
        return False, f"last scheduler heartbeat at {beat.at.isoformat()}, over 3 minutes ago"
    return True, f"scheduler heartbeat at {beat.at.isoformat()}"
