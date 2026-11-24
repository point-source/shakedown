"""Runs started by `serve` in a separate process (§spec:api-sync-runs).

The child is the ordinary one-shot CLI run. When serve hands it a readiness fd, it
writes one line there: `busy` when another run holds the writer lock, `failed` when
it cannot load its config, or `started` once it holds the lock — then one `opened`
line per run record, as each collection starts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from shakedown.config import CollectionConfig
from shakedown.lock import writer_lock
from shakedown.trigger import READY_FD_ENV
from tests.conftest import make_config, write_config
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent


def _spawn_reading(
    tmp_path: Path, args: list[str], *, items: dict | None = None, gate: Path | None = None
) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    """Start a fake-source child with a readiness fd; return it and the fd's reader."""
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({"items": items or {}, "gate": str(gate) if gate else None}))
    r, w = os.pipe()
    env = {**os.environ, READY_FD_ENV: str(w), SPEC_ENV: str(spec)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.serve_child", "--config", str(tmp_path / "cfg.yaml"),
         *args],
        pass_fds=(w,), env=env, cwd=REPO_ROOT,
    )
    os.close(w)
    return proc, os.fdopen(r, "rb")


def _read(reader: BinaryIO) -> dict | None:
    line = reader.readline()
    return json.loads(line) if line else None


def _spawn(
    tmp_path: Path, args: list[str], *, items: dict | None = None, gate: Path | None = None
) -> tuple[subprocess.Popen[bytes], dict | None]:
    """Start a fake-source child with a readiness fd; return it and its first line."""
    proc, reader = _spawn_reading(tmp_path, args, items=items, gate=gate)
    with reader:
        return proc, _read(reader)


