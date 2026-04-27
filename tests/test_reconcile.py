"""reconcile: rebuild DB from on-disk archive + source manifests. PRD §10, §13."""
from __future__ import annotations

from pathlib import Path

from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.reconcile import run_reconcile
from shakedown.state import ItemRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def test_reconcile_rebuilds_db_from_archive(tmp_roots: tuple[Path, Path]) -> None:
    """Wipe the state DB; reconcile must restore the items table from on-disk + source."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    for n in range(3):
        FakePlugin.items[f"id-{n}"] = FakeItem(
            identifier=f"id-{n}",
            files=[FakeFile(name=f"id-{n}.flac", content=f"data-{n}".encode())],
        )

    assert run_sync(config) == 0

    # Disaster: nuke state.db; archive on disk is intact.
    config.state_db.unlink()  # type: ignore[union-attr]
    assert run_reconcile(config) == 0

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    for n in range(3):
        item = items.get("fake-src", "coll1", f"id-{n}")
        assert item is not None
        assert item.status == ItemStatus.COMPLETE
        assert item.recorded_manifest is not None
        assert item.recorded_manifest.files[0].name == f"id-{n}.flac"

    # Subsequent sync after reconcile must not re-fetch.
    fc_before = dict(FakePlugin.fetch_count)
    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == fc_before, "post-reconcile sync must be a no-op"


def test_reconcile_deletes_stale_db_rows(tmp_roots: tuple[Path, Path]) -> None:
    """A row in DB whose archive directory has been deleted on disk must be removed —
    reconcile is meant to be a true rebuild."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["keep"] = FakeItem(
        identifier="keep", files=[FakeFile(name="k.flac", content=b"k")]
    )
    FakePlugin.items["gone"] = FakeItem(
        identifier="gone", files=[FakeFile(name="g.flac", content=b"g")]
    )
    assert run_sync(config) == 0

    # User wipes one item's archive directory by hand.
    import shutil
    shutil.rmtree(archive / "fake-src" / "coll1" / "gone")
    # Source no longer enumerates it either (e.g., it was deleted upstream too).
    del FakePlugin.items["gone"]

    assert run_reconcile(config) == 0

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    assert items.get("fake-src", "coll1", "keep") is not None
    assert items.get("fake-src", "coll1", "gone") is None, (
        "reconcile must delete DB rows for archive dirs that no longer exist"
    )


def test_reconcile_marks_disappeared_for_on_disk_only_items(
    tmp_roots: tuple[Path, Path],
) -> None:
    """An on-disk item that the source no longer enumerates is recorded as DISAPPEARED."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["here"] = FakeItem(
        identifier="here", files=[FakeFile(name="h.flac", content=b"h")]
    )
    assert run_sync(config) == 0

    # Upstream pulls the item; archive copy is preserved.
    del FakePlugin.items["here"]

    assert run_reconcile(config) == 0
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "here")
    assert row is not None
    assert row.status == ItemStatus.DISAPPEARED


def test_reconcile_tolerates_source_unavailable(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """If `discover` raises (source down, network out), reconcile still records on-disk
    items rather than refusing to run."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["a"] = FakeItem(
        identifier="a", files=[FakeFile(name="a.flac", content=b"a")]
    )
    assert run_sync(config) == 0
    config.state_db.unlink()  # type: ignore[union-attr]

    def discover_explodes(self, collection):
        raise ConnectionError("upstream unreachable")
        yield  # pragma: no cover  (keeps it a generator)

    monkeypatch.setattr(FakePlugin, "discover", discover_explodes)
    assert run_reconcile(config) == 0

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "a")
    assert row is not None, "on-disk items must be reconciled even when source is unreachable"
    # No prior DB row + no source descriptor → COMPLETE with no manifest;
    # next sync (when source returns) will refresh the manifest.
    assert row.status == ItemStatus.COMPLETE
    assert row.recorded_manifest is None
