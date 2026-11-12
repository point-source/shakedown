"""A finishing sync catches up rescheduled collections (§spec:rescheduled-syncs).

When a sync that holds the writer lock finishes its own work, it re-reads the config,
takes every rescheduled entry outstanding at that moment, drops the ones the config no
longer names, and syncs the rest as one multi-collection sync started oldest skip
first. Each catch-up is an ordinary sync and clears its entry whatever its result.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from shakedown.cli import main
from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.reconcile import run_reconcile
from shakedown.state import RescheduledRepo, RunRepo
from shakedown.sync import run_sync
from tests.conftest import write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

SRC = "fake-src"
OTHER = "other-src"


def _config(archive: Path, library: Path, *, names: tuple[str, ...] = ("a", "b", "c")) -> Config:
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(
                name=SRC,
                type="fake",
                collections=[CollectionConfig(name=name, query="*") for name in names],
            ),
            SourceConfig(
                name=OTHER,
                type="fake",
                collections=[CollectionConfig(name="b", query="*")],
            ),
        ],
    )


@pytest.fixture()
def config(tmp_roots: tuple[Path, Path]) -> Config:
    cfg = _config(*tmp_roots)
    assert cfg.state_db is not None
    connect(cfg.state_db).close()
    return cfg


@pytest.fixture()
def config_file(config: Config, tmp_path: Path) -> Path:
    return write_config(tmp_path / "shakedown.yaml", config)


@pytest.fixture(autouse=True)
def _seed() -> None:
    FakePlugin.items["gd-1"] = FakeItem(
        identifier="gd-1",
        files=[FakeFile(name="gd-1.flac", content=b"hello world")],
        metadata={"title": "Show"},
    )


def _mark(config: Config, source: str, collection: str, hour: int) -> None:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RescheduledRepo(conn).record(source, collection, datetime(2026, 11, 10, hour, 0, 0))


def _entries(config: Config) -> set[tuple[str, str]]:
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        return {
            (source, collection)
            for source, collection in conn.execute(
                "SELECT source_name, collection_name FROM rescheduled_syncs"
            )
        }


def _staged_order(monkeypatch: pytest.MonkeyPatch, hook=None) -> list[tuple[str, str]]:
    """Record the (source, collection) of each collection as it reaches staging, and
    run ``hook(source, collection)`` there first — "while that collection runs"."""
    from shakedown import sync

    real = sync._fill_tags
    order: list[tuple[str, str]] = []

    def spy(config, source_name, collection, items):  # type: ignore[no-untyped-def]
        order.append((source_name, collection.name))
        if hook is not None:
            hook(source_name, collection.name)
        return real(config, source_name, collection, items)

    monkeypatch.setattr("shakedown.sync._fill_tags", spy)
    return order


def _latest(config: Config, source: str, collection: str):  # type: ignore[no-untyped-def]
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        return RunRepo(conn).latest(source, collection)


def test_finishing_sync_catches_up_every_entry_oldest_skip_first(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operator acceptance (3): the next lock holder runs the oldest skipped
    collection first, then the rest, and clears every entry it caught up."""
    config.max_concurrent_collections = 1
    path = write_config(tmp_path / "shakedown.yaml", config)
    _mark(config, SRC, "c", 3)
    _mark(config, OTHER, "b", 4)
    _mark(config, SRC, "b", 2)
    order = _staged_order(monkeypatch)
    caplog.set_level(logging.INFO)

    assert run_sync(config, source_filter=SRC, collection_filter="a", config_path=path) == 0

    assert order == [(SRC, "a"), (SRC, "b"), (SRC, "c"), (OTHER, "b")]
    assert _entries(config) == set()
    for source, collection in ((SRC, "b"), (SRC, "c"), (OTHER, "b")):
        run = _latest(config, source, collection)
        assert run is not None and run.kind.value == "sync" and run.finished_at is not None
    catch_up_lines = [r.getMessage() for r in caplog.records if "catch-up" in r.getMessage()]
    assert any("fake-src/b" in m and "2026-11-10T02:00:00" in m for m in catch_up_lines)


def test_catch_ups_open_their_runs_oldest_skip_first_under_the_cap(
    config: Config, config_file: Path
) -> None:
    """With two collection workers, both catch-ups run in one bounded pass; the older
    skip is opened, and so started, first."""
    _mark(config, SRC, "c", 1)
    _mark(config, SRC, "b", 5)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 0
    b, c = _latest(config, SRC, "b"), _latest(config, SRC, "c")
    assert b is not None and c is not None
    assert c.id < b.id