def _run_row(db: Path, run_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()


def test_child_announces_busy_when_another_run_holds_the_lock(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    config = make_config(*tmp_roots)
    write_config(tmp_path / "cfg.yaml", config)
    with writer_lock(config):
        proc, line = _spawn(tmp_path, ["sync"], items={"gd-x": {"x.flac": "a"}})
        assert proc.wait(timeout=30) == 1  # the CLI still exits non-zero
    assert line == {"state": "busy"}


def test_child_announces_started_once_it_holds_the_lock_then_each_run_it_opens(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    config = make_config(*tmp_roots)
    write_config(tmp_path / "cfg.yaml", config)
    gate = tmp_path / "go"
    proc, reader = _spawn_reading(
        tmp_path, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate
    )
    with reader:
        try:
            assert _read(reader) == {"state": "started"}
            # The lock is held when serve is told the run started.
            from shakedown.lock import LockHeld

            try:
                with writer_lock(config):
                    raise AssertionError("the child must hold the writer lock")
            except LockHeld:
                pass
            opened = _read(reader)
            assert opened is not None and opened["state"] == "opened"
            assert (opened["source"], opened["collection"]) == ("fake-src", "coll1")
            assert config.state_db is not None
            row = _run_row(config.state_db, opened["run_id"])
            assert row is not None and row["finished_at"] is None  # open while it runs
        finally:
            gate.touch()
        assert _read(reader) is None  # nothing more; the fd closes as the child exits
    assert proc.wait(timeout=30) == 0
    assert _run_row(config.state_db, opened["run_id"])["finished_at"] is not None


def test_child_announces_failed_with_the_config_error(tmp_path: Path) -> None:
    (tmp_path / "cfg.yaml").write_text(
        "archive_root: /a\nlibrary_root: /b\nsources: []\nmax_concurrent_collections: 0\n"
    )
    proc, line = _spawn(tmp_path, ["sync"])
    assert proc.wait(timeout=30) == 2
    assert line is not None and line["state"] == "failed"
    assert "max_concurrent_collections" in line["error"]


def test_child_opens_each_collection_run_as_it_starts(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """With one collection at a time, a waiting collection's run record opens — and
    is announced — only when it starts, timed from then (§spec:sync-workflow)."""
    config = make_config(*tmp_roots)
    config.max_concurrent_collections = 1
    config.sources[0].collections += [
        CollectionConfig(name="coll2", query="*"),
        CollectionConfig(name="coll3", query="*"),
    ]
    write_config(tmp_path / "cfg.yaml", config)
    gate = tmp_path / "go"
    proc, reader = _spawn_reading(
        tmp_path, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate
    )
    with reader:
        try:
            assert _read(reader) == {"state": "started"}
            first = _read(reader)
            assert first is not None and first["collection"] == "coll1"
            assert config.state_db is not None
            conn = sqlite3.connect(config.state_db)
            try:
                # coll1 holds its slot at the gate: the others have no record yet.
                assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
            finally:
                conn.close()
        finally:
            released = datetime.now()
            gate.touch()
        rest = [_read(reader), _read(reader)]
        assert _read(reader) is None
    assert proc.wait(timeout=30) == 0
    assert [r["collection"] for r in rest if r] == ["coll2", "coll3"]
    for later in rest:
        assert later is not None
        started = datetime.fromisoformat(_run_row(config.state_db, later["run_id"])["started_at"])
        assert started >= released


# --- serve side: launch a run, wait for its line, watch it to the end -------------

def _launch(tmp_path: Path, config, args: list[str], *, items=None, gate=None, on_exit=None):
    from shakedown import trigger

    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({"items": items or {}, "gate": str(gate) if gate else None}))
    return trigger.launch(
        write_config(tmp_path / "cfg.yaml", config),
        args,
        state_db=config.state_db,
        on_exit=on_exit,
        argv_prefix=CHILD_PREFIX,
        env={SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)},
    )


CHILD_PREFIX = (sys.executable, "-m", "tests.serve_child")


def test_launch_reports_busy_and_reaps_the_child(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    from shakedown.trigger import Busy

    config = make_config(*tmp_roots)
    with writer_lock(config):
        result = _launch(tmp_path, config, ["sync"], items={"gd-x": {"x.flac": "a"}})
    assert isinstance(result, Busy)


def test_launch_reports_a_config_failure(tmp_roots: tuple[Path, Path], tmp_path: Path) -> None:
    from shakedown import trigger

    (tmp_path / "cfg.yaml").write_text("archive_root: /a\nlibrary_root: /b\nsources: nope\n")
    result = trigger.launch(
        tmp_path / "cfg.yaml", ["sync"], state_db=tmp_path / "state.db",
        argv_prefix=CHILD_PREFIX, env={"PYTHONPATH": str(REPO_ROOT)},
    )
    assert isinstance(result, trigger.Failed)
    assert "sources" in result.error


def test_watcher_reports_a_clean_exit_and_leaves_the_runs_as_the_child_closed_them(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    from shakedown.trigger import Started

    config = make_config(*tmp_roots)
    exits: list[int] = []
    gate = tmp_path / "go"
    result = _launch(
        tmp_path, config, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate,
        on_exit=exits.append,
    )
    assert isinstance(result, Started)
    gate.touch()
    assert result.run.done.wait(timeout=30)
    assert exits == [0]
    assert config.state_db is not None
    row = _run_row(config.state_db, result.run.runs[0]["run_id"])
    assert row["finished_at"] is not None
    assert "ended unexpectedly" not in row["errors"]


def test_watcher_closes_the_runs_of_a_killed_child(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """The run record always closes, even when the run is killed (§spec:failure-behavior)."""
    import signal

    from shakedown import trigger

    config = make_config(*tmp_roots)
    exits: list[int] = []
    gate = tmp_path / "go"
    result = _launch(
        tmp_path, config, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate,
        on_exit=exits.append,
    )
    assert isinstance(result, trigger.Started)
    # Wait for its collection to start: serve knows the run record from then on.
    deadline = time.monotonic() + 30
    while not result.run.runs:
        assert time.monotonic() < deadline, "the run record never opened"
        time.sleep(0.02)
    result.run.process.send_signal(signal.SIGKILL)
    assert result.run.done.wait(timeout=30)
    assert exits == [-signal.SIGKILL]
    [opened] = result.run.runs
    assert config.state_db is not None
    row = _run_row(config.state_db, opened["run_id"])
    assert row["finished_at"] is not None
    assert "run ended unexpectedly (killed by SIGKILL)" in json.loads(row["errors"])
    conn = sqlite3.connect(config.state_db)
    try:
        status = conn.execute(
            "SELECT status, finished_at FROM operation_outcomes WHERE id=?",
            (opened["outcome_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert status[0] == "failed_before_completion" and status[1] is not None

    # Closing is by exact id: a later run of the same collection keeps its open record.
    later = _launch(
        tmp_path, config, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate.with_name("g2")
    )
    assert isinstance(later, trigger.Started)
    try:
        deadline = time.monotonic() + 30
        while not later.run.runs:
            assert time.monotonic() < deadline, "the later run record never opened"
            time.sleep(0.02)
        trigger.close_abandoned(config.state_db, result.run.runs, -signal.SIGKILL)
        assert _run_row(config.state_db, later.run.runs[0]["run_id"])["finished_at"] is None
    finally:
        gate.with_name("g2").touch()
        assert later.run.done.wait(timeout=30)


# --- runs the scheduler starts (§spec:schedules) ------------------------------------

def test_a_run_the_scheduler_starts_is_rescheduled_not_refused_when_busy(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """A tick keeps scheduler semantics under the writer lock: it records its skip, and
    it is not counted as started by serve (§spec:rescheduled-syncs)."""
    from shakedown import db, trigger
    from shakedown.state import RescheduledRepo

    config = make_config(*tmp_roots)
    assert config.state_db is not None
    db.connect(config.state_db).close()  # an install where runs have written it
    path = write_config(tmp_path / "cfg.yaml", config)
    with writer_lock(config):
        result = trigger.launch(
            path, ["sync", "--source", "fake-src", "--collection", "coll1"],
            state_db=config.state_db, argv_prefix=CHILD_PREFIX,
            env={"PYTHONPATH": str(REPO_ROOT)}, started_by="scheduler",
        )
    assert isinstance(result, trigger.Busy)
    conn = db.connect_reader(config.state_db)
    try:
        assert RescheduledRepo(conn).get("fake-src", "coll1") is not None
    finally:
        conn.close()


def test_launch_stops_waiting_for_a_silent_child_and_keeps_watching_it(
    tmp_path: Path,
) -> None:
    """A child that says nothing (stuck on a hung mount) never holds up the caller:
    after the bound it is watched in the background like any started run."""
    from shakedown import trigger

    exits: list[int] = []
    result = trigger.launch(
        tmp_path / "cfg.yaml", [], state_db=tmp_path / "state.db",
        argv_prefix=(sys.executable, "-c", "import time; time.sleep(1)"),
        on_exit=exits.append, started_by="scheduler", first_line_timeout=0.2,
    )
    assert isinstance(result, trigger.Unconfirmed)
    assert not result.run.done.is_set()
    assert result.run.done.wait(timeout=30)
    assert exits == [0]


def test_the_first_line_wait_works_on_a_high_numbered_fd() -> None:
    """serve and the scheduler live for months with many files open: the wait must
    not depend on the pipe's fd number (select() refuses any fd from 1024 up)."""
    import resource

    from shakedown.trigger import _readable

    high = 1500
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] > high, "test needs fds > 1500"
    r, w = os.pipe()
    os.dup2(r, high)
    try:
        assert not _readable(high, 0.05)
        os.write(w, b"x\n")
        assert _readable(high, 0.05)
    finally:
        for fd in (r, w, high):
            os.close(fd)
