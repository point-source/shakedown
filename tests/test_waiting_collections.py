"""Collections waiting for a slot in a multi-collection sync (§spec:sync-workflow,
§spec:run-activity "Waiting collections").

A sync with more collections than `max_concurrent_collections` runs the rest as
slots free. A collection still waiting has no run record and no operation outcome:
its run opens when it starts. Until then it has a waiting entry, which `status` and
`/api/activity` show as waiting — never as running, stuck, or in need of recovery.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import shakedown.activity as activity
from shakedown import stopping
from shakedown.activity import ActivityRepo
from shakedown.config import CollectionConfig, Config
from shakedown.db import connect, connect_reader
from shakedown.lock import run_exclusive
from shakedown.state import RescheduledRepo
from shakedown.status import collection_summaries, print_status
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS = ("coll1", "coll2", "coll3")


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(activity, "TICK_S", 0.05)
    stopping.reset()
    yield
    stopping.reset()


def _config(tmp_roots: tuple[Path, Path]) -> Config:
    """Three collections, one slot: coll1 runs, coll2 and coll3 wait."""
    config = make_config(*tmp_roots)
    config.max_concurrent_collections = 1
    config.sources[0].collections += [
        CollectionConfig(name=name, query="*") for name in COLLECTIONS[1:]
    ]
    return config


def _wait_for(check: Callable[[], Any], what: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _summaries(config: Config) -> dict[str, dict[str, Any]]:
    with closing(connect_reader(config.state_db)) as conn:  # type: ignore[arg-type]
        return {s["collection"]: s for s in collection_summaries(config, conn)}


def _run_rows(config: Config) -> list[sqlite3.Row]:
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def _activity_rows(config: Config) -> list[sqlite3.Row]:
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        return conn.execute("SELECT * FROM run_activity ORDER BY id").fetchall()


def _hold_first_fetch(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """The first download waits for ``release``; ``entered`` says it is waiting."""
    FakePlugin.items["gd-a"] = FakeItem(
        "gd-a", files=[FakeFile("gd-a.flac", b"x" * 100, format="Flac")]
    )
    entered, release = threading.Event(), threading.Event()
    real = FakePlugin.fetch

    def held(self, identifier, files, dest_dir):  # type: ignore[no-untyped-def]
        if not entered.is_set():
            entered.set()
            assert release.wait(20)
        return real(self, identifier, files, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", held)
    return entered, release


def test_collections_waiting_for_a_slot_show_as_waiting_not_running(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_roots)
    entered, release = _hold_first_fetch(monkeypatch)
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(run_sync(config)))
    runner.start()
    try:
        assert entered.wait(10)
        # A tick has passed, so every entry has a fresh heartbeat.
        time.sleep(0.2)
        summaries = _summaries(config)

        running = summaries["coll1"]
        # The run in progress still reports: no recovery warning while it works.
        assert running["recovery"] is None
        assert running["waiting"] is None
        assert running["last_run"]["finished_at"] is None
        for name in ("coll2", "coll3"):
            waiting = summaries[name]
            assert waiting["recovery"] is None
            assert waiting["last_run"] is None  # no run record until it starts
            assert waiting["waiting"]["kind"] == "sync"
            assert waiting["waiting"]["since"] is not None

        # Only coll1 has a run record and an in-progress outcome.
        assert [r["collection_name"] for r in _run_rows(config)] == ["coll1"]

        with closing(connect_reader(config.state_db)) as conn:  # type: ignore[arg-type]
            repo = ActivityRepo(conn)
            now = datetime.now()
            live = repo.live(now)
            queue = repo.waiting(now)
        assert [(e["collection"], e["state"]) for e in live] == [("coll1", "running")]
        # In the order they start.
        assert [e["collection"] for e in queue] == ["coll2", "coll3"]
        assert all(e["kind"] == "sync" for e in queue)

        print_status(config, as_json=False)
        out = capsys.readouterr().out
        assert "RECOVERY" not in out
        sections = dict(
            (block.splitlines()[0], block) for block in out.split("=== ")[1:]
        )
        assert "; starts when a slot frees in the current sync" in (
            sections["fake-src/coll2 ==="]
        )
        assert "waiting:" not in sections["fake-src/coll1 ==="]
    finally:
        released = datetime.now()
        release.set()
        runner.join(20)
    assert result == [0]

    rows = _run_rows(config)
    assert [r["collection_name"] for r in rows] == list(COLLECTIONS)
    # Each run is timed from when it actually began.
    for row in rows[1:]:
        assert datetime.fromisoformat(row["started_at"]) >= released
        assert row["finished_at"] is not None
    entries = _activity_rows(config)
    assert [(e["collection_name"], e["state"], e["result"]) for e in entries] == [
        (name, "ended", "completed") for name in COLLECTIONS
    ]
    assert [e["run_id"] for e in entries] == [r["id"] for r in rows]
    for entry in entries[1:]:
        assert datetime.fromisoformat(entry["started_at"]) >= released


def test_a_stop_mid_round_starts_no_waiting_collection(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from shakedown import sync

    config = _config(tmp_roots)
    FakePlugin.items["gd-a"] = FakeItem("gd-a", files=[FakeFile("gd-a.flac", b"x")])
    since = datetime(2026, 11, 16, 3, 0)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        for name in COLLECTIONS:
            RescheduledRepo(conn).record("fake-src", name, since)

    def stop_mid_fetch(self, identifier, files, dest_dir):  # type: ignore[no-untyped-def]
        stopping.request(signal.SIGTERM)
        stopping.check()
        raise AssertionError("unreachable")

    monkeypatch.setattr(FakePlugin, "fetch", stop_mid_fetch)
    scope = sync._scope(config, None, None)
    caplog.set_level(logging.INFO, logger="shakedown")

    code = run_exclusive(
        config,
        lambda: sync._run_sync(
            config, scope, False, False,
            catch_up={("fake-src", name): since for name in COLLECTIONS},
        ),
    )

    assert code == 143
    # Only the collection that started has a run, and it was stopped.
    assert [r["collection_name"] for r in _run_rows(config)] == ["coll1"]
    assert [(e["collection_name"], e["state"], e["result"]) for e in _activity_rows(config)] == [
        ("coll1", "ended", "stopped")
    ]
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        kept = {name: RescheduledRepo(conn).get("fake-src", name) for name in COLLECTIONS}
    assert kept == dict.fromkeys(COLLECTIONS, since)
    # The waiting ones never started: no "starting", no "discovering", no stop line
    # each — one line names them.
    summary = "stopped before 2 collections started: fake-src/coll2, fake-src/coll3"
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count(summary) == 1
    for name in ("coll2", "coll3"):
        assert [m for m in messages if f"/{name}" in m and m != summary] == []


def _spawn_cli(tmp_path: Path, config: Config, gate: Path) -> subprocess.Popen[bytes]:
    """A scheduled-style run (no serve) of the fake source, held at its first fetch."""
    write_config(tmp_path / "cfg.yaml", config)
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({"items": {"gd-x": {"x.flac": "a"}}, "gate": str(gate)}))
    env = {k: v for k, v in os.environ.items() if k != "SHAKEDOWN_READY_FD"}
    env[SPEC_ENV] = str(spec)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.serve_child", "--config", str(tmp_path / "cfg.yaml"),
         "sync"],
        env=env, cwd=REPO_ROOT,
    )


def test_a_crash_mid_round_reports_only_the_collection_that_ran(
    tmp_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_roots)
    gate = tmp_path / "go"
    proc = _spawn_cli(tmp_path, config, gate)
    try:
        def holding() -> bool:
            if not config.state_db.exists():  # type: ignore[union-attr]
                return False
            try:
                rows = _activity_rows(config)
            except sqlite3.OperationalError:
                return False
            return sorted((r["collection_name"], r["state"]) for r in rows) == [
                ("coll1", "running"), ("coll2", "waiting"), ("coll3", "waiting")
            ]

        _wait_for(holding, "coll1 running, coll2 and coll3 waiting", timeout=30)
        proc.send_signal(signal.SIGKILL)
        assert proc.wait(timeout=30) == -signal.SIGKILL
    finally:
        gate.touch()
        if proc.poll() is None:
            proc.kill()

    # Its heartbeat goes stale: the run that was working ended unexpectedly; the
    # waiting ones simply are not waiting any more.
    monkeypatch.setattr(activity, "STALE_AFTER_S", 0.2)
    time.sleep(0.4)
    summaries = _summaries(config)
    crashed = summaries["coll1"]
    assert crashed["recovery"] is not None
    assert crashed["recovery"]["status"] == "in_progress"
    assert crashed["last_run"]["end_reason"] == "stopped_reporting"
    for name in ("coll2", "coll3"):
        assert summaries[name]["recovery"] is None
        assert summaries[name]["waiting"] is None
        assert summaries[name]["last_run"] is None

    # The next run that takes the writer lock closes what the killed run left open:
    # coll1's record and outcome, and the waiting entries, which leave no trace.
    assert run_exclusive(config, lambda: 0) == 0
    [run] = _run_rows(config)
    assert run["collection_name"] == "coll1" and run["finished_at"] is not None
    assert activity.ABANDONED_ERROR in json.loads(run["errors"])
    assert [(e["collection_name"], e["state"]) for e in _activity_rows(config)] == [
        ("coll1", "ended")
    ]
    summaries = _summaries(config)
    assert summaries["coll1"]["recovery"]["status"] == "failed_before_completion"
    for name in ("coll2", "coll3"):
        assert summaries[name]["recovery"] is None
        assert summaries[name]["last_run"] is None
