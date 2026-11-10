"""Opt-in tag filling (§spec:tag-filling, §req:success-criteria #25)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shakedown.config import CollectionConfig
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.state import ItemRepo, TagRecord, TagRepo
from shakedown.sync import _record_disappeared


def test_pruning_a_show_drops_its_tag_records(tmp_path: Path) -> None:
    """A pruned show's archive copy is gone, and with it the bytes the records
    describe."""
    conn = connect(tmp_path / "s.db")
    items, tags = ItemRepo(conn), TagRepo(conn)
    folder = tmp_path / "archive" / "x"
    folder.mkdir(parents=True)
    (folder / "a.flac").write_bytes(b"x")
    item = Item(
        source_name="s", collection_name="c", identifier="x", status=ItemStatus.COMPLETE,
        archive_path=folder, downloaded_at=datetime.now(),
        recorded_manifest=Manifest(files=(ManifestFile("a.flac", 1, None),)),
    )
    items.upsert(item)
    tags.put_file("s", "c", "x", "a.flac", TagRecord("md5", {"album": "A"}))
    tags.set_fingerprint("s", "c", "x", "fp")

    _record_disappeared(items, "s", CollectionConfig(name="c", query="q", prune_disappeared=True), item)

    assert tags.file_records("s", "c", "x") == {}
    assert tags.fingerprints("s", "c") == {}
