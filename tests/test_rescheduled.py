"""Syncs skipped because another run holds the writer lock (§spec:rescheduled-syncs).

A `sync --collection` that finds the lock held records each (source, collection) it
covers, once, with the time of its oldest skip, and still exits 1. Recording never
takes the lock and never migrates the database. A sync that finishes a collection
with a complete enumeration clears its entry.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.lock import writer_lock
from shakedown.reconcile import run_reconcile
from shakedown.restage import run_restage
from shakedown.state import RescheduledRepo
from shakedown.sync import refetch_item, run_sync
from shakedown.trigger import READY_FD_ENV
from shakedown.verify import run_verify
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

SRC = "fake-src"


def _config(archive: Path, library: Path) -> Config:
    """Three collections on one source, plus a second source that also has a `b`."""
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(
                name=SRC,
                type="fake",
                collections=[
                    CollectionConfig(name=name, query="*")
                    for name in ("a", "b", "c")
                ],
            ),
            SourceConfig(
                name="other-src",
                type="fake",
                collections=[CollectionConfig(name="b", query="*")],
            ),
        ],
    )


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]) -> Config:
    cfg = _config(*tmp_roots)
    assert cfg.state_db is not None
    connect(cfg.state_db).close()  # a database at the current schema, as after any run
    return cfg


def _seed(identifier: str = "gd-1") -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=f"{identifier}.flac", content=b"hello world")],
        metadata={"title": "Show"},
    )


def _entries(config: Config) -> dict[tuple[str, str], str]:
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        return {
            (source, collection): since
            for source, collection, since in conn.execute(
                "SELECT source_name, collection_name, since FROM rescheduled_syncs"
            )
        }


def _version(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]


# --- recording --------------------------------------------------------------------


def test_lock_blocked_collection_sync_records_one_entry(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Operator acceptance (1): a lock-blocked run records one entry, logs that the
    collection was rescheduled with its skip time, and still exits 1."""
    caplog.set_level(logging.INFO)
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1

    entries = _entries(config)
    assert list(entries) == [(SRC, "b")]
    since = entries[(SRC, "b")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "rescheduled" in r.getMessage() and f"{SRC}/b" in r.getMessage()
        and since in r.getMessage()
        for r in warnings
    ), caplog.text
    # The refusal still names the writer lock (§spec:cli).
    assert "another shakedown run" in caplog.text


