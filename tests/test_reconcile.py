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


def test_reconcile_keeps_prior_status_when_source_unreachable(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """An outage is not a takedown: reconcile must not mark known shows disappeared."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["a"] = FakeItem(
        identifier="a", files=[FakeFile(name="a.flac", content=b"a")]
    )
    assert run_sync(config) == 0

    def discover_explodes(self, collection):
        raise ConnectionError("upstream unreachable")
        yield  # pragma: no cover  (keeps it a generator)

    monkeypatch.setattr(FakePlugin, "list_items", discover_explodes)
    assert run_reconcile(config) == 1

    row = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", "a")  # type: ignore[arg-type]
    assert row is not None
    assert row.status == ItemStatus.COMPLETE
    assert row.recorded_manifest is not None


def test_reconcile_never_marks_a_listed_show_disappeared_because_its_lookup_failed(
    tmp_roots: tuple[Path, Path],
) -> None:
    """§spec:per-item-isolation: reconcile marks a show `disappeared` only when a
    complete listing omits it. A show the listing still has, whose lookup fails, keeps
    its status, manifest, and files; the failure is reported and reconcile exits 1."""
    from shakedown.models import OperationStatus, OperationType
    from shakedown.state import OperationOutcomeRepo

    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["a"] = FakeItem(identifier="a", files=[FakeFile(name="a.flac", content=b"a")])
    FakePlugin.items["b"] = FakeItem(identifier="b", files=[FakeFile(name="b.flac", content=b"b")])
    assert run_sync(config) == 0
    FakePlugin.lookup_faults["a"] = [ConnectionResetError("connection reset")]

    assert run_reconcile(config) == 1

    conn = connect(config.state_db)  # type: ignore[arg-type]
    row = ItemRepo(conn).get("fake-src", "coll1", "a")
    assert row is not None
    assert row.status == ItemStatus.COMPLETE
    assert row.recorded_manifest is not None
    assert (archive / "fake-src" / "coll1" / "a" / "a.flac").read_bytes() == b"a"
    other = ItemRepo(conn).get("fake-src", "coll1", "b")
    assert other is not None and other.status == ItemStatus.COMPLETE

    outcome = OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1")
    assert outcome is not None
    assert outcome.operation == OperationType.RECONCILE
    assert outcome.status == OperationStatus.COMPLETED_WITH_ITEM_ISSUES
    assert outcome.errors == ["a: lookup failed after 3 attempts: connection error"]


def test_reconcile_looks_up_only_shows_on_disk(tmp_roots: tuple[Path, Path]) -> None:
    """The listing says what is upstream; only shows on disk need their manifests."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["a"] = FakeItem(identifier="a", files=[FakeFile(name="a.flac", content=b"a")])
    assert run_sync(config) == 0
    FakePlugin.items["not-mirrored"] = FakeItem(identifier="not-mirrored")
    FakePlugin.lookup_calls.clear()

    assert run_reconcile(config) == 0

    assert [identifier for identifier, _ in FakePlugin.lookup_calls] == ["a"]


def test_reconcile_keeps_a_fallback_shows_reason(tmp_roots: tuple[Path, Path]) -> None:
    """A show mirrored as its fallback copy stays listed in `status` with its reason
    after a rebuild (§spec:item-lifecycle: restricted shows appear with their reason)."""
    from shakedown.selection import LOSSLESS_REFUSED_REASON

    archive, library = tmp_roots
    config = make_config(archive, library, format_filters=["shn"], fallback_format_filters=["mp3"])
    FakePlugin.items["sbd"] = FakeItem(
        identifier="sbd",
        files=[
            FakeFile(name="t.shn", content=b"lossless", refused=True),
            FakeFile(name="t.mp3", content=b"mp3"),
        ],
    )
    assert run_sync(config) == 0
    config.state_db.unlink()  # type: ignore[union-attr]

    assert run_reconcile(config) == 0

    row = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", "sbd")  # type: ignore[arg-type]
    assert row is not None
    assert row.status == ItemStatus.COMPLETE
    assert row.restriction_reason == LOSSLESS_REFUSED_REASON


def test_reconcile_keeps_the_reason_of_a_show_whose_lookup_fails(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A show that keeps its prior status keeps everything it had, its reason too."""
    from shakedown.selection import LOSSLESS_REFUSED_REASON

    archive, library = tmp_roots
    config = make_config(archive, library, format_filters=["shn"], fallback_format_filters=["mp3"])
    FakePlugin.items["sbd"] = FakeItem(
        identifier="sbd",
        files=[
            FakeFile(name="t.shn", content=b"lossless", refused=True),
            FakeFile(name="t.mp3", content=b"mp3"),
        ],
        change_signal="v1",
    )
    assert run_sync(config) == 0
    FakePlugin.lookup_faults["sbd"] = [ConnectionResetError("connection reset")]

    assert run_reconcile(config) == 1

    row = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", "sbd")  # type: ignore[arg-type]
    assert row is not None
    assert row.status == ItemStatus.COMPLETE
    assert row.restriction_reason == LOSSLESS_REFUSED_REASON