def test_entry_recorded_during_the_catch_up_waits_for_the_next_sync(
    config: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def skip_arrives(source: str, collection: str) -> None:
        if (source, collection) == (SRC, "b"):
            _mark(config, SRC, "c", 9)

    _staged_order(monkeypatch, skip_arrives)
    _mark(config, SRC, "b", 2)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 0
    assert _entries(config) == {(SRC, "c")}


def test_collection_removed_from_the_config_is_dropped_and_logged(
    config: Config, config_file: Path, tmp_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def remove_c(source: str, collection: str) -> None:
        if (source, collection) == (SRC, "a"):
            write_config(config_file, _config(*tmp_roots, names=("a", "b")))

    order = _staged_order(monkeypatch, remove_c)
    _mark(config, SRC, "b", 2)
    _mark(config, SRC, "c", 3)
    caplog.set_level(logging.INFO)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 0
    assert order == [(SRC, "a"), (SRC, "b")]
    assert _entries(config) == set()
    dropped = [r for r in caplog.records
               if r.levelno == logging.WARNING and "fake-src/c" in r.getMessage()]
    assert dropped and "no longer" in dropped[0].getMessage()


def test_catch_up_reads_the_config_as_it_is_now(
    config: Config, tmp_roots: tuple[Path, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection added after the running sync loaded its config is caught up."""
    path = write_config(tmp_path / "shakedown.yaml", _config(*tmp_roots, names=("a", "d")))
    order = _staged_order(monkeypatch)
    _mark(config, SRC, "d", 2)
    assert run_sync(config, source_filter=SRC, collection_filter="a", config_path=path) == 0
    assert order == [(SRC, "a"), (SRC, "d")]
    assert _entries(config) == set()


def test_invalid_config_at_catch_up_keeps_every_entry_and_fails_the_run(
    config: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def break_config(source: str, collection: str) -> None:
        config_file.write_text("archive_root: [unclosed\n")

    order = _staged_order(monkeypatch, break_config)
    _mark(config, SRC, "b", 2)
    _mark(config, SRC, "c", 3)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 1
    assert order == [(SRC, "a")]
    assert _entries(config) == {(SRC, "b"), (SRC, "c")}
    a = _latest(config, SRC, "a")
    assert a is not None and a.items_failed == 0 and not a.errors
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("catch-up" in m and "shakedown.yaml" in m for m in errors)


def test_stale_catch_up_clears_its_entry_and_fails_the_run(
    config: Config, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    FakePlugin.unreachable_sources.add(OTHER)
    _mark(config, OTHER, "b", 2)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 1
    assert _entries(config) == set()
    run = _latest(config, OTHER, "b")
    assert run is not None and run.stale
    failed = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("other-src/b" in m and "STALE" in m and "catch-up" in m for m in failed)


def test_crashed_catch_up_clears_its_entry_and_fails_the_run(
    config: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def crash_c(source: str, collection: str) -> None:
        if (source, collection) == (SRC, "c"):
            raise RuntimeError("boom")

    _staged_order(monkeypatch, crash_c)
    _mark(config, SRC, "b", 2)
    _mark(config, SRC, "c", 3)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    config_path=config_file) == 1
    assert _entries(config) == set()
    run = _latest(config, SRC, "c")
    assert run is not None and run.finished_at is not None
    assert any("crashed" in e for e in run.errors)
    failed = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("fake-src/c" in m and "catch-up" in m for m in failed)


def test_own_stale_collection_with_an_entry_is_caught_up_in_the_same_run(
    config: Config, config_file: Path
) -> None:
    """Own work that ends stale keeps the entry; the catch-up takes every entry
    outstanding when it starts, so it retries the collection and clears it."""
    FakePlugin.unreachable_sources.add(OTHER)
    _mark(config, OTHER, "b", 2)
    assert run_sync(config, source_filter=OTHER, collection_filter="b",
                    config_path=config_file) == 1
    assert FakePlugin.list_calls == 2
    assert _entries(config) == set()


def test_refresh_metadata_and_dry_run_never_catch_up(
    config: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _staged_order(monkeypatch)
    _mark(config, SRC, "b", 2)
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    refresh_metadata=True, config_path=config_file) == 0
    assert run_sync(config, source_filter=SRC, collection_filter="a",
                    dry_run=True, config_path=config_file) == 0
    assert order == []
    assert _entries(config) == {(SRC, "b")}


def test_cli_sync_catches_up_and_exits_non_zero_when_a_catch_up_fails(
    config: Config, config_file: Path
) -> None:
    FakePlugin.unreachable_sources.add(OTHER)
    _mark(config, SRC, "b", 2)
    _mark(config, OTHER, "b", 3)
    result = CliRunner().invoke(
        main, ["--config", str(config_file), "sync", "--source", SRC, "--collection", "a"]
    )
    assert result.exit_code == 1, result.output
    assert _entries(config) == set()
    b = _latest(config, SRC, "b")
    assert b is not None and not b.stale and b.finished_at is not None


def test_cli_sync_exits_zero_when_own_work_and_catch_ups_succeed(
    config: Config, config_file: Path
) -> None:
    _mark(config, SRC, "b", 2)
    result = CliRunner().invoke(
        main, ["--config", str(config_file), "sync", "--source", SRC, "--collection", "a"]
    )
    assert result.exit_code == 0, result.output
    assert _entries(config) == set()


def test_rebuilt_database_starts_with_no_entries(config: Config) -> None:
    """§spec:rescheduled-syncs Rebuilds: a database rebuilt by reconcile from the
    archive tree carries no rescheduled entries."""
    assert run_sync(config) == 0
    _mark(config, SRC, "b", 2)
    assert config.state_db is not None
    config.state_db.unlink()
    assert run_reconcile(config) == 0
    assert _entries(config) == set()
