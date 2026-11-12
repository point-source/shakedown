"""`GET /api/network` (§spec:network-activity): per configured source, the per-minute
series widened to buckets, and the newest events, over the last N days."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.dashboard import VIEW_TTL_S
from shakedown.dashboard_network import bucket_seconds
from shakedown.db import connect
from shakedown.network import EventKind, MinuteTotals, NetworkEvent, NetworkRepo
from shakedown.server import build_app
from tests.conftest import write_config
from tests.test_server import _Clock

SERIES_FIELDS = {
    "t", "bytes", "peak_requests_in_flight", "rate_limited", "server_errors",
    "timeouts", "connection_errors", "retries", "backoff_seconds",
}
EVENT_FIELDS = {"at", "kind", "http_status", "collection", "identifier", "wait_seconds",
                "detail"}


def _config(tmp_roots: tuple[Path, Path], *names: str) -> Config:
    archive, library = tmp_roots
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(name=name, type="fake",
                         collections=[CollectionConfig(name=f"c{i}", query="*")])
            for i, name in enumerate(names)
        ],
    )


def _app(config: Config, clock: _Clock | None = None):  # type: ignore[no-untyped-def]
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config),
                     clock=clock or _Clock())


async def _get(app, path: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(path)


async def _body(app, path: str = "/api/network") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = await _get(app, path)
    assert r.status_code == 200, r.text
    return r.json()


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


@pytest.mark.parametrize(("days", "bucket"), [(1, 60), (2, 300), (7, 900), (14, 1800),
                                             (30, 3600)])
def test_buckets_widen_with_the_window(days: int, bucket: int) -> None:
    """60 s for one day; otherwise the narrowest bucket keeping ≤ 720 points (Q-3-1)."""
    assert bucket_seconds(days) == bucket


@pytest.mark.asyncio
async def test_one_day_shows_each_minute_and_the_newest_events_first(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _config(tmp_roots, "ia")
    now = datetime.now()
    first, second = _minute(now - timedelta(minutes=10)), _minute(now - timedelta(minutes=9))
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        repo = NetworkRepo(conn)
        repo.add_minutes("ia", {
            first: MinuteTotals(bytes=1000, peak_requests_in_flight=2, rate_limited=1,
                                retries=1, backoff_seconds=30.0),
            second: MinuteTotals(bytes=500, peak_requests_in_flight=1),
            _minute(now - timedelta(days=2)): MinuteTotals(bytes=7),  # outside the window
        })
        repo.add_events("ia", [
            NetworkEvent(at=first, kind=EventKind.RATE_LIMITED, http_status=429,
                         collection="c", identifier="gd1", wait_seconds=30.0,
                         detail="HTTP 429"),
            NetworkEvent(at=first + timedelta(seconds=1), kind=EventKind.RETRY,
                         collection="c", identifier="gd1"),
            NetworkEvent(at=now - timedelta(days=2), kind=EventKind.TIMEOUT),
        ])

    body = await _body(_app(config), "/api/network?days=1")

    assert set(body) == {"generated_at", "bucket_seconds", "sources"}
    assert body["bucket_seconds"] == 60
    [ia] = body["sources"]
    assert set(ia) == {"source", "series", "events", "events_total"}
    assert ia["source"] == "ia"
    for point in ia["series"]:
        assert set(point) == SERIES_FIELDS
    assert [datetime.fromisoformat(p["t"]).replace(tzinfo=None) for p in ia["series"]] == [
        first, second,
    ]
    assert {k: v for k, v in ia["series"][0].items() if k != "t"} == {
        "bytes": 1000, "peak_requests_in_flight": 2, "rate_limited": 1, "server_errors": 0,
        "timeouts": 0, "connection_errors": 0, "retries": 1, "backoff_seconds": 30.0,
    }
    assert datetime.fromisoformat(ia["series"][0]["t"]).tzinfo is not None
    assert [e["kind"] for e in ia["events"]] == ["retry", "rate_limited"]
    assert set(ia["events"][1]) == EVENT_FIELDS
    assert ia["events"][1]["wait_seconds"] == 30.0 and ia["events"][0]["http_status"] is None
    assert ia["events_total"] == 2


@pytest.mark.asyncio
async def test_thirty_days_by_default_in_hours(tmp_roots: tuple[Path, Path]) -> None:
    config = _config(tmp_roots, "ia")
    hour = (datetime.now() - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        NetworkRepo(conn).add_minutes("ia", {
            hour + timedelta(minutes=5): MinuteTotals(bytes=100, peak_requests_in_flight=3,
                                                      backoff_seconds=1.5),
            hour + timedelta(minutes=50): MinuteTotals(bytes=200, peak_requests_in_flight=1,
                                                       server_errors=2, backoff_seconds=2.0),
            hour + timedelta(minutes=65): MinuteTotals(bytes=1),
        })

    body = await _body(_app(config))

    assert body["bucket_seconds"] == 3600
    [ia] = body["sources"]
    assert [(p["bytes"], p["peak_requests_in_flight"], p["server_errors"],
             p["backoff_seconds"]) for p in ia["series"]] == [(300, 3, 2, 3.5), (1, 0, 0, 0.0)]
    assert datetime.fromisoformat(ia["series"][0]["t"]).replace(tzinfo=None) == hour


@pytest.mark.asyncio
async def test_events_are_the_newest_500_with_a_total(tmp_roots: tuple[Path, Path]) -> None:
    config = _config(tmp_roots, "ia")
    start = datetime.now() - timedelta(hours=1)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        NetworkRepo(conn).add_events("ia", [
            NetworkEvent(at=start + timedelta(seconds=i), kind=EventKind.RETRY,
                         identifier=f"s{i}")
            for i in range(501)
        ])

    [ia] = (await _body(_app(config), "/api/network?days=1"))["sources"]

    assert len(ia["events"]) == 500 and ia["events_total"] == 501
    assert ia["events"][0]["identifier"] == "s500" and ia["events"][-1]["identifier"] == "s1"


@pytest.mark.asyncio
async def test_every_configured_source_appears_and_only_those(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A source with no activity has an empty series; history of a source no longer
    configured is not shown."""
    config = _config(tmp_roots, "ia", "etree", "ia")
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        NetworkRepo(conn).add_minutes("removed", {_minute(datetime.now()): MinuteTotals(bytes=1)})

    body = await _body(_app(config))

    assert body["sources"] == [
        {"source": "ia", "series": [], "events": [], "events_total": 0},
        {"source": "etree", "series": [], "events": [], "events_total": 0},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("days", ["0", "31", "-1", "week"])
async def test_a_window_outside_1_to_30_days_is_refused(
    tmp_roots: tuple[Path, Path], days: str
) -> None:
    r = await _get(_app(_config(tmp_roots, "ia")), f"/api/network?days={days}")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_answer_is_shared_per_window_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _config(tmp_roots, "ia")
    clock = _Clock()
    app = _app(config, clock)
    first = await _body(app, "/api/network?days=1")
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        NetworkRepo(conn).add_minutes("ia", {_minute(datetime.now()): MinuteTotals(bytes=5)})
    again = await _body(app, "/api/network?days=1")
    assert again == first
    assert (await _body(app, "/api/network?days=2"))["sources"][0]["series"] != []
    clock.now += VIEW_TTL_S
    assert (await _body(app, "/api/network?days=1"))["sources"][0]["series"] != []


@pytest.mark.asyncio
async def test_a_pre_v10_database_answers_empty_history_and_never_migrates(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _config(tmp_roots, "ia")
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        for table in ("run_activity", "network_minutes", "network_events"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE schema_version SET version = 9")

    body = await _body(_app(config))

    assert body["sources"] == [{"source": "ia", "series": [], "events": [], "events_total": 0}]
    with closing(sqlite3.connect(config.state_db)) as conn:  # type: ignore[arg-type]
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
