"""Archive write safety and run integrity: an empty or short upstream answer, or
half-written state, must fail — never count as done."""
from __future__ import annotations

from pathlib import Path

from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.state import ItemRepo
from shakedown.sync import refetch_item, run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _seed(identifier: str, *files: tuple[str, bytes]) -> FakeItem:
    item = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=n, content=c) for n, c in files]
        or [FakeFile(name=f"{identifier}.flac", content=b"hello world")],
        metadata={"title": "Show"},
    )
    FakePlugin.items[identifier] = item
    return item


def _row(config, identifier: str):
    return ItemRepo(connect(config.state_db)).get("fake-src", "coll1", identifier)  # type: ignore[arg-type]


# --- C2: an empty upstream file list never replaces the archive copy ------------


def test_empty_upstream_manifest_keeps_complete_archive_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"hello world"

    # archive.org answers with zero files (darked item, glitch, filter matches nothing).
    item.files = []
    run_sync(config)

    assert archived.read_bytes() == b"hello world", "local copy must be untouched"
    row = _row(config, "gd-show")
    assert row is not None
    assert row.status == ItemStatus.UNAVAILABLE
    assert row.restriction_reason
    assert row.archive_path == archived.parent
    assert row.recorded_manifest is not None and len(row.recorded_manifest.files) == 1
    assert FakePlugin.fetch_count["gd-show"] == 1


def test_empty_upstream_manifest_for_new_item_is_unavailable(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-empty").files = []

    run_sync(config)

    assert not (archive / "fake-src" / "coll1" / "gd-empty").exists()
    row = _row(config, "gd-empty")
    assert row is not None and row.status == ItemStatus.UNAVAILABLE
    assert FakePlugin.fetch_count.get("gd-empty", 0) == 0


def test_item_back_upstream_with_same_files_returns_to_complete(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    files = item.files
    item.files = []
    run_sync(config)
    assert _row(config, "gd-show").status == ItemStatus.UNAVAILABLE  # type: ignore[union-attr]

    item.files = files
    assert run_sync(config) == 0

    row = _row(config, "gd-show")
    assert row is not None and row.status == ItemStatus.COMPLETE
    assert row.restriction_reason is None
    assert FakePlugin.fetch_count["gd-show"] == 1, "same files on disk: no re-download"


def test_refetch_with_empty_upstream_manifest_keeps_archive_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    item.files = []

    assert refetch_item(config, "gd-show") == 1

    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"hello world"
    row = _row(config, "gd-show")
    assert row is not None and row.archive_path == archived.parent
