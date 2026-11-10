"""Runs started by `serve` in a separate process (§spec:api-sync-runs).

The child is the ordinary one-shot CLI run. When serve hands it a readiness fd, it
writes one line there: `busy` when another run holds the writer lock, `failed` when
it cannot load its config, or `started` (with its run records) once it holds the
lock and has opened every run record it will use.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from shakedown.config import CollectionConfig
from shakedown.lock import writer_lock
from shakedown.trigger import READY_FD_ENV
from tests.conftest import make_config, write_config
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent


def _spawn(
    tmp_path: Path, args: list[str], *, items: dict | None = None, gate: Path | None = None
) -> tuple[subprocess.Popen[bytes], dict | None]:
    """Start a fake-source child with a readiness fd; return it and its one line."""
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
    with os.fdopen(r, "rb") as reader:
        line = reader.readline()
    return proc, (json.loads(line) if line else None)


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


def test_child_announces_started_once_it_holds_the_lock_and_opened_its_runs(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    config = make_config(*tmp_roots)
    write_config(tmp_path / "cfg.yaml", config)
    gate = tmp_path / "go"
    proc, line = _spawn(tmp_path, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate)
    try:
        assert line is not None and line["state"] == "started"
        [run] = line["runs"]
        assert (run["source"], run["collection"]) == ("fake-src", "coll1")
        assert config.state_db is not None
        row = _run_row(config.state_db, run["run_id"])
        assert row is not None and row["finished_at"] is None  # open while it runs
        # The lock is held when serve is told the run started.
        from shakedown.lock import LockHeld

        try:
            with writer_lock(config):
                raise AssertionError("the child must hold the writer lock")
        except LockHeld:
            pass
    finally:
        gate.touch()
    assert proc.wait(timeout=30) == 0
    assert _run_row(config.state_db, run["run_id"])["finished_at"] is not None


def test_child_announces_failed_with_the_config_error(tmp_path: Path) -> None:
    (tmp_path / "cfg.yaml").write_text(
        "archive_root: /a\nlibrary_root: /b\nsources: []\nmax_concurrent_collections: 0\n"
    )
    proc, line = _spawn(tmp_path, ["sync"])
    assert proc.wait(timeout=30) == 2
    assert line is not None and line["state"] == "failed"
    assert "max_concurrent_collections" in line["error"]


def test_child_opens_every_collection_run_up_front_and_times_each_from_its_start(
    tmp_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """With one collection at a time, three collections' runs are all announced at
    start, and a queued collection's run is timed from when it began, not from when
    the record was opened."""
    config = make_config(*tmp_roots)
    config.max_concurrent_collections = 1
    config.sources[0].collections += [
        CollectionConfig(name="coll2", query="*"),
        CollectionConfig(name="coll3", query="*"),
    ]
    write_config(tmp_path / "cfg.yaml", config)
    gate = tmp_path / "go"
    proc, line = _spawn(tmp_path, ["sync"], items={"gd-x": {"x.flac": "a"}}, gate=gate)
    try:
        assert line is not None and line["state"] == "started"
        assert sorted(r["collection"] for r in line["runs"]) == ["coll1", "coll2", "coll3"]
    finally:
        # The first collection may begin before or after this moment, but it holds
        # its slot at the gate, so the two queued ones can only begin after it.
        released = datetime.now()
        gate.touch()
    assert proc.wait(timeout=30) == 0
    assert config.state_db is not None
    starts = sorted(
        datetime.fromisoformat(_run_row(config.state_db, r["run_id"])["started_at"])
        for r in line["runs"]
    )
    assert released <= starts[1] <= starts[2]


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
    result.run.process.send_signal(signal.SIGKILL)
    assert result.run.done.wait(timeout=30)
    assert exits == [-signal.SIGKILL]
    [opened] = result.run.runs
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
        trigger.close_abandoned(config.state_db, result.run.runs, -signal.SIGKILL)
        assert _run_row(config.state_db, later.run.runs[0]["run_id"])["finished_at"] is None
    finally:
        gate.with_name("g2").touch()
        assert later.run.done.wait(timeout=30)
