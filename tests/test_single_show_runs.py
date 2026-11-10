"""Single-show operations behave like a sync of exactly those shows
(§spec:single-show-runs, §req:success-criteria #23)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shakedown.db import connect
from shakedown.models import RunKind
from shakedown.state import DriftRepo, ItemRepo, OperationOutcomeRepo, RunRepo
from shakedown.status import _collection_summary
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _add_show(identifier: str, content: bytes = b"audio") -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier, files=[FakeFile(name=f"{identifier}.flac", content=content)]
    )


def _summary(config) -> dict:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    return _collection_summary(
        config, "fake-src", "coll1", ItemRepo(conn), RunRepo(conn), DriftRepo(conn),
        OperationOutcomeRepo(conn),
    )


def test_status_keeps_a_stale_sync_flagged_after_a_single_show_run(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Only a sync lists the source, so only a sync can set or clear STALE: a later
    single-show run is the latest run, but the collection stays stale."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _add_show("gd-a")
    FakePlugin.unreachable_sources.add("fake-src")
    assert run_sync(config) == 1
    conn = connect(config.state_db)  # type: ignore[arg-type]
    runs = RunRepo(conn)
    refetch = runs.start("fake-src", "coll1", datetime.now(), kind=RunKind.REFETCH)
    refetch.finished_at = datetime.now()
    runs.finish(refetch)

    summary = _summary(config)

    assert summary["last_run"]["kind"] == "refetch"
    assert summary["stale"] is True
