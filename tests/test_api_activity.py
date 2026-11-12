"""`GET /api/activity` (§spec:run-activity): running entries first, then entries that
ended in the last ten minutes; a run that stopped reporting reads as ended."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.activity import ABANDONED_ERROR, ActivityKind, ActivityRepo
from shakedown.dashboard import LIVE_TTL_S
from shakedown.db import connect
from shakedown.network import EventKind, NetworkEvent, NetworkRepo
from shakedown.server import TOKEN_ENV, build_app
from shakedown.state import RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.serve_child import SPEC_ENV
from tests.test_server import AUTH, CHILD_PREFIX, REPO_ROOT, _Clock, _finish

FIELDS = {
    "activity_id", "run_id", "source", "collection", "kind", "started_by_serve",
    "started_at", "finished_at", "last_heartbeat_at", "last_progress_at", "state",
    "result", "end_reason", "phase", "items_listed", "items_to_look_up",
    "items_looked_up", "items_to_fetch", "items_done", "items_failed", "current_items",
    "bytes_downloaded", "bytes_to_fetch", "speed_bytes_per_second", "eta_seconds",
}


def _app(config, clock: _Clock):  # type: ignore[no-untyped-def]
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config), clock=clock)


async def _get(app, path: str = "/api/activity") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get(path)
        assert r.status_code == 200, r.text
        return r.json()


def _seed_entry(conn: sqlite3.Connection, kind: ActivityKind, *, started: datetime,
                heartbeat: datetime, ended_at: datetime | None = None,
                result: str = "completed") -> int:
    repo = ActivityRepo(conn)
    activity_id = repo.create("fake-src", "coll1", kind, run_id=None,
                              started_by_serve=False, at=started)
    if ended_at is not None:
        repo.end(activity_id, result, ended_at)
    else:
        conn.execute("UPDATE run_activity SET last_heartbeat_at=? WHERE id=?",
                     (heartbeat.isoformat(), activity_id))
    return activity_id


@pytest.mark.asyncio
async def test_live_view_lists_running_then_recently_ended_newest_first(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:
        running = _seed_entry(conn, ActivityKind.SYNC, started=now - timedelta(minutes=3),
                              heartbeat=now - timedelta(seconds=2))
        newer_running = _seed_entry(conn, ActivityKind.VERIFY,
                                    started=now - timedelta(minutes=1),
                                    heartbeat=now - timedelta(seconds=1))
        stale = _seed_entry(conn, ActivityKind.RESTAGE, started=now - timedelta(minutes=8),
                            heartbeat=now - timedelta(seconds=30))
        ended = _seed_entry(conn, ActivityKind.RECONCILE, started=now - timedelta(minutes=9),
                            heartbeat=now, ended_at=now - timedelta(minutes=5),
                            result="completed_with_item_issues")
        _seed_entry(conn, ActivityKind.SYNC, started=now - timedelta(minutes=30),
                    heartbeat=now, ended_at=now - timedelta(minutes=15))
        _seed_entry(conn, ActivityKind.SYNC, started=now - timedelta(minutes=30),
                    heartbeat=now - timedelta(minutes=12))  # stopped reporting long ago

    body = await _get(_app(config, _Clock()))
    assert [n["source"] for n in body["network"]] == ["fake-src"]
    runs = body["runs"]
    assert [r["activity_id"] for r in runs] == [newer_running, running, stale, ended]
    for entry in runs:
        assert set(entry) == FIELDS
    assert runs[0]["state"] == "running" and runs[0]["result"] is None
    # A run that stopped reporting is shown as ended, never as running.
    gone = runs[2]
    assert (gone["state"], gone["result"], gone["end_reason"]) == (
        "ended", "failed_before_completion", "stopped_reporting")
    assert gone["finished_at"] == gone["last_heartbeat_at"]
    assert gone["eta_seconds"] is None and gone["current_items"] == []
    assert runs[3]["result"] == "completed_with_item_issues"
    # ISO times with their UTC offset (§spec:dashboard).
    assert datetime.fromisoformat(runs[0]["started_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_live_view_is_shared_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    clock = _Clock()
    app = _app(config, clock)
    first = await _get(app)
    assert (await _get(app))["generated_at"] == first["generated_at"]
    with closing(connect(config.state_db)) as conn:
        _seed_entry(conn, ActivityKind.SYNC, started=datetime.now(), heartbeat=datetime.now())
    assert (await _get(app))["runs"] == []  # still the shared answer
    clock.now += LIVE_TTL_S
    assert len((await _get(app))["runs"]) == 1


@pytest.mark.asyncio
async def test_live_view_on_a_pre_v10_database_is_empty_and_never_migrates(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    body = await _get(_app(config, _Clock()))
    assert body["runs"] == [] and body["network"] == []
    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


@pytest.mark.asyncio
async def test_live_view_never_writes(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:
        _seed_entry(conn, ActivityKind.SYNC, started=datetime.now(),
                    heartbeat=datetime.now() - timedelta(minutes=1))  # stale: derived only

    def dump() -> list[str]:
        with closing(sqlite3.connect(config.state_db)) as conn:
            return list(conn.iterdump())

    before = dump()
    [entry] = (await _get(_app(config, _Clock())))["runs"]
    assert entry["end_reason"] == "stopped_reporting"
    assert dump() == before


async def _poll(app, clock: _Clock, check, what: str):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 30
    while True:
        clock.now += LIVE_TTL_S
        runs = (await _get(app))["runs"]
        if (found := check(runs)) is not None:
            return found
        assert time.monotonic() < deadline, f"timed out waiting for {what}: {runs}"
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_run_serve_started_shows_live_and_then_its_result(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    config = make_config(*tmp_roots)
    root = config.archive_root.parent
    gate = root / "go"
    spec = root / "fake-spec.json"
    spec.write_text(json.dumps({"items": {"gd-x": {"x.flac": "audio"}}, "gate": str(gate)}))
    clock = _Clock()
    app = build_app(
        write_config(root / "cfg.yaml", config),
        argv_prefix=CHILD_PREFIX,
        child_env={SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)},
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post("/sync", headers=AUTH)
        assert r.status_code == 202
    try:
        live = await _poll(app, clock, lambda runs: next(
            (e for e in runs if e["current_items"] == ["gd-x"]), None), "the fetch")
        assert live["started_by_serve"] is True
        assert live["run_id"] == r.json()["run_ids"][0]
        assert (live["state"], live["kind"]) == ("running", "sync")
        assert live["items_to_fetch"] == 1
    finally:
        gate.touch()
    await _finish(app)
    done = await _poll(app, clock, lambda runs: next(
        (e for e in runs if e["state"] == "ended"), None), "the result")
    assert (done["result"], done["end_reason"], done["items_done"]) == ("completed", None, 1)


@pytest.mark.asyncio
async def test_a_killed_run_outside_serve_reads_ended_and_the_next_sync_closes_it(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """§spec:failure-behavior "Run killed or crashed outside serve"."""
    config = make_config(*tmp_roots)
    root = config.archive_root.parent
    cfg = write_config(root / "cfg.yaml", config)
    gate = root / "go"
    spec = root / "fake-spec.json"
    spec.write_text(json.dumps({"items": {"gd-x": {"x.flac": "audio"}}, "gate": str(gate)}))
    env = {k: v for k, v in os.environ.items() if k != "SHAKEDOWN_READY_FD"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.serve_child", "--config", str(cfg), "sync"],
        env={**env, SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)}, cwd=REPO_ROOT,
    )
    db = config.state_db
    clock = _Clock()
    app = _app(config, clock)
    try:
        live = await _poll(app, clock, lambda runs: next(
            (e for e in runs if e["current_items"] == ["gd-x"]), None), "the fetch")
        assert live["started_by_serve"] is False and live["state"] == "running"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)

    # Its heartbeat stops; once it is ~20 s old the run reads as ended. Age it rather
    # than wait.
    heartbeat = datetime.now() - timedelta(seconds=30)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE run_activity SET last_heartbeat_at=?", (heartbeat.isoformat(),))
        conn.commit()
    [gone] = await _poll(app, clock, lambda runs: runs if runs and runs[0]["state"] == "ended"
                         else None, "stopped reporting")
    assert (gone["result"], gone["end_reason"]) == ("failed_before_completion", "stopped_reporting")

    print_status(config, as_json=True)
    [summary] = json.loads(capsys.readouterr().out)
    assert summary["last_run"]["end_reason"] == "stopped_reporting"
    assert summary["last_run"]["run_id"] == gone["run_id"]

    assert run_sync(config) == 0  # takes the writer lock: closes the abandoned record
    with closing(connect(db)) as conn:
        row = conn.execute("SELECT finished_at, errors FROM runs WHERE id=?",
                           (gone["run_id"],)).fetchone()
        assert RunRepo(conn).latest("fake-src", "coll1") is not None
    assert row["finished_at"] == heartbeat.isoformat()
    assert json.loads(row["errors"]) == [ABANDONED_ERROR]


def _running(conn: sqlite3.Connection, source: str, *, started: datetime, speed: float | None,
             in_flight: int | None, heartbeat: datetime | None = None) -> int:
    repo = ActivityRepo(conn)
    activity_id = repo.create(source, "coll1", ActivityKind.SYNC, run_id=None,
                              started_by_serve=False, at=started)
    conn.execute(
        "UPDATE run_activity SET speed_bytes_per_second=?, requests_in_flight=?, "
        "last_heartbeat_at=? WHERE id=?",
        (speed, in_flight, (heartbeat or datetime.now()).isoformat(), activity_id),
    )
    return activity_id


@pytest.mark.asyncio
async def test_network_now_lists_each_source_with_a_run_in_progress(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Per source with a run in progress: its speed and requests in flight now, and
    the last network event of that run (§spec:network-activity)."""
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:
        _running(conn, "ia", started=now - timedelta(minutes=5), speed=1000.0, in_flight=2)
        _running(conn, "ia", started=now - timedelta(minutes=2), speed=500.0, in_flight=1)
        _running(conn, "etree", started=now - timedelta(minutes=1), speed=None, in_flight=None)
        _running(conn, "gone", started=now - timedelta(minutes=9), speed=9.0, in_flight=9,
                 heartbeat=now - timedelta(minutes=1))  # stopped reporting
        ended = _running(conn, "phish", started=now - timedelta(minutes=3), speed=1.0,
                         in_flight=1)
        ActivityRepo(conn).end(ended, "completed", now)
        repo = NetworkRepo(conn)
        repo.add_events("ia", [
            NetworkEvent(at=now - timedelta(minutes=4), kind=EventKind.RETRY,
                         collection="coll1", identifier="gd1"),
            NetworkEvent(at=now - timedelta(minutes=3), kind=EventKind.RATE_LIMITED,
                         http_status=429, collection="coll1", identifier="gd2",
                         wait_seconds=30.0, detail="HTTP 429"),
        ])
        # From an earlier run: not this run's last event.
        repo.add_events("etree", [NetworkEvent(at=now - timedelta(hours=2),
                                               kind=EventKind.TIMEOUT)])

    body = await _get(_app(config, _Clock()))

    network = {n["source"]: n for n in body["network"]}
    assert set(network) == {"ia", "etree"}
    assert network["etree"] == {"source": "etree", "speed_bytes_per_second": 0.0,
                                "requests_in_flight": 0, "last_event": None}
    ia = network["ia"]
    assert (ia["speed_bytes_per_second"], ia["requests_in_flight"]) == (1500.0, 3)
    last = ia["last_event"]
    assert {k: v for k, v in last.items() if k != "at"} == {
        "kind": "rate_limited", "http_status": 429, "collection": "coll1",
        "identifier": "gd2", "wait_seconds": 30.0, "detail": "HTTP 429",
    }
    assert datetime.fromisoformat(last["at"]).tzinfo is not None