def test_second_skip_keeps_the_original_time(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Operator acceptance (2) and (5): a second skip neither adds an entry nor moves
    the time, and its warning names the oldest skip."""
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
        first = _entries(config)
        caplog.clear()
        caplog.set_level(logging.WARNING)
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    assert _entries(config) == first
    assert first[(SRC, "b")] in caplog.text


def test_each_skipped_collection_gets_its_own_entry(config: Config) -> None:
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
        assert run_sync(config, source_filter=SRC, collection_filter="c") == 1
    assert set(_entries(config)) == {(SRC, "b"), (SRC, "c")}


def test_collection_without_source_records_every_collection_it_covers(
    config: Config,
) -> None:
    """`sync --collection b` covers `b` on every source that has one."""
    with writer_lock(config):
        assert run_sync(config, collection_filter="b") == 1
    assert set(_entries(config)) == {(SRC, "b"), ("other-src", "b")}


def test_refusals_that_are_not_rescheduled_leave_no_entry(config: Config) -> None:
    """§spec:rescheduled-syncs "What is not rescheduled": these refuse as before."""
    _seed()
    assert run_sync(config) == 0
    with writer_lock(config):
        assert run_sync(config) == 1  # all collections
        assert run_sync(config, source_filter=SRC) == 1  # every collection of a source
        assert run_sync(config, collection_filter="b", refresh_metadata=True) == 1
        assert run_restage(config) == 1
        assert run_reconcile(config) == 1
        assert refetch_item(config, "gd-1") == 1
        assert run_verify(
            config, source_filter=None, collection_filter=None, deep=True,
            reconform=True, list_drift=False, assume_yes=True,
        ) == 1
        # A dry run never takes the lock.
        assert run_sync(config, collection_filter="b", dry_run=True) == 0
    assert _entries(config) == {}


def test_a_sync_started_by_serve_announces_busy_and_records_nothing(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `POST /sync` child is refused with 409 and nothing is rescheduled
    (§spec:api-sync-runs)."""
    r, w = os.pipe()
    monkeypatch.setenv(READY_FD_ENV, str(w))
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    with os.fdopen(r, "rb") as reader:
        assert reader.readline() == b'{"state": "busy"}\n'
    assert _entries(config) == {}


def test_older_schema_records_nothing_and_is_not_migrated(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A skip while the previous release still writes (DB at v8): nothing recorded,
    the log says why, the tick exits 1, and the DB stays at v8."""
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        conn.execute("DROP TABLE rescheduled_syncs")
        conn.execute("UPDATE schema_version SET version = 8")
        conn.commit()

    caplog.set_level(logging.WARNING)
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1

    assert _version(config.state_db) == 8
    with closing(sqlite3.connect(config.state_db)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "rescheduled_syncs" not in tables
    assert "not rescheduled" in caplog.text
    assert "schema v8" in caplog.text


def test_busy_database_records_nothing(
    config: Config, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running writer holds the database past the bounded wait."""
    monkeypatch.setattr("shakedown.db.BUSY_TIMEOUT_MS", 50)
    assert config.state_db is not None
    holder = sqlite3.connect(config.state_db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    caplog.set_level(logging.WARNING)
    try:
        with writer_lock(config):
            assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _entries(config) == {}
    assert "not rescheduled" in caplog.text
    assert "busy" in caplog.text


def test_missing_database_records_nothing_and_creates_none(
    tmp_roots: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config(*tmp_roots)
    assert cfg.state_db is not None
    caplog.set_level(logging.WARNING)
    with writer_lock(cfg):
        assert run_sync(cfg, source_filter=SRC, collection_filter="b") == 1
    assert not cfg.state_db.exists()
    assert "not rescheduled" in caplog.text


def test_entries_written_by_the_repo_read_back_as_times(config: Config) -> None:
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        since = RescheduledRepo(conn).get(SRC, "b")
    assert isinstance(since, datetime)
    assert since.isoformat() == _entries(config)[(SRC, "b")]


# --- clearing ---------------------------------------------------------------------


def _mark(config: Config, *scope: tuple[str, str]) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        repo = RescheduledRepo(conn)
        for source, collection in scope:
            repo.record(source, collection, datetime(2026, 11, 10, 3, 0, 0))


def test_finishing_the_collection_clears_only_its_entry(config: Config) -> None:
    """Operator acceptance (4): the entry clears when a sync finishes the collection."""
    _seed()
    _mark(config, (SRC, "b"), (SRC, "c"))
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 0
    assert set(_entries(config)) == {(SRC, "c")}


def test_completed_with_item_issues_clears_the_entry(config: Config) -> None:
    """A complete enumeration with item issues clears: the next sync retries the
    failed items anyway."""
    FakePlugin.items["evil"] = FakeItem(
        identifier="evil", files=[FakeFile(name="../../escape.flac", content=b"x")],
    )
    _mark(config, (SRC, "b"))
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    assert _entries(config) == {}


def test_stale_enumeration_keeps_the_entry(config: Config) -> None:
    FakePlugin.unreachable_sources.add(SRC)
    _mark(config, (SRC, "b"))
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    assert set(_entries(config)) == {(SRC, "b")}


def test_crashed_collection_keeps_the_entry(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*_args: object) -> dict[str, list[str]]:
        raise RuntimeError("boom")

    monkeypatch.setattr("shakedown.sync._fill_tags", crash)
    _mark(config, (SRC, "b"))
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
    assert set(_entries(config)) == {(SRC, "b")}


def test_skip_recorded_while_the_run_goes_is_cleared_when_it_finishes(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shakedown import sync

    real = sync._fill_tags

    def skip_arrives(*args: object) -> dict[str, list[str]]:
        _mark(config, (SRC, "b"))
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr("shakedown.sync._fill_tags", skip_arrives)
    _seed()
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 0
    assert _entries(config) == {}


def test_all_collections_sync_clears_every_finished_collection(config: Config) -> None:
    _seed()
    _mark(config, (SRC, "b"), (SRC, "c"), ("other-src", "b"))
    assert run_sync(config) == 0
    assert _entries(config) == {}


def test_refresh_metadata_and_dry_run_keep_the_entry(config: Config) -> None:
    """Neither finishes the collection with a complete enumeration of its own."""
    _seed()
    _mark(config, (SRC, "b"))
    assert run_sync(config, source_filter=SRC, collection_filter="b",
                    refresh_metadata=True) == 0
    assert run_sync(config, source_filter=SRC, collection_filter="b", dry_run=True) == 0
    assert set(_entries(config)) == {(SRC, "b")}


# --- status -----------------------------------------------------------------------


def test_status_json_carries_the_oldest_skip_per_collection(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status --json` (and `GET /status`, which shares the summary) carry
    `rescheduled: {"since": <time>}` or null; an entry for a collection no longer
    configured is not shown."""
    import json

    from shakedown.status import print_status

    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b") == 1
        assert run_sync(config, source_filter=SRC, collection_filter="c") == 1
    _mark(config, (SRC, "gone"))
    entries = _entries(config)
    capsys.readouterr()

    print_status(config, as_json=True)

    summaries = {
        (s["source"], s["collection"]): s["rescheduled"]
        for s in json.loads(capsys.readouterr().out)
    }
    assert summaries == {
        (SRC, "a"): None,
        (SRC, "b"): {"since": entries[(SRC, "b")]},
        (SRC, "c"): {"since": entries[(SRC, "c")]},
        ("other-src", "b"): None,
    }


def test_status_text_shows_the_rescheduled_collection(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    from shakedown.status import print_status

    _mark(config, (SRC, "b"))
    capsys.readouterr()

    print_status(config, as_json=False)

    out = capsys.readouterr().out
    line = (
        "  rescheduled: first skipped 2026-11-10T03:00:00; "
        "runs when the current sync finishes"
    )
    assert out.count(line) == 1
    section = out.split(f"=== {SRC}/b ===")[1].split("===")[0]
    assert line in section


# --- reading an older schema --------------------------------------------------------


def _roll_back_to_v8(config: Config) -> None:
    """The database as the previous release left it: at v8, with no rescheduled
    record."""
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        conn.execute("DROP TABLE rescheduled_syncs")
        conn.execute("UPDATE schema_version SET version = 8")
        conn.commit()


def test_status_reads_an_older_schema_without_migrating_it(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only writers holding the writer lock migrate: `status` run while the previous
    release still writes leaves the database at its version and reports no
    rescheduled entries (Q-1-1, operator 2026-11-12)."""
    import json

    from shakedown.status import print_status, show_item

    _seed()
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 0
    _roll_back_to_v8(config)
    capsys.readouterr()

    print_status(config, as_json=True)
    summaries = {
        (s["source"], s["collection"]): s for s in json.loads(capsys.readouterr().out)
    }
    print_status(config, as_json=False)
    text = capsys.readouterr().out
    show_item(config, "gd-1")
    shown = capsys.readouterr().out

    assert config.state_db is not None
    assert _version(config.state_db) == 8
    assert all(s["rescheduled"] is None for s in summaries.values())
    b = summaries[(SRC, "b")]
    assert b["counts"]["complete"] == 1
    assert b["last_run"] is not None and b["last_run"]["items_new"] == 1
    assert summaries[(SRC, "a")]["last_run"] is None
    assert "rescheduled" not in text
    assert f"=== {SRC}/b ===" in text and "last run: never" in text
    assert f"=== {SRC}/b/gd-1 ===" in shown


def test_status_on_a_new_install_creates_the_database(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """No run has written a database yet: status still answers."""
    from shakedown.status import print_status

    cfg = _config(*tmp_roots)
    print_status(cfg, as_json=False)
    assert "last run: never" in capsys.readouterr().out


def test_dry_run_reads_an_older_schema_without_migrating_it(config: Config) -> None:
    """A dry run takes no writer lock, so it must not migrate either: checking a
    collection while the previous release still writes leaves the database at v8."""
    _seed()
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 0
    _roll_back_to_v8(config)
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="b", dry_run=True) == 0
    assert config.state_db is not None
    assert _version(config.state_db) == 8


def test_skip_is_recorded_when_the_state_path_needs_escaping(tmp_path: Path) -> None:
    """A state path with URI-special characters names the real database, never a
    stray file beside it."""
    archive, library = tmp_path / "a#1" / "archive", tmp_path / "library"
    archive.mkdir(parents=True)
    library.mkdir()
    cfg = _config(archive, library)
    assert cfg.state_db is not None
    connect(cfg.state_db).close()
    with writer_lock(cfg):
        assert run_sync(cfg, source_filter=SRC, collection_filter="b") == 1
    assert set(_entries(cfg)) == {(SRC, "b")}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#1", "library"]


def test_verify_without_reconform_reads_an_older_schema_without_migrating_it(
    config: Config,
) -> None:
    """Only writers holding the writer lock migrate (Q-1-1, Q-1-2): a shallow or deep
    verify while the previous release still writes leaves the database at v8."""
    _seed()
    assert run_sync(config, source_filter=SRC, collection_filter="b") == 0
    _roll_back_to_v8(config)
    for deep in (False, True):
        assert run_verify(
            config, source_filter=None, collection_filter=None, deep=deep,
            reconform=False, list_drift=False, assume_yes=False,
        ) == 0
    assert config.state_db is not None
    assert _version(config.state_db) == 8


def test_validate_reads_an_older_schema_without_migrating_it(
    config: Config, tmp_path: Path
) -> None:
    """validate still records its outcome, but never migrates (Q-1-2)."""
    from shakedown.validate import run_validate
    from tests.conftest import write_config

    _roll_back_to_v8(config)
    run_validate(write_config(tmp_path / "cfg.yaml", config))
    assert config.state_db is not None
    assert _version(config.state_db) == 8
    with closing(sqlite3.connect(config.state_db)) as conn:
        recorded = conn.execute(
            "SELECT COUNT(*) FROM operation_outcomes WHERE operation='validate'"
        ).fetchone()[0]
    assert recorded > 0
