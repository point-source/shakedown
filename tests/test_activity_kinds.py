"""Every kind of run reports (§spec:run-activity): one activity entry per collection
it works on, with its kind and the counters that kind has; the rest stay null."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from shakedown.db import connect
from shakedown.reconcile import run_reconcile
from shakedown.restage import run_restage
from shakedown.sync import refetch_item, run_sync
from shakedown.verify import run_verify
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

LISTING = ("items_listed", "items_to_look_up", "items_looked_up")
FETCHING = ("items_to_fetch", "items_done", "items_failed")


def _seed(identifier: str, size: int = 100) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier, files=[FakeFile(f"{identifier}.flac", b"y" * size, format="Flac")]
    )


def _entries(config, kind: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(config.state_db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM run_activity WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()


def _latest_outcome_id(config, operation: str) -> int:
    with closing(sqlite3.connect(config.state_db)) as conn:
        return conn.execute(
            "SELECT MAX(id) FROM operation_outcomes WHERE operation=?", (operation,)
        ).fetchone()[0]


def _values(row: sqlite3.Row, columns: tuple[str, ...]) -> tuple:
    return tuple(row[c] for c in columns)


@pytest.fixture()
def mirrored(tmp_roots: tuple[Path, Path]):
    """A collection of two shows, synced once."""
    config = make_config(*tmp_roots)
    _seed("gd-a", 100)
    _seed("gd-b", 300)
    assert run_sync(config) == 0
    return config


def _verify(config, *, deep: bool = False, reconform: bool = False) -> int:
    return run_verify(
        config, source_filter=None, collection_filter=None, deep=deep,
        reconform=reconform, list_drift=False, assume_yes=True,
    )


def test_a_refetch_reports_its_lookups_and_downloads(mirrored) -> None:
    assert refetch_item(mirrored, "gd-b") == 0
    [entry] = _entries(mirrored, "refetch")
    assert entry["run_id"] is not None
    assert (entry["state"], entry["result"], entry["phase"]) == ("ended", "completed", "finishing")
    assert entry["items_listed"] is None  # a single-show run lists nothing
    assert _values(entry, ("items_to_look_up", "items_looked_up")) == (1, 1)
    assert _values(entry, FETCHING) == (1, 1, 0)
    assert (entry["bytes_downloaded"], entry["bytes_to_fetch"]) == (300, 300)


def test_a_restage_reports_the_shows_it_stages_and_lists_nothing(mirrored) -> None:
    assert run_restage(mirrored) == 0
    [entry] = _entries(mirrored, "restage")
    assert entry["run_id"] is None
    assert (entry["state"], entry["result"]) == ("ended", "completed")
    assert _values(entry, LISTING) == (None, None, None)
    assert _values(entry, FETCHING) == (2, 2, 0)
    assert (entry["bytes_downloaded"], entry["bytes_to_fetch"]) == (None, None)
    assert entry["speed_bytes_per_second"] is None
    # Its outcome holds its errors for the run history (§spec:run-history).
    assert entry["outcome_id"] == _latest_outcome_id(mirrored, "restage")


def test_a_verify_reports_the_shows_it_checks(mirrored) -> None:
    assert _verify(mirrored) == 0
    [entry] = _entries(mirrored, "verify")
    assert entry["run_id"] is None
    assert (entry["state"], entry["result"], entry["phase"]) == ("ended", "completed", "verifying")
    assert entry["outcome_id"] is None  # a verify records no outcome
    assert _values(entry, LISTING) == (None, None, None)
    assert _values(entry, FETCHING) == (2, 2, 0)
    assert entry["bytes_downloaded"] is None


def test_missing_files_are_a_verify_item_issue(mirrored) -> None:
    (mirrored.archive_root / "fake-src" / "coll1" / "gd-a" / "gd-a.flac").unlink()
    assert _verify(mirrored) == 1
    [entry] = _entries(mirrored, "verify")
    assert entry["result"] == "completed_with_item_issues"
    assert _values(entry, FETCHING) == (2, 1, 1)


def test_drift_is_not_a_failure_and_a_reconform_reports_its_own_runs(mirrored) -> None:
    """Drift is informational (§spec:verify-drift); a reconform is a single-show run
    with its own entry and run record."""
    (mirrored.archive_root / "fake-src" / "coll1" / "gd-a" / "gd-a.flac").write_bytes(b"z" * 100)
    assert _verify(mirrored, deep=True) == 0
    [deep] = _entries(mirrored, "verify")
    assert deep["result"] == "completed"
    assert _values(deep, FETCHING) == (2, 2, 0)

    assert _verify(mirrored, deep=True, reconform=True) == 0
    verifies = _entries(mirrored, "verify")
    [reconform] = _entries(mirrored, "reconform")
    assert len(verifies) == 2
    assert reconform["run_id"] is not None
    assert (reconform["result"], reconform["items_to_look_up"]) == ("completed", 1)
    assert _values(reconform, FETCHING) == (1, 1, 0)


def test_a_reconcile_reports_its_listing_and_fetches_nothing(mirrored) -> None:
    assert run_reconcile(mirrored) == 0
    [entry] = _entries(mirrored, "reconcile")
    assert entry["run_id"] is None
    assert (entry["state"], entry["result"]) == ("ended", "completed")
    assert _values(entry, LISTING) == (2, 2, 2)
    assert _values(entry, FETCHING) == (None, None, None)
    assert entry["bytes_downloaded"] is None
    assert entry["outcome_id"] == _latest_outcome_id(mirrored, "reconcile")


def test_a_plain_verify_on_a_pre_v10_database_records_nothing(mirrored) -> None:
    """`verify` takes no lock and never migrates (§spec:state): right after an upgrade
    it records no activity."""
    with closing(connect(mirrored.state_db)) as conn:
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
    assert _verify(mirrored) == 0
    with closing(sqlite3.connect(mirrored.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='run_activity'").fetchone() is None


def test_a_restage_counts_a_show_with_an_issue_as_failed_not_done(mirrored) -> None:
    """Done and failed never overlap: a show whose link is blocked is failed only."""
    folder = mirrored.library_root / "fake-src" / "coll1" / "gd-a"
    (folder / "gd-a.flac").unlink()
    (folder / "gd-a.flac").write_bytes(b"not shakedown's")  # a foreign file at the target
    assert run_restage(mirrored) == 1
    [entry] = _entries(mirrored, "restage")
    assert entry["result"] == "completed_with_item_issues"
    assert _values(entry, FETCHING) == (2, 1, 1)
