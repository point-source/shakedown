"""A sync's memory stays flat as a collection grows (a small NAS has 2-4 GB shared).

Each show's details (metadata, manifest) are held only while that show is being
handled, never for the whole listing. Metadata is made large and unique per show so
anything held for every show dominates the peak.
"""
from __future__ import annotations

import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

SHOWS = 300
METADATA_BYTES = 50_000  # per show: 15 MB across the collection


def _seed() -> None:
    for i in range(SHOWS):
        FakePlugin.items[f"gd-{i}"] = FakeItem(
            identifier=f"gd-{i}",
            files=[FakeFile(name=f"gd-{i}.flac", content=b"audio")],
            metadata={"title": f"Show {i}"},
        )


def _fresh_metadata(monkeypatch) -> None:
    """A real source builds new metadata objects for every lookup."""
    real_lookup = FakePlugin.lookup_item

    def lookup(self, identifier, timeout):
        answer = real_lookup(self, identifier, timeout)
        notes = identifier + "x" * METADATA_BYTES
        return replace(answer, metadata={**answer.metadata, "notes": notes})

    monkeypatch.setattr(FakePlugin, "lookup_item", lookup)


def _warm_up(config) -> None:
    """Run once on an empty collection, so one-time imports don't count."""
    assert run_sync(config) == 0


def _peak_mb(config) -> float:
    tracemalloc.start()
    try:
        assert run_sync(config) == 0
        return tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()


def _slow_downloads(monkeypatch) -> None:
    real_fetch = FakePlugin.fetch

    def slow_fetch(self, identifier, files, dest_dir):
        time.sleep(0.01)
        return real_fetch(self, identifier, files, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", slow_fetch)


def test_first_sync_does_not_hold_every_show_while_downloads_lag(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """Downloads take far longer than listing: shows listed but not yet handled must
    not pile up in memory."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _warm_up(config)
    _seed()
    _fresh_metadata(monkeypatch)
    _slow_downloads(monkeypatch)

    assert _peak_mb(config) < 5


def test_recurring_sync_does_not_hold_every_show(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A no-op weekly run: neither the shows on record nor the freshly listed details
    are kept for the whole collection."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed()
    _fresh_metadata(monkeypatch)
    assert run_sync(config) == 0  # everything on record, with its metadata

    assert _peak_mb(config) < 5


def test_restricted_shows_are_not_held_until_the_listing_ends(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """Stream-only shows are flagged from their metadata on every run; their details
    must not be kept for the whole listing either."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _warm_up(config)
    _seed()
    for item in FakePlugin.items.values():
        item.is_restricted = True
        item.restriction_reason = "stream-only"
    _fresh_metadata(monkeypatch)

    assert _peak_mb(config) < 5
