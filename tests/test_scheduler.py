"""`shakedown schedule`, the long-running scheduler (§spec:schedules): it plans each
collection's ticks from the config file, starts each tick as the one-shot
`sync --collection` run, replans from a changed file, and records a heartbeat."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from shakedown import db, heartbeat
from shakedown.cli import main
from shakedown.heartbeat import Heartbeat
from shakedown.scheduler import Scheduler
from shakedown.state import RescheduledRepo
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
CHILD_PREFIX = (sys.executable, "-m", "tests.serve_child")
BERLIN = ZoneInfo("Europe/Berlin")
# A Tuesday, 02:59 in Berlin.
T0 = datetime(2026, 11, 24, 2, 59, 0, tzinfo=BERLIN)


def _write(path: Path, root: Path, *schedules: str | None) -> Path:
    path.write_text(yaml.safe_dump({
        "archive_root": str(root / "archive"),
        "library_root": str(root / "library"),
        "timezone": "Europe/Berlin",
        "sources": [{
            "name": "fake-src",
            "type": "fake",
            "collections": [
                {"name": f"coll{i}", "query": "*",
                 **({"schedule": s} if s is not None else {})}
                for i, s in enumerate(schedules, start=1)
            ],
        }],
    }))
    return path


def _bump(path: Path) -> None:
    """Move the file's modification time on, as a save a moment later would."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "archive").mkdir()
    (tmp_path / "library").mkdir()
    return tmp_path


@pytest.fixture
def recorder(tmp_path: Path) -> tuple[tuple[str, ...], Path]:
    """A child that records its arguments and exits without a word."""
    log = tmp_path / "launched.jsonl"
    code = (
        "import json, sys; "
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')"
    )
    return (sys.executable, "-c", code), log


def _launched(log: Path) -> list[list[str]]:
    deadline = time.monotonic() + 10
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _state_db(root: Path) -> Path:
    return root / "archive" / ".shakedown" / "state.db"


def _beat(root: Path) -> Heartbeat | None:
    with closing(db.connect_reader(_state_db(root))) as conn:
        return heartbeat.read_heartbeat(conn)


