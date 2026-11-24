"""What `serve` reports about the scheduler (§spec:schedules): `scheduler` in
`GET /api/health` and `GET /status`, the `scheduler_stale` and `tick_overdue` entries
in Needs attention, and one failure notice per stale episode."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown import heartbeat
from shakedown.config import (
    Config,
    FailureNotification,
    NotificationsConfig,
)
from shakedown.db import connect
from shakedown.heartbeat import Heartbeat
from shakedown.scheduler_watch import notify_if_stale
from shakedown.server import build_app
from shakedown.state import RescheduledRepo
from tests.conftest import make_config, write_config
from tests.test_durable_handoff import GOOD, _Webhooks
from tests.test_server import _Clock, run_now


def _scheduled(config: Config, *schedules: str | None) -> Config:
    """``config`` with its collections scheduled in UTC (one collection per schedule)."""
    template = config.sources[0].collections[0]
    collections = [
        template.model_copy(update={"name": f"coll{i}", "schedule": s})
        for i, s in enumerate(schedules, start=1)
    ]
    source = config.sources[0].model_copy(update={"collections": collections})
    return config.model_copy(update={"sources": [source], "timezone": "UTC"})


def _daily_at(moment: datetime) -> str:
    """A daily schedule whose latest tick is ``moment`` (to the minute, UTC)."""
    moment = moment.astimezone(UTC)
    return f"{moment.minute} {moment.hour} * * *"


def _beat(config: Config, age: timedelta, *, valid: bool = True) -> datetime:
    assert config.state_db is not None
    at = datetime.now(UTC) - age
    heartbeat.record_heartbeat(
        config.state_db, Heartbeat(at, "9.9.9", valid, None), create=True
    )
    return at


def _run(config: Config, collection: str, started: datetime,
         finished: datetime | None, kind: str = "sync") -> None:
    """A run record, with times stored as runs store them: naive, local."""
    assert config.state_db is not None

    def local(dt: datetime | None) -> str | None:
        return dt.astimezone().replace(tzinfo=None).isoformat() if dt else None

    with closing(connect(config.state_db)) as conn:
        conn.execute(
            "INSERT INTO runs (source_name, collection_name, started_at, finished_at, kind)"
            " VALUES (?, ?, ?, ?, ?)",
            ("fake-src", collection, local(started), local(finished), kind),
        )


def _app(config: Config):  # type: ignore[no-untyped-def]
    path = write_config(config.archive_root.parent / "cfg.yaml", config)
    return build_app(path, clock=_Clock(), spawn=run_now)


async def _get(config: Config, path: str) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)), base_url="http://testserver"
    ) as client:
        r = await client.get(path)
        assert r.status_code == 200, r.text
        return r.json()


def _get_now(config: Config, path: str) -> Any:
    return asyncio.run(_get(config, path))


def _kinds(config: Config) -> list[tuple[str, str | None, str | None]]:
    entries = _get_now(config, "/api/attention")["entries"]
    return [(e["kind"], e["source"], e["collection"]) for e in entries]


# --- scheduler state in health and status ----------------------------------------------

def test_health_and_status_carry_the_scheduler_state(tmp_roots: tuple[Path, Path]) -> None:
    config = _scheduled(make_config(*tmp_roots), "0 3 * * *")
    connect(config.state_db).close()  # type: ignore[arg-type]
    never = {"state": "never", "last_seen": None, "release": None}
    assert _get_now(config, "/api/health")["scheduler"] == never
    status = _get_now(config, "/status")
    assert status["scheduler"] == never
    assert [c["collection"] for c in status["collections"]] == ["coll1"]

    at = _beat(config, timedelta(seconds=10))
    for answer in (_get_now(config, "/api/health"), _get_now(config, "/status")):
        assert answer["scheduler"]["state"] == "running"
        assert answer["scheduler"]["release"] == "9.9.9"
        assert datetime.fromisoformat(answer["scheduler"]["last_seen"]) == at


def test_status_reports_a_stale_and_a_scheduleless_scheduler(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _scheduled(make_config(*tmp_roots), "0 3 * * *")
    _beat(config, timedelta(seconds=10), valid=False)
    assert _get_now(config, "/status")["scheduler"]["state"] == "no_schedule"
    _beat(config, timedelta(minutes=4))
    assert _get_now(config, "/status")["scheduler"]["state"] == "stale"


# --- scheduler_stale -------------------------------------------------------------------

def test_scheduler_stale_is_one_entry_while_stale_or_never(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _scheduled(make_config(*tmp_roots), "0 3 * * *", "0 4 * * *")
    connect(config.state_db).close()  # type: ignore[arg-type]
    assert ("scheduler_stale", None, None) in _kinds(config)
    at = _beat(config, timedelta(minutes=5))
    [entry] = [e for e in _get_now(config, "/api/attention")["entries"]
               if e["kind"] == "scheduler_stale"]
    assert entry["source"] is None and entry["collection"] is None
    assert datetime.fromisoformat(entry["since"]) == at
    assert entry["identifiers"] == [] and entry["count"] == 0
    # Clears on its own once the scheduler is back.
    _beat(config, timedelta(seconds=5))
    assert ("scheduler_stale", None, None) not in _kinds(config)


def test_scheduler_stale_needs_a_scheduled_collection(tmp_roots: tuple[Path, Path]) -> None:
    config = _scheduled(make_config(*tmp_roots), None)
    _beat(config, timedelta(minutes=5))
    assert ("scheduler_stale", None, None) not in _kinds(config)


# --- tick_overdue ----------------------------------------------------------------------

def test_a_tick_missed_more_than_ten_minutes_ago_is_overdue(
    tmp_roots: tuple[Path, Path],
) -> None:
    now = datetime.now(UTC)
    config = _scheduled(
        make_config(*tmp_roots), _daily_at(now - timedelta(minutes=15)),
        _daily_at(now - timedelta(minutes=5)), None,
    )
    _beat(config, timedelta(seconds=5))
    entries = _get_now(config, "/api/attention")["entries"]
    overdue = [e for e in entries if e["kind"] == "tick_overdue"]
    assert [(e["source"], e["collection"]) for e in overdue] == [("fake-src", "coll1")]
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    assert datetime.fromisoformat(overdue[0]["since"]) == tick


def test_a_sync_started_at_or_after_the_tick_covers_it(tmp_roots: tuple[Path, Path]) -> None:
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick), _daily_at(tick))
    _run(config, "coll1", tick, None)
    _run(config, "coll2", tick + timedelta(minutes=2), now, kind="catch_up")
    assert not [k for k in _kinds(config) if k[0] == "tick_overdue"]


def test_a_sync_running_across_the_tick_covers_it(tmp_roots: tuple[Path, Path]) -> None:
    """A long sync of the collection itself spans its tick: no miss (Q-2-1)."""
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick), _daily_at(tick))
    _run(config, "coll1", tick - timedelta(days=2), now - timedelta(minutes=1))
    _run(config, "coll2", tick - timedelta(days=2), tick - timedelta(minutes=1))
    assert [k for k in _kinds(config) if k[0] == "tick_overdue"] == [
        ("tick_overdue", "fake-src", "coll2")
    ]


def _activity(config: Config, collection: str, heartbeat_age: timedelta) -> None:
    """The live-progress row of the collection's latest run record, last heard from
    ``heartbeat_age`` ago."""
    assert config.state_db is not None
    beat = (datetime.now() - heartbeat_age).isoformat()
    with closing(connect(config.state_db)) as conn:
        run_id = conn.execute(
            "SELECT MAX(id) FROM runs WHERE collection_name = ?", (collection,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO run_activity (source_name, collection_name, kind, run_id, "
            "started_at, last_heartbeat_at, last_progress_at, state) "
            "VALUES ('fake-src', ?, 'sync', ?, ?, ?, ?, 'running')",
            (collection, run_id, beat, beat, beat),
        )


def test_a_sync_still_reporting_across_the_tick_covers_it(
    tmp_roots: tuple[Path, Path],
) -> None:
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick))
    _run(config, "coll1", tick - timedelta(days=2), None)
    _activity(config, "coll1", timedelta(seconds=2))
    assert ("tick_overdue", "fake-src", "coll1") not in _kinds(config)


def test_a_dead_runs_open_record_does_not_cover_later_ticks(
    tmp_roots: tuple[Path, Path],
) -> None:
    """The NAS lost power mid-sync: its record stays open until the next run takes
    the writer lock, but a run that stopped reporting covers no tick."""
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick), _daily_at(tick))
    _run(config, "coll1", tick - timedelta(days=2), None)
    _activity(config, "coll1", timedelta(days=2))
    _run(config, "coll2", tick - timedelta(days=2), None)  # no live-progress row at all
    kinds = _kinds(config)
    assert ("tick_overdue", "fake-src", "coll1") in kinds
    assert ("tick_overdue", "fake-src", "coll2") in kinds


def test_an_outstanding_rescheduled_entry_covers_the_tick(
    tmp_roots: tuple[Path, Path],
) -> None:
    """The skip time is the oldest skip, so it can predate the tick (Q-2-1)."""
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick))
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        RescheduledRepo(conn).record("fake-src", "coll1", datetime.now() - timedelta(days=3))
    kinds = _kinds(config)
    assert ("rescheduled", "fake-src", "coll1") in kinds
    assert ("tick_overdue", "fake-src", "coll1") not in kinds


def test_a_refetch_does_not_cover_a_tick(tmp_roots: tuple[Path, Path]) -> None:
    now = datetime.now(UTC)
    tick = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    config = _scheduled(make_config(*tmp_roots), _daily_at(tick))
    _run(config, "coll1", tick + timedelta(minutes=1), now, kind="refetch")
    assert ("tick_overdue", "fake-src", "coll1") in _kinds(config)


# --- the stale-scheduler notice --------------------------------------------------------

def _notified(config: Config) -> Config:
    return config.model_copy(update={"notifications": NotificationsConfig(
        on_failure=FailureNotification(webhook=GOOD + "?c={collection}")
    )})


def _watch(config: Config, hooks: _Webhooks, now: datetime | None = None) -> None:
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        notify_if_stale(config, now or datetime.now(UTC))


def test_a_stale_scheduler_is_notified_once_per_episode(tmp_roots: tuple[Path, Path]) -> None:
    config = _notified(_scheduled(make_config(*tmp_roots), "0 3 * * *"))
    hooks = _Webhooks()
    first = _beat(config, timedelta(minutes=4))
    _watch(config, hooks)
    _watch(config, hooks)  # the next pass, or a restarted serve over the same database
    [(url, body)] = hooks.calls
    assert url == GOOD + "?c="
    assert body["event"] == "scheduler.stale"
    assert body["payload_version"] == 2
    assert body["source"] is None and body["collection"] is None
    assert body["staging_root"] is None and body["run"] is None
    assert body["scheduler"] == {"last_seen": first.isoformat(), "release": "9.9.9"}
    assert body["errors"] == [f"the scheduler has not reported since {first.isoformat()}"]
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        notice = heartbeat.read_notice(conn)
    assert notice is not None and notice.episode_last_seen == first and notice.error is None

    # Back, then stopped again: a new episode, a new notice.
    _beat(config, timedelta(seconds=5))
    _watch(config, hooks)
    assert len(hooks.calls) == 1
    _beat(config, timedelta(minutes=4))
    _watch(config, hooks)
    assert len(hooks.calls) == 2


def test_no_notice_while_running_or_never_started(tmp_roots: tuple[Path, Path]) -> None:
    config = _notified(_scheduled(make_config(*tmp_roots), "0 3 * * *"))
    hooks = _Webhooks()
    _watch(config, hooks)  # no database yet
    assert not config.state_db.exists()  # type: ignore[union-attr]
    connect(config.state_db).close()  # type: ignore[arg-type]
    _watch(config, hooks)  # never
    _beat(config, timedelta(seconds=30))
    _watch(config, hooks)  # running
    assert hooks.calls == []


def test_an_undelivered_notice_is_recorded_and_not_retried(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _notified(_scheduled(make_config(*tmp_roots), "0 3 * * *"))
    hooks = _Webhooks(down={GOOD + "?c="})
    _beat(config, timedelta(minutes=4))
    _watch(config, hooks)
    _watch(config, hooks)
    assert len(hooks.calls) == 1
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        notice = heartbeat.read_notice(conn)
    assert notice is not None and notice.error


def test_serve_watches_the_scheduler_while_it_runs(
    tmp_roots: tuple[Path, Path],
) -> None:
    """The watch runs on its own thread from serve's start to its stop, from the
    config serve last loaded, sending without anyone opening the dashboard."""
    import threading
    import time

    from shakedown import scheduler_watch

    config = _notified(_scheduled(make_config(*tmp_roots), "0 3 * * *"))
    _beat(config, timedelta(minutes=4))
    hooks = _Webhooks()
    stop = threading.Event()
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        thread = scheduler_watch.start_watching(lambda: config, stop, interval=0.01)
        deadline = time.monotonic() + 10
        while not hooks.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)
    assert len(hooks.calls) == 1 and not thread.is_alive()

    started: list[threading.Event] = []

    def record(config_of, stop_event, interval=60.0):  # type: ignore[no-untyped-def]
        assert config_of() is not None
        started.append(stop_event)

    app = _app(config)

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert started and not started[0].is_set()

    with patch("shakedown.server.start_watching", side_effect=record):
        asyncio.run(lifespan())
    assert started[0].is_set()
