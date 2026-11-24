"""When each collection syncs (§spec:schedules).

A collection's `schedule` is a standard five-field cron expression (minute, hour, day
of month, month, day of week) in the clock the top-level `timezone` names, or the
container's local time without one. This module is the one place that reads them:
the scheduler (`shakedown schedule`), `serve`'s overdue check, and
the dashboard's next-sync times all use it, so they agree on every tick.

Config validation (`shakedown.config`) checks each expression and the zone at load.
Cron arithmetic is cronsim's, including the day the clock changes: a time that does
not exist fires once at the next valid minute. A wall-clock time that occurs twice
fires once here — the first time — because cronsim fires every minute of a repeated
hour twice when the expression names more than one minute in it.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim

if TYPE_CHECKING:
    from shakedown.config import Config

log = logging.getLogger(__name__)

LOCALTIME = Path("/etc/localtime")


def schedule_zone(config: Config) -> ZoneInfo:
    """The clock schedules follow: the config's `timezone`, else the container's local
    time (`TZ`, then /etc/localtime), else UTC."""
    if config.timezone:
        return ZoneInfo(config.timezone)
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("TZ is not an IANA time zone name; schedules follow UTC")
            return ZoneInfo("UTC")
    try:
        with LOCALTIME.open("rb") as f:
            return ZoneInfo.from_file(f)
    except (OSError, ValueError):
        return ZoneInfo("UTC")


def _repeated(dt: datetime) -> bool:
    """Whether ``dt`` is the second occurrence of a wall-clock time the clock change
    repeats."""
    return dt.fold == 1 and dt.replace(fold=0).utcoffset() != dt.utcoffset()


def _ticks(expr: str, zone: ZoneInfo, start: datetime, *, reverse: bool) -> Iterator[datetime]:
    # cronsim steps in wall time, so from the repeated hour's second pass it can
    # return a time of its first pass, already gone: compared as instants, in UTC.
    begin = start.astimezone(UTC)
    for dt in CronSim(expr, start.astimezone(zone), reverse=reverse):
        instant = dt.astimezone(UTC)
        if _repeated(dt) or (instant > begin if reverse else instant <= begin):
            continue
        yield dt


def next_tick(expr: str, zone: ZoneInfo, after: datetime) -> datetime:
    """The first scheduled time strictly after ``after`` (an aware datetime), as an
    aware datetime in ``zone``."""
    return next(_ticks(expr, zone, after, reverse=False))


def previous_tick(expr: str, zone: ZoneInfo, at: datetime) -> datetime | None:
    """The latest scheduled time at or before ``at`` (an aware datetime), or None when
    the expression never fired before it."""
    # cronsim's reverse iteration starts strictly before its start time.
    return next(_ticks(expr, zone, at + timedelta(seconds=1), reverse=True), None)


def scheduled(config: Config) -> list[tuple[str, str, str]]:
    """Every (source, collection, schedule) with a schedule, in file order."""
    return [
        (source.name, collection.name, collection.schedule)
        for source in config.sources
        for collection in source.collections
        if collection.schedule
    ]


def next_syncs(config: Config, now: datetime) -> dict[tuple[str, str], datetime | None]:
    """Each configured collection's next scheduled sync after ``now``, or None for a
    manual-only collection (`next_syncs`, §spec:dashboard-settings)."""
    zone = schedule_zone(config)
    return {
        (source.name, collection.name): (
            next_tick(collection.schedule, zone, now) if collection.schedule else None
        )
        for source in config.sources
        for collection in source.collections
    }


# provisional: §spec:schedules staggered default; the operator may change the
# cadence (weekly on Sunday) or the start hour (03:00).
STAGGER_FIRST_HOUR = 3
SUNDAY = {0, 7}


def staggered_default(config: Config | None) -> str:
    """The schedule a newly added collection starts with: weekly on Sunday at the
    first whole hour from 03:00 at which no other collection's schedule starts, so
    collections do not all start at once. With no config yet (setup), 03:00. An hour is taken when another schedule can
    fire on a Sunday in it (a day-of-month limit is not read as ruling Sunday out).
    When every hour from 03:00 is taken, 03:00."""
    taken: set[int] = set()
    for _source, _collection, expr in scheduled(config) if config is not None else []:
        sim = CronSim(expr, datetime(2000, 1, 1))
        # cronsim keeps `SUN#1` and `0L` as (weekday, which) pairs.
        days = {d[0] if isinstance(d, tuple) else d for d in sim.weekdays}
        if days & SUNDAY:
            taken |= sim.hours
    for hour in range(STAGGER_FIRST_HOUR, 24):
        if hour not in taken:
            return f"0 {hour} * * 0"
    return f"0 {STAGGER_FIRST_HOUR} * * 0"
