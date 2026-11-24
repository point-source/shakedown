"""`serve` watching the scheduler (§spec:schedules): when the scheduler turns stale,
send the configured failure notification once for that episode.

A process cannot report its own death, so `serve`, independently of the scheduler,
reads the heartbeat about once a minute. The episode is the heartbeat time the
scheduler went stale on: the notice record keeps it, so a second pass, or a restarted
`serve`, never sends twice, and a later stop (a newer heartbeat gone stale) is a new
episode. A scheduler that never recorded a heartbeat did not turn stale; Needs
attention shows it instead. The notice is sent once, not retried: an undelivered one
is recorded with its reason, like any failure notice (§spec:durable-handoff).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime

from shakedown import heartbeat, notify
from shakedown.config import Config
from shakedown.db import open_unmigrated
from shakedown.heartbeat import Notice

log = logging.getLogger(__name__)

WATCH_INTERVAL_S = 60.0


def notify_if_stale(config: Config, now: datetime) -> None:
    """Send the stale-scheduler notice when the scheduler has turned stale and this
    episode has not been notified. Never creates the database; never raises on a
    database it cannot read."""
    state_db = config.state_db
    if state_db is None or not state_db.exists():
        return
    with closing(open_unmigrated(state_db)) as conn:
        beat = heartbeat.read_heartbeat(conn)
        notice = heartbeat.read_notice(conn)
    if beat is None or heartbeat.state_of(beat, now) != "stale":
        return
    if notice is not None and notice.episode_last_seen == beat.at:
        return
    last_seen = beat.at.astimezone().isoformat()
    log.warning("the scheduler has not reported since %s; sending the failure notice",
                last_seen)
    error = notify.fire_failure(
        config.notifications,
        notify.SchedulerStalePayload(last_seen=last_seen, release=beat.release),
    )
    heartbeat.record_notice(state_db, Notice(beat.at, now, error))


def start_watching(config: Callable[[], Config | None], stop: threading.Event,
                   interval: float = WATCH_INTERVAL_S) -> threading.Thread:
    """Run ``notify_if_stale`` about every ``interval`` seconds on a daemon thread
    until ``stop`` is set, over the config ``config()`` returns (None: no valid
    config yet, skip)."""
    def watch() -> None:
        while not stop.wait(interval):
            try:
                current = config()
                if current is not None:
                    notify_if_stale(current, datetime.now(UTC))
            except Exception:
                log.exception("could not check the scheduler's heartbeat")

    thread = threading.Thread(target=watch, name="scheduler-watch", daemon=True)
    thread.start()
    return thread
