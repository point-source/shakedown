"""The `trim` run (§spec:run-error-retention "Trim now"): under the writer lock it
applies the retention rule at once, then compacts the state database so the space
returns to the disk. It is `shakedown trim-history` on the CLI and
`POST /api/history/trim` in serve, and shows as kind `trim` in the live view and the
run history."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from shakedown import retention
from shakedown.activity import ActivityRepo
from shakedown.cli import main
from shakedown.config import Config
from shakedown.db import connect
from shakedown.history import run_history
from shakedown.lock import writer_lock
from shakedown.models import OperationType
from shakedown.state import OperationOutcomeRepo, RunRepo
from shakedown.write_gate import TOKEN_ENV
from tests.conftest import make_config, write_config

S, C = "fake-src", "coll1"
NOISE = [f"gd-{i}: download failed: " + "x" * 120 for i in range(60)]


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]) -> Config:
    config = make_config(*tmp_roots)
    config.keep_run_errors_days = 30
    return config


def _seed(config: Config, runs: int = 300) -> list[int]:
    """Old runs with bulky per-show lines, and one recent run that keeps them."""
    assert config.state_db is not None
    ids = []
    with closing(connect(config.state_db)) as conn:
        repo = RunRepo(conn)
        for age in [40] * runs + [5]:
            started = datetime.now() - timedelta(days=age)
            run = repo.start(S, C, started)
            run.finished_at = started + timedelta(minutes=5)
            run.items_failed = len(NOISE)
            run.errors = NOISE
            repo.finish(run)
            assert run.id is not None
            ids.append(run.id)
    return ids


def _db_bytes(db: Path) -> int:
    wal = db.with_name(db.name + "-wal")
    return db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def _trim_entry(config: Config) -> dict[str, Any]:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        [entry] = [e for e in ActivityRepo(conn).since(datetime.min, datetime.now())
                   if e["kind"] == "trim"]
    return entry


def test_trim_history_trims_old_runs_and_shrinks_the_database(config: Config) -> None:
    ids = _seed(config)
    assert config.state_db is not None
    before = _db_bytes(config.state_db)

    assert retention.run_trim_history(config) == 0

    assert _db_bytes(config.state_db) < before
    entry = _trim_entry(config)
    assert (entry["state"], entry["result"]) == ("ended", "completed")
    assert entry["runs_trimmed"] == len(ids) - 1
    assert 0 < entry["bytes_freed"] <= before
    with closing(connect(config.state_db)) as conn:
        runs = {r.id: r for r in RunRepo(conn).since(datetime.min)}
        outcome = OperationOutcomeRepo(conn).get(entry["outcome_id"])
    assert runs[ids[0]].errors == [] and runs[ids[0]].items_failed == len(NOISE)
    assert runs[ids[-1]].errors == NOISE
    assert outcome is not None and outcome.operation == OperationType.TRIM
    assert outcome.errors == []


def test_a_failed_compaction_still_trims_and_reports_the_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed(config, runs=3)

    def no_room(db: Path) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(retention, "_vacuum", no_room)
    # No space came back, so the run fails (operator, 2026-11-24).
    assert retention.run_trim_history(config) == 1

    entry = _trim_entry(config)
    assert entry["result"] == "failed_before_completion"
    assert (entry["runs_trimmed"], entry["bytes_freed"]) == (3, 0)
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        assert {r.id: r for r in RunRepo(conn).since(datetime.min)}[ids[0]].errors == []
        [row] = [r for r in run_history(conn, datetime.now(), 30) if r["kind"] == "trim"]
    assert row["errors"] == ["compaction failed: database or disk is full"]


def test_trim_history_is_refused_while_another_run_holds_the_lock(config: Config) -> None:
    _seed(config, runs=1)
    with writer_lock(config):
        assert retention.run_trim_history(config) == 1
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        assert [e for e in ActivityRepo(conn).since(datetime.min, datetime.now())
                if e["kind"] == "trim"] == []


def test_the_cli_runs_trim_history(config: Config, tmp_path: Path) -> None:
    _seed(config, runs=2)
    cfg = write_config(tmp_path / "shakedown.yaml", config)
    result = CliRunner().invoke(main, ["--config", str(cfg), "trim-history"])
    assert result.exit_code == 0, result.output
    assert _trim_entry(config)["runs_trimmed"] == 2


# --- serve --------------------------------------------------------------------------

pytest.importorskip("fastapi")

from tests.test_server import (  # noqa: E402
    AUTH,
    BUSY,
    CROSS_SITE,
    DASHBOARD,
    _app,
    _child_app,
    _finish,
)


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.mark.asyncio
async def test_post_trim_starts_a_trim_run_shown_in_activity_and_runs(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    _seed(config, runs=50)
    assert config.state_db is not None
    before = _db_bytes(config.state_db)
    app = _child_app(config)
    async with _client(app) as client:
        r = await client.post("/api/history/trim", headers=AUTH)
        assert (r.status_code, r.json()) == (202, {"started": "trim"})
        await _finish(app)
        activity = (await client.get("/api/activity")).json()["runs"]
        runs = (await client.get("/api/runs")).json()["runs"]
    assert _db_bytes(config.state_db) < before
    [live] = [e for e in activity if e["kind"] == "trim"]
    [row] = [r for r in runs if r["kind"] == "trim"]
    for answer in (live, row):
        assert (answer["source"], answer["collection"]) == (None, None)
        assert answer["runs_trimmed"] == 50 and answer["bytes_freed"] > 0
    assert row["result"] == "completed" and row["errors"] == []
    others = [r for r in runs if r["kind"] != "trim"]
    assert others and all(
        (r["runs_trimmed"], r["bytes_freed"]) == (None, None) for r in others
    )


@pytest.mark.asyncio
async def test_post_trim_while_a_run_holds_the_lock_answers_409(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    assert config.state_db is not None
    connect(config.state_db).close()
    app = _child_app(config)
    async with _client(app) as client:
        with writer_lock(config):
            r = await client.post("/api/history/trim", headers=DASHBOARD)
    assert (r.status_code, r.json()["detail"]) == (409, BUSY)


@pytest.mark.asyncio
async def test_post_trim_passes_the_write_gate_before_reading_the_config(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    app = _app(config)
    cfg_path = config.archive_root.parent / "cfg.yaml"
    async with _client(app) as client:
        r = await client.post("/api/history/trim")
        assert (r.status_code, r.json()) == (403, CROSS_SITE)
        cfg_path.write_text(cfg_path.read_text() + "max_concurrent_collections: 0\n")
        r = await client.post("/api/history/trim")
        assert (r.status_code, r.json()) == (403, CROSS_SITE)
        r = await client.post("/api/history/trim", headers=DASHBOARD)
        assert r.status_code == 503
