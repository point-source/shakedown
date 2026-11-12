"""A sync records its live progress (§spec:run-activity): phases, counters, current
shows, bytes — and its run record moves at the same cadence, so `status` and the
dashboard agree mid-run."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import shakedown.activity as activity
from shakedown.db import connect
from shakedown.models import ManifestFile, RunKind
from shakedown.plugins.base import FetchResult
from shakedown.state import RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "TICK_S", 0.05)


def _seed(identifier: str, size: int) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier, files=[FakeFile(f"{identifier}.flac", b"x" * size, format="Flac")]
    )


def _wait_for(check: Callable[[], Any], what: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _entries(db: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM run_activity ORDER BY id").fetchall()


def _status(config, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    print_status(config, as_json=True)
    [summary] = json.loads(capsys.readouterr().out)
    return summary


def test_a_running_sync_reports_progress_and_status_agrees(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a", 2000)
    _seed("gd-b", 3000)
    entered, release = threading.Event(), threading.Event()
    real_fetch = FakePlugin.fetch

    def held_fetch(self, identifier: str, files: Sequence[ManifestFile],
                   dest_dir: Path) -> FetchResult:
        if identifier == "gd-b":
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "gd-b.flac").write_bytes(b"x" * 1000)  # a third is in
            entered.set()
            assert release.wait(20)
        return real_fetch(self, identifier, files, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", held_fetch)
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(run_sync(config)))
    runner.start()
    try:
        assert entered.wait(10)
        db = config.state_db

        def mid_run() -> sqlite3.Row | None:
            rows = _entries(db)
            row = rows[0] if rows else None
            return row if row and row["items_done"] == 1 and row["bytes_downloaded"] == 3000 \
                else None

        row = _wait_for(mid_run, "gd-a done and gd-b's first bytes")
        assert (row["kind"], row["state"], row["phase"]) == ("sync", "running", "downloading")
        assert (row["items_listed"], row["items_to_look_up"], row["items_looked_up"]) == (2, 2, 2)
        assert (row["items_to_fetch"], row["bytes_to_fetch"]) == (2, 5000)
        assert json.loads(row["current_items"]) == ["gd-b"]
        assert row["items_failed"] == 0
        assert row["run_id"] is not None and row["started_by_serve"] == 0

        # The run record moves at the same cadence: status shows the same figures.
        last = _status(config, capsys)["last_run"]
        assert last["finished_at"] is None and last["end_reason"] is None
        assert last["run_id"] == row["run_id"]
        assert last["items_new"] + last["items_updated"] == row["items_done"]
        assert last["items_failed"] == row["items_failed"]
        assert last["bytes_downloaded"] == row["bytes_downloaded"]
    finally:
        release.set()
        runner.join(20)
    assert result == [0]

    [done] = _entries(config.state_db)
    assert (done["state"], done["result"], done["end_reason"]) == ("ended", "completed", None)
    assert (done["items_done"], done["items_failed"], done["bytes_downloaded"]) == (2, 0, 5000)
    assert done["current_items"] == "[]" and done["eta_seconds"] is None
    with closing(connect(config.state_db)) as conn:
        run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.finished_at is not None
    assert (run.items_new, run.bytes_downloaded) == (2, 5000)


def test_a_stale_catch_up_marks_the_collection_stale(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A catch-up lists the source like any sync, so its failed enumeration leaves the
    collection stale (§spec:failure-behavior)."""
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:
        runs = RunRepo(conn)
        run = runs.start("fake-src", "coll1", datetime(2026, 11, 12, 3), kind=RunKind.CATCH_UP)
        run.stale = True
        run.finished_at = datetime(2026, 11, 12, 3, 5)
        runs.finish(run)
    summary = _status(config, capsys)
    assert summary["stale"] is True
    assert summary["last_run"]["kind"] == "catch_up"


def _mirror(tmp_path: Path, name: str) -> tuple[int, list[tuple], tuple]:
    archive, library = tmp_path / name / "archive", tmp_path / name / "library"
    archive.mkdir(parents=True)
    library.mkdir(parents=True)
    config = make_config(archive, library)
    code = run_sync(config)
    with closing(connect(config.state_db)) as conn:
        items = [
            (r["identifier"], r["status"], r["recorded_manifest"], r["restriction_reason"])
            for r in conn.execute("SELECT * FROM items ORDER BY identifier")
        ]
        r = conn.execute("SELECT * FROM runs").fetchone()
        run = (r["kind"], r["items_discovered"], r["items_new"], r["items_updated"],
               r["items_failed"], r["bytes_downloaded"], r["errors"], r["stale"],
               r["finished_at"] is not None)
    files = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*.flac"))
    return code, [*items, tuple(files)], run


def test_a_failing_recorder_leaves_the_run_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording failure never fails, slows, or changes a run (§spec:run-activity)."""
    _seed("gd-a", 100)
    _seed("gd-b", 200)
    FakePlugin.lookup_faults["gd-c"] = [ValueError("malformed")]
    _seed("gd-c", 300)

    with monkeypatch.context() as mp:
        mp.setattr(activity.ActivityRecorder, "_open", lambda self, db_path: None)
        unrecorded = _mirror(tmp_path, "unrecorded")

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    for name in ("create", "restart", "write", "end", "trim"):
        monkeypatch.setattr(activity.ActivityRepo, name, broken)
    recorded = _mirror(tmp_path, "failing")

    assert recorded == unrecorded
    assert unrecorded[0] == 1  # gd-c's lookup failed: the run is not clean either way