def test_a_due_tick_starts_the_collections_one_shot_sync(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    prefix, log = recorder
    config = _write(root / "cfg.yaml", root, "0 3 * * *", None)
    scheduler = Scheduler(config, argv_prefix=prefix)
    scheduler.step(T0)
    assert not log.exists(), "nothing is due at 02:59"
    scheduler.step(T0 + timedelta(minutes=1))
    scheduler.step(T0 + timedelta(minutes=2))
    assert _launched(log) == [[
        "--config", str(config), "sync", "--source", "fake-src", "--collection", "coll1",
    ]]


def test_the_heartbeat_holds_the_time_release_validity_and_next_tick(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    from shakedown import __version__

    config = _write(root / "cfg.yaml", root, "0 3 * * *", "30 4 * * *")
    Scheduler(config, argv_prefix=recorder[0]).step(T0)
    assert _beat(root) == Heartbeat(
        at=T0, release=__version__, schedule_valid=True,
        next_tick_at=T0.replace(hour=3, minute=0),
    )


def test_a_changed_file_is_replanned_without_a_restart(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    prefix, log = recorder
    config = _write(root / "cfg.yaml", root, "0 5 * * *")
    scheduler = Scheduler(config, argv_prefix=prefix)
    scheduler.step(T0)
    _write(config, root, "0 3 * * *")
    _bump(config)
    scheduler.step(T0 + timedelta(minutes=1))
    assert [args[-1] for args in _launched(log)] == ["coll1"]


def test_an_invalid_file_keeps_the_last_valid_schedules(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    prefix, log = recorder
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    scheduler = Scheduler(config, argv_prefix=prefix)
    scheduler.step(T0)
    config.write_text(config.read_text().replace("0 3 * * *", "0 3 * *"))
    _bump(config)
    scheduler.step(T0 + timedelta(minutes=1))
    # The tick still starts its run, which then fails on the broken file.
    assert [args[-1] for args in _launched(log)] == ["coll1"]
    beat = _beat(root)
    assert beat is not None and beat.schedule_valid


def test_a_scheduler_that_starts_on_an_invalid_file_has_no_schedules(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    prefix, log = recorder
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    config.write_text(config.read_text() + "unknown_key: 1\n")
    scheduler = Scheduler(config, argv_prefix=prefix)
    scheduler.step(T0 + timedelta(minutes=1))
    assert not log.exists()
    beat = _beat(root)
    assert beat is not None
    assert not beat.schedule_valid and beat.next_tick_at is None
    # Fixed: planned from the next check on.
    config.write_text(config.read_text().replace("unknown_key: 1\n", ""))
    _bump(config)
    scheduler.step(T0.replace(day=25, hour=3, minute=0))
    assert [args[-1] for args in _launched(log)] == ["coll1"]


def test_the_scheduler_never_writes_the_config_file(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    before = (config.read_bytes(), config.stat().st_mtime_ns)
    scheduler = Scheduler(config, argv_prefix=recorder[0])
    for minute in range(3):
        scheduler.step(T0 + timedelta(minutes=minute))
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before


def test_a_tick_fires_once_however_often_its_minute_is_checked(
    root: Path, recorder: tuple[tuple[str, ...], Path]
) -> None:
    prefix, log = recorder
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    scheduler = Scheduler(config, argv_prefix=prefix)
    at = T0 + timedelta(minutes=1)
    scheduler.step(at)
    _bump(config)  # a replan in the same minute does not fire it again
    scheduler.step(at + timedelta(seconds=30))
    assert len(_launched(log)) == 1


def _fake_env(tmp_path: Path, gate: Path) -> dict[str, str]:
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({"items": {"gd-x": {"x.flac": "a"}}, "gate": str(gate)}))
    return {SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)}


def _wait_for(predicate, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 30
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


def test_collections_due_together_start_in_file_order_and_the_later_is_rescheduled(
    root: Path,
) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *", "0 3 * * *")
    db.connect(_state_db(root)).close()
    gate = root / "go"
    scheduler = Scheduler(config, argv_prefix=CHILD_PREFIX,
                          child_env=_fake_env(root, gate))
    try:
        scheduler.step(T0 + timedelta(minutes=1))
        with closing(db.connect_reader(_state_db(root))) as conn:
            repo = RescheduledRepo(conn)
            assert repo.get("fake-src", "coll1") is None
            assert repo.get("fake-src", "coll2") is not None
    finally:
        gate.touch()
        scheduler.stop()


def test_the_scheduler_closes_the_run_record_of_a_killed_tick(root: Path) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    gate = root / "go"
    scheduler = Scheduler(config, argv_prefix=CHILD_PREFIX,
                          child_env=_fake_env(root, gate))
    scheduler.step(T0 + timedelta(minutes=1))
    [run] = scheduler.children
    _wait_for(lambda: run.runs, "the tick's run record never opened")
    run.process.send_signal(signal.SIGKILL)
    assert run.done.wait(timeout=30)
    with closing(sqlite3.connect(_state_db(root))) as conn:
        finished = conn.execute(
            "SELECT finished_at FROM runs WHERE id=?", (run.runs[0]["run_id"],)
        ).fetchone()[0]
    assert finished is not None


def test_a_fault_in_planning_exits_non_zero(
    root: Path, recorder: tuple[tuple[str, ...], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    scheduler = Scheduler(config, argv_prefix=recorder[0])

    def broken(now: datetime) -> None:
        raise RuntimeError("planner bug")

    monkeypatch.setattr(scheduler, "step", broken)
    assert scheduler.run_forever() == 1


# --- shakedown schedule --check ------------------------------------------------------

def _check(config: Path) -> int:
    return CliRunner().invoke(main, ["--config", str(config), "schedule", "--check"]).exit_code


def test_check_passes_on_a_fresh_heartbeat(root: Path) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    heartbeat.record_heartbeat(
        _state_db(root), Heartbeat(datetime.now(UTC), "1", True, None), create=True
    )
    assert _check(config) == 0


def test_check_fails_when_the_heartbeat_is_missing(root: Path) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    assert _check(config) == 1
    assert not _state_db(root).exists(), "a check never creates the database"
    db.connect(_state_db(root)).close()
    assert _check(config) == 1


def test_check_fails_when_the_heartbeat_is_older_than_three_minutes(root: Path) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    old = datetime.now(UTC) - timedelta(minutes=3, seconds=5)
    heartbeat.record_heartbeat(_state_db(root), Heartbeat(old, "1", True, None), create=True)
    assert _check(config) == 1


def test_check_finds_the_database_while_the_file_is_invalid(root: Path) -> None:
    config = _write(root / "cfg.yaml", root, "0 3 * * *")
    heartbeat.record_heartbeat(
        _state_db(root), Heartbeat(datetime.now(UTC), "1", False, None), create=True
    )
    config.write_text(config.read_text() + "unknown_key: 1\n")
    assert _check(config) == 0
