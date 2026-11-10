"""Single-show operations behave like a sync of exactly those shows
(§spec:single-show-runs, §req:success-criteria #23)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from shakedown import discovery
from shakedown.config import Config, WebhookHandoff
from shakedown.db import connect
from shakedown.models import ItemStatus, RunKind
from shakedown.plugins.base import ItemLookup
from shakedown.state import DriftRepo, ItemRepo, OperationOutcomeRepo, PendingHandoffRepo, RunRepo
from shakedown.status import _collection_summary
from shakedown.sync import NO_FILES_REASON, refetch_item, run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

HOOK = "http://receiver.example/hook"


class _Webhooks:
    """Stands in for httpx.post: URLs in `down` cannot be reached; others answer 200."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = down or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None):
        assert json is not None
        self.calls.append((url, json))
        if url in self.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    def staged(self) -> list[list[str]]:
        """The shows named by each handoff notice, in order."""
        return [
            sorted(entry["identifier"] for entry in body["staged"])
            for _, body in self.calls
            if body["event"] == "sync.complete"
        ]


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


def _hooked(archive: Path, library: Path, url: str = HOOK) -> Config:
    return make_config(archive, library, on_complete=WebhookHandoff(webhook=url))


def _show_paths(config: Config, identifier: str) -> tuple[Path, Path]:
    name = f"{identifier}.flac"
    return (
        config.archive_root / "fake-src" / "coll1" / identifier / name,
        config.library_root / "fake-src" / "coll1" / identifier / name,
    )


def test_refetch_in_a_large_collection_looks_up_one_show_and_hands_it_off(
    tmp_roots: tuple[Path, Path],
) -> None:
    """§req:success-criteria #23 test path: a handful of upstream requests, a recorded
    refetch run, and a handoff notice naming the show."""
    archive, library = tmp_roots
    config = _hooked(archive, library)
    for n in range(200):
        _add_show(f"gd-{n:03d}")
    hooks = _Webhooks()
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        assert run_sync(config) == 0
        FakePlugin.lookup_calls.clear()
        FakePlugin.list_calls = 0
        hooks.calls.clear()

        assert refetch_item(config, "gd-050") == 0

    assert [identifier for identifier, _ in FakePlugin.lookup_calls] == ["gd-050"]
    assert FakePlugin.list_calls == 0, "a refetch never lists the collection"
    assert FakePlugin.fetch_count["gd-050"] == 2
    last = _summary(config)["last_run"]
    assert last["kind"] == "refetch"
    assert (last["items_updated"], last["items_failed"]) == (1, 0)
    assert hooks.staged() == [["gd-050"]]


def test_refetch_resends_shows_whose_handoff_was_not_delivered(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = _hooked(archive, library)
    _add_show("gd-a")
    _add_show("gd-b")
    hooks = _Webhooks(down={HOOK})
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        assert run_sync(config) == 0
        hooks.down.clear()
        hooks.calls.clear()

        assert refetch_item(config, "gd-a") == 0

    assert hooks.staged() == [["gd-a", "gd-b"]]
    conn = connect(config.state_db)  # type: ignore[arg-type]
    assert PendingHandoffRepo(conn).list("fake-src", "coll1") == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (ConnectionResetError("connection reset"), "lookup failed"),
        (ItemLookup(metadata={}, files=[]), NO_FILES_REASON),
    ],
    ids=["lookup-fails", "absent-from-answer"],
)
def test_refetch_that_finds_nothing_fails_and_leaves_the_show_alone(
    tmp_roots: tuple[Path, Path], reply: Any, reason: str
) -> None:
    """The run records the show failed with the reason and the command exits non-zero;
    the archive copy, its library placement, and its record are untouched."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _add_show("gd-a")
    assert run_sync(config) == 0
    archived, staged = _show_paths(config, "gd-a")
    inode = staged.stat().st_ino
    FakePlugin.lookup_faults["gd-a"] = [reply]

    assert refetch_item(config, "gd-a") == 1

    last = _summary(config)["last_run"]
    assert last["kind"] == "refetch"
    assert last["items_failed"] == 1
    assert any(e.startswith("gd-a: ") and reason in e for e in last["errors"])
    assert archived.read_bytes() == b"audio"
    assert staged.stat().st_ino == inode == archived.stat().st_ino
    conn = connect(config.state_db)  # type: ignore[arg-type]
    row = ItemRepo(conn).get("fake-src", "coll1", "gd-a")
    assert row is not None and row.status == ItemStatus.COMPLETE
    assert FakePlugin.fetch_count["gd-a"] == 1


def test_refetch_lookup_and_download_draw_from_the_source_budget(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _add_show("gd-a")
    assert run_sync(config) == 0
    slots: list[None] = []
    real_slot = discovery.SourceBudget.slot

    def counted(self: discovery.SourceBudget):
        slots.append(None)
        return real_slot(self)

    monkeypatch.setattr(discovery.SourceBudget, "slot", counted)

    assert refetch_item(config, "gd-a") == 0

    assert len(slots) == 2, "one slot for the lookup, one for the download"


def test_a_crashed_refetch_closes_its_run_and_outcome(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _add_show("gd-a")
    assert run_sync(config) == 0

    def crash(self: Any) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("shakedown.placement.LibraryPlacement.finish", crash)

    assert refetch_item(config, "gd-a") == 1

    summary = _summary(config)
    assert summary["last_run"]["kind"] == "refetch"
    assert summary["last_run"]["finished_at"] is not None
    assert any("disk on fire" in e for e in summary["last_run"]["errors"])
    assert summary["recovery"]["operation"] == "refetch"
    assert summary["recovery"]["status"] == "failed_before_completion"


def test_a_refetch_clears_its_own_warning_but_never_a_syncs(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A single-show run did not redo a sync's work, so it cannot clear the sync's
    warning; a later good refetch clears an earlier failed refetch's."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _add_show("gd-a")
    assert run_sync(config) == 0
    FakePlugin.lookup_faults["gd-a"] = [ConnectionResetError("connection reset")]
    assert refetch_item(config, "gd-a") == 1
    assert _summary(config)["recovery"]["operation"] == "refetch"
    FakePlugin.lookup_faults.clear()
    assert refetch_item(config, "gd-a") == 0
    assert _summary(config)["recovery"] is None

    FakePlugin.unreachable_sources.add("fake-src")
    assert run_sync(config) == 1
    assert refetch_item(config, "gd-a") == 0

    recovery = _summary(config)["recovery"]
    assert recovery["operation"] == "sync"
    assert recovery["status"] == "stale_enumeration"
