"""`GET /api/runs` (§spec:run-history): every run of the last N days, newest first,
with the figures `status` reports for runs that have a run record."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.activity import ActivityKind, ActivityRepo
from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.dashboard import VIEW_TTL_S
from shakedown.db import connect
from shakedown.server import build_app
from shakedown.state import RescheduledRepo, RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from shakedown.verify import run_verify
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.test_server import _Clock

FIELDS = {
    "run_id", "activity_id", "kind", "source", "collection", "started_at",
    "finished_at", "duration_seconds", "result", "end_reason", "items_discovered",
    "items_new", "items_updated", "items_failed", "bytes_downloaded", "errors",
    "failure_notice_error",
}
# The figures a run record holds, as `status --json`'s `last_run` reports them.
RECORD_FIGURES = (
    "run_id", "kind", "items_discovered", "items_new", "items_updated", "items_failed",
    "bytes_downloaded", "errors", "failure_notice_error", "end_reason",
)


def _app(config: Config, clock: _Clock):  # type: ignore[no-untyped-def]
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config), clock=clock)


async def _call(app, path: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(path)


async def _get(app, path: str = "/api/runs") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = await _call(app, path)
    assert r.status_code == 200, r.text
    return r.json()


def _same_instant(api_time: str, status_time: str) -> bool:
    """`/api/*` times carry their offset; `status` prints naive local times."""
    return datetime.fromisoformat(api_time) == datetime.fromisoformat(status_time).astimezone()


def _last_run(config: Config, capsys: pytest.CaptureFixture[str], collection: str) -> dict:
    capsys.readouterr()  # drop what the run printed
    print_status(config, as_json=True)
    summaries = json.loads(capsys.readouterr().out)
    return next(s for s in summaries if s["collection"] == collection)["last_run"]


@pytest.mark.asyncio
async def test_two_syncs_a_verify_and_a_failed_catch_up_list_newest_first(
    tmp_roots: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    archive, library = tmp_roots
    config = Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(name="fake-src", type="fake",
                         collections=[CollectionConfig(name="a", query="*")]),
            SourceConfig(name="other-src", type="fake",
                         collections=[CollectionConfig(name="b", query="*")]),
        ],
    )
    config_file = write_config(tmp_path / "shakedown.yaml", config)
    FakePlugin.items["gd-1"] = FakeItem(
        identifier="gd-1", files=[FakeFile(name="gd-1.flac", content=b"hello world")],
        metadata={"title": "Show"},
    )

    def sync() -> int:
        return run_sync(config, source_filter="fake-src", collection_filter="a",
                        config_path=config_file)

    assert sync() == 0
    first_sync = _last_run(config, capsys, "a")
    assert run_verify(config, source_filter="fake-src", collection_filter="a", deep=False,
                      reconform=False, list_drift=False, assume_yes=True) == 0
    # The second sync catches up other-src/b, whose sync was skipped; its source is
    # unreachable, so the catch-up fails.
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RescheduledRepo(conn).record("other-src", "b", datetime.now() - timedelta(hours=1))
    FakePlugin.unreachable_sources.add("other-src")
    FakePlugin.items["gd-2"] = FakeItem(
        identifier="gd-2", files=[FakeFile(name="gd-2.flac", content=b"more audio")],
        metadata={"title": "Another show"},
    )
    assert sync() == 1  # the failed catch-up fails the run
    second_sync = _last_run(config, capsys, "a")

    runs = (await _get(_app(config, _Clock())))["runs"]
    assert [(r["kind"], r["collection"]) for r in runs] == [
        ("catch_up", "b"), ("sync", "a"), ("verify", "a"), ("sync", "a"),
    ]
    catch_up, sync_2, verify, sync_1 = runs
    for row in runs:
        assert set(row) == FIELDS
        assert row["activity_id"] is not None
    for row, status_run in ((sync_1, first_sync), (sync_2, second_sync)):
        assert {k: row[k] for k in RECORD_FIGURES} == {
            k: status_run[k] for k in RECORD_FIGURES}
        assert _same_instant(row["started_at"], status_run["started_at"])
        assert _same_instant(row["finished_at"], status_run["finished_at"])
        assert row["result"] == "completed"
    assert sync_2["items_new"] == 1
    assert (verify["run_id"], verify["result"]) == (None, "completed")
    assert catch_up["result"] == "stale_enumeration"
    assert catch_up["run_id"] is not None and catch_up["errors"]


@pytest.mark.asyncio
async def test_a_killed_run_shows_it_stopped_reporting(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    now = datetime.now()
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        run = RunRepo(conn).start("fake-src", "coll1", now - timedelta(minutes=5))
        activity_id = ActivityRepo(conn).create(
            "fake-src", "coll1", ActivityKind.SYNC, run_id=run.id,
            started_by_serve=False, at=now - timedelta(minutes=5))
        conn.execute("UPDATE run_activity SET last_heartbeat_at=? WHERE id=?",
                     ((now - timedelta(minutes=2)).isoformat(), activity_id))
    [row] = (await _get(_app(config, _Clock())))["runs"]
    assert (row["run_id"], row["activity_id"]) == (run.id, activity_id)
    assert (row["result"], row["end_reason"]) == ("failed_before_completion",
                                                  "stopped_reporting")
    assert row["finished_at"] is not None and row["duration_seconds"] == 180


@pytest.mark.asyncio
async def test_a_pre_release_database_lists_its_run_records_and_is_not_migrated(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    started = datetime.now() - timedelta(hours=3)
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        runs = RunRepo(conn)
        run = runs.start("fake-src", "coll1", started)
        run.finished_at = started + timedelta(minutes=10)
        run.items_new = 2
        runs.finish(run)
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    [row] = (await _get(_app(config, _Clock())))["runs"]
    assert (row["run_id"], row["activity_id"], row["kind"]) == (run.id, None, "sync")
    assert (row["result"], row["duration_seconds"], row["items_new"]) == ("completed", 600, 2)
    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("days", ["0", "31", "-1", "week"])
async def test_days_outside_1_to_30_is_refused(
    tmp_roots: tuple[Path, Path], days: str,
) -> None:
    r = await _call(_app(make_config(*tmp_roots), _Clock()), f"/api/runs?days={days}")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_days_narrows_the_window_and_defaults_to_30(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    now = datetime.now()
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        runs = RunRepo(conn)
        for age in (timedelta(hours=1), timedelta(days=5), timedelta(days=40)):
            run = runs.start("fake-src", "coll1", now - age)
            run.finished_at = now - age + timedelta(minutes=1)
            runs.finish(run)
    app = _app(config, _Clock())
    assert len((await _get(app))["runs"]) == 2
    assert len((await _get(app, "/api/runs?days=30"))["runs"]) == 2
    assert len((await _get(app, "/api/runs?days=1"))["runs"]) == 1


@pytest.mark.asyncio
async def test_the_answer_is_shared_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    clock = _Clock()
    app = _app(config, clock)
    first = await _get(app)
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RunRepo(conn).start("fake-src", "coll1", datetime.now())
    assert await _get(app) == first
    assert len((await _get(app, "/api/runs?days=7"))["runs"]) == 1  # its own answer
    clock.now += VIEW_TTL_S
    later = await _get(app)
    assert len(later["runs"]) == 1 and later["generated_at"] >= first["generated_at"]


@pytest.mark.asyncio
async def test_answering_never_writes(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        run = RunRepo(conn).start("fake-src", "coll1", datetime.now() - timedelta(hours=1))
        ActivityRepo(conn).create("fake-src", "coll1", ActivityKind.SYNC, run_id=run.id,
                                  started_by_serve=False,
                                  at=datetime.now() - timedelta(hours=1))  # stale

    def dump() -> list[str]:
        with closing(sqlite3.connect(config.state_db)) as conn:  # type: ignore[arg-type]
            return list(conn.iterdump())

    before = dump()
    [row] = (await _get(_app(config, _Clock())))["runs"]
    assert row["end_reason"] == "stopped_reporting"
    assert dump() == before
