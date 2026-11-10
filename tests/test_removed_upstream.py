"""Removed upstream is a fact, not an error (§spec:item-lifecycle): the date a show was
first found missing from a complete listing is recorded, kept while it stays missing,
and cleared when the show is listed again."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import Item, ItemStatus
from shakedown.reconcile import run_reconcile
from shakedown.state import ItemRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

# An older takedown date, seeded so "the date is unchanged" is provable within a test.
EARLIER = datetime(2026, 1, 2, 3, 4, 5)


def _seed(identifier: str, content: bytes = b"audio") -> FakeItem:
    item = FakeItem(identifier=identifier, files=[FakeFile(name="t01.flac", content=content)])
    FakePlugin.items[identifier] = item
    return item


def _row(config: Config, identifier: str) -> Item:
    row = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", identifier)  # type: ignore[arg-type]
    assert row is not None
    return row


def _set_date(config: Config, identifier: str, when: datetime | None) -> None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    conn.execute(
        "UPDATE items SET disappeared_at=? WHERE identifier=?",
        (when.isoformat() if when else None, identifier),
    )
    conn.commit()


def _dated_today(run: Callable[[], int]) -> tuple[int, date, date]:
    """Run, returning its exit code and the dates either side of it, so an assertion on
    "today" holds across midnight."""
    before = date.today()
    code = run()
    return code, before, date.today()


# --- recording, through sync -------------------------------------------------------


def test_a_show_missing_from_the_listing_records_today(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a")
    assert run_sync(config) == 0
    assert _row(config, "gd-a").disappeared_at is None

    FakePlugin.items.clear()
    code, before, after = _dated_today(lambda: run_sync(config))

    assert code == 0
    row = _row(config, "gd-a")
    assert row.status == ItemStatus.DISAPPEARED
    assert row.disappeared_at is not None and before <= row.disappeared_at.date() <= after


def test_a_show_still_missing_keeps_its_first_date(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0
    _set_date(config, "gd-a", EARLIER)

    assert run_sync(config) == 0

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.DISAPPEARED
    assert row.disappeared_at == EARLIER


def test_a_show_listed_again_unchanged_clears_its_date(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    returning = _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0
    assert _row(config, "gd-a").disappeared_at is not None

    FakePlugin.items["gd-a"] = returning
    assert run_sync(config) == 0

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.COMPLETE
    assert row.disappeared_at is None


def test_a_show_listed_again_unchanged_on_an_incremental_collection_clears_its_date(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots, incremental_discovery=True)
    returning = _seed("gd-a")
    returning.change_signal = "sig-1"
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0

    FakePlugin.items["gd-a"] = returning
    assert run_sync(config) == 0

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.COMPLETE
    assert row.disappeared_at is None


def test_a_show_listed_again_with_new_files_clears_its_date(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a", b"v1")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0

    _seed("gd-a", b"v2, re-uploaded")
    assert run_sync(config) == 0

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.COMPLETE
    assert row.disappeared_at is None


def test_a_pruned_show_keeps_the_date_of_its_takedown(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots, prune_disappeared=True)
    _seed("gd-a")
    assert run_sync(config) == 0

    FakePlugin.items.clear()
    code, before, after = _dated_today(lambda: run_sync(config))

    assert code == 0
    row = _row(config, "gd-a")
    assert row.status == ItemStatus.PRUNED
    assert row.disappeared_at is not None and before <= row.disappeared_at.date() <= after


def test_pruning_a_show_that_had_already_disappeared_keeps_its_first_date(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    retain = make_config(archive, library)
    _seed("gd-a")
    assert run_sync(retain) == 0
    FakePlugin.items.clear()
    assert run_sync(retain) == 0
    _set_date(retain, "gd-a", EARLIER)

    assert run_sync(make_config(archive, library, prune_disappeared=True)) == 0

    row = _row(retain, "gd-a")
    assert row.status == ItemStatus.PRUNED
    assert row.disappeared_at == EARLIER


def test_pruning_a_show_with_no_known_date_invents_none(tmp_roots: tuple[Path, Path]) -> None:
    """A show that disappeared before the date was recorded has none; pruning it later
    is not the day it was first found missing."""
    archive, library = tmp_roots
    retain = make_config(archive, library)
    _seed("gd-a")
    assert run_sync(retain) == 0
    FakePlugin.items.clear()
    assert run_sync(retain) == 0
    _set_date(retain, "gd-a", None)

    assert run_sync(make_config(archive, library, prune_disappeared=True)) == 0

    row = _row(retain, "gd-a")
    assert row.status == ItemStatus.PRUNED
    assert row.disappeared_at is None


# --- recording, through reconcile --------------------------------------------------


def test_reconcile_records_today_for_a_show_it_finds_missing(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()

    code, before, after = _dated_today(lambda: run_reconcile(config))

    assert code == 0
    row = _row(config, "gd-a")
    assert row.status == ItemStatus.DISAPPEARED
    assert row.disappeared_at is not None and before <= row.disappeared_at.date() <= after


def test_reconcile_keeps_the_first_date_of_a_show_still_missing(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0
    _set_date(config, "gd-a", EARLIER)

    assert run_reconcile(config) == 0

    assert _row(config, "gd-a").disappeared_at == EARLIER


def test_reconcile_keeps_the_date_when_the_source_is_unreachable(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    config = make_config(*tmp_roots)
    _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0
    _set_date(config, "gd-a", EARLIER)

    def listing_fails(self, collection):
        raise ConnectionError("upstream unreachable")
        yield  # pragma: no cover  (keeps it a generator)

    monkeypatch.setattr(FakePlugin, "list_items", listing_fails)
    assert run_reconcile(config) == 1

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.DISAPPEARED
    assert row.disappeared_at == EARLIER


def test_reconcile_clears_the_date_of_a_show_listed_again(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    returning = _seed("gd-a")
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    assert run_sync(config) == 0

    FakePlugin.items["gd-a"] = returning
    assert run_reconcile(config) == 0

    row = _row(config, "gd-a")
    assert row.status == ItemStatus.COMPLETE
    assert row.disappeared_at is None


# --- the record ---------------------------------------------------------------------


def test_saving_a_show_without_a_date_clears_it(tmp_path: Path) -> None:
    """Unlike the other timestamps, the date is never kept from the old row: a show
    back upstream must lose it."""
    items = ItemRepo(connect(tmp_path / "state.db"))
    gone = Item("src", "coll", "gd-a", ItemStatus.DISAPPEARED, disappeared_at=EARLIER)
    items.upsert(gone)
    assert (row := items.get("src", "coll", "gd-a")) is not None
    assert row.disappeared_at == EARLIER

    items.upsert(Item("src", "coll", "gd-a", ItemStatus.COMPLETE))

    assert (row := items.get("src", "coll", "gd-a")) is not None
    assert row.disappeared_at is None
