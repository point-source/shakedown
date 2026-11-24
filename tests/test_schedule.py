"""Schedule arithmetic (§spec:schedules): next and previous ticks in the schedule's
clock, the day the clock changes, and the staggered default for a new collection."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from shakedown import schedule
from shakedown.config import Config

BERLIN = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")


def _config(*schedules: str | None, timezone: str | None = None) -> Config:
    return Config.model_validate({
        "archive_root": "/a",
        "library_root": "/l",
        "timezone": timezone,
        "sources": [{
            "name": "ia",
            "type": "ia",
            "collections": [
                {"name": f"c{i}", "query": "q", "schedule": s}
                for i, s in enumerate(schedules)
            ],
        }],
    })


def test_next_tick_is_strictly_after_and_in_the_schedules_zone() -> None:
    after = datetime(2026, 11, 24, 3, 30, tzinfo=BERLIN)
    assert schedule.next_tick("30 3 * * *", BERLIN, after) == datetime(
        2026, 11, 25, 3, 30, tzinfo=BERLIN
    )
    # 03:30 in Berlin is 02:30 UTC in winter, whichever clock `after` is given in.
    tick = schedule.next_tick("30 3 * * *", BERLIN, datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
    assert tick.astimezone(UTC) == datetime(2026, 11, 24, 2, 30, tzinfo=UTC)


def test_previous_tick_includes_the_moment_itself() -> None:
    at = datetime(2026, 11, 24, 3, 30, tzinfo=BERLIN)
    assert schedule.previous_tick("30 3 * * *", BERLIN, at) == at
    assert schedule.previous_tick("30 3 * * *", BERLIN, at.replace(minute=29)) == datetime(
        2026, 11, 23, 3, 30, tzinfo=BERLIN
    )


def test_a_time_the_clock_skips_fires_once_at_the_next_valid_minute() -> None:
    # 2026-03-29: Berlin's clocks jump from 02:00 to 03:00; 02:30 never happens.
    start = datetime(2026, 3, 28, 0, 0, tzinfo=BERLIN)
    first = schedule.next_tick("30 2 * * *", BERLIN, start)
    second = schedule.next_tick("30 2 * * *", BERLIN, first)
    third = schedule.next_tick("30 2 * * *", BERLIN, second)
    assert (first.day, first.hour, first.minute) == (28, 2, 30)
    assert (second.day, second.hour, second.minute) == (29, 3, 0)
    assert (third.day, third.hour, third.minute) == (30, 2, 30)


@pytest.mark.parametrize("expr, per_day", [("30 2 * * *", 1), ("*/30 2 * * *", 2)])
def test_a_time_the_clock_repeats_fires_once(expr: str, per_day: int) -> None:
    # 2026-10-25: Berlin's clocks go back from 03:00 to 02:00; 02:00-02:59 happens twice.
    tick = datetime(2026, 10, 24, 12, 0, tzinfo=BERLIN)
    fired = []
    while True:
        tick = schedule.next_tick(expr, BERLIN, tick)
        if tick.day != 25:
            break
        fired.append(tick)
    assert len(fired) == per_day
    assert all(t.utcoffset() == fired[0].utcoffset() for t in fired)


def test_schedule_zone_prefers_the_config_then_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert schedule.schedule_zone(_config(timezone="Europe/Berlin")) == BERLIN
    assert schedule.schedule_zone(_config()) == ZoneInfo("America/New_York")


def test_schedule_zone_falls_back_to_localtime_then_utc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(schedule, "LOCALTIME", tmp_path / "missing")
    assert schedule.schedule_zone(_config()) == UTC
    monkeypatch.setenv("TZ", "Not/AZone")
    assert schedule.schedule_zone(_config()) == UTC


def test_next_syncs_is_none_for_manual_collections() -> None:
    config = _config("0 4 * * 0", None, timezone="Europe/Berlin")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=BERLIN)  # a Tuesday
    assert schedule.next_syncs(config, now) == {
        ("ia", "c0"): datetime(2026, 11, 29, 4, 0, tzinfo=BERLIN),
        ("ia", "c1"): None,
    }


@pytest.mark.parametrize("existing, expected", [
    ((), "0 3 * * 0"),
    ((None,), "0 3 * * 0"),
    (("0 3 * * 0",), "0 4 * * 0"),
    (("0 3 * * 0", "30 4 * * *"), "0 5 * * 0"),  # daily at 04:30 fires on Sundays too
    (("0 3 * * 7",), "0 4 * * 0"),  # 7 is Sunday as well as 0
    (("0 3 * * 1",), "0 3 * * 0"),  # Mondays leave Sunday 03:00 free
    (("0 3 * * SUN#1",), "0 4 * * 0"),  # the first Sunday of the month is a Sunday
    (("0 3 * * 0L",), "0 4 * * 0"),  # and so is the last
    (("0 3-23 * * *",), "0 3 * * 0"),  # every hour from 03:00 taken: 03:00
])
def test_staggered_default(existing: tuple[str | None, ...], expected: str) -> None:
    assert schedule.staggered_default(_config(*existing)) == expected


def test_staggered_default_with_no_config_yet() -> None:
    """Setup offers it before any file exists (§spec:first-run-setup)."""
    assert schedule.staggered_default(None) == "0 3 * * 0"
