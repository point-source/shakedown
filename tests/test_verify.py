"""verify --deep [--reconform] tests. Maps to PRD §16.5."""
from __future__ import annotations

from pathlib import Path

from shakedown.db import connect
from shakedown.state import DriftRepo, ItemRepo
from shakedown.sync import run_sync
from shakedown.verify import _scan_drift, run_verify
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _setup_with_one_item(tmp_roots, content: bytes = b"original audio"):
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x",
        files=[FakeFile(name="gd-x.flac", content=content)],
    )
    assert run_sync(config) == 0
    return config, archive / "fake-src" / "coll1" / "gd-x" / "gd-x.flac"


def test_verify_shallow_passes_when_files_present(tmp_roots: tuple[Path, Path]) -> None:
    config, _ = _setup_with_one_item(tmp_roots)
    rc = run_verify(
        config, source_filter=None, collection_filter=None,
        deep=False, reconform=False, list_drift=False, assume_yes=True,
    )
    assert rc == 0


def test_verify_deep_detects_drift(tmp_roots: tuple[Path, Path]) -> None:
    """Mutating bytes (simulating a tag rewrite) shows up as drift under --deep."""
    config, archived = _setup_with_one_item(tmp_roots)
    archived.write_bytes(b"original audio + ID3v2 tags rewritten by Beets")

    conn = connect(config.state_db)
    items = ItemRepo(conn)
    item = items.get("fake-src", "coll1", "gd-x")
    assert item is not None
    drifts = _scan_drift(item)
    assert len(drifts) == 1
    assert drifts[0].file_name == "gd-x.flac"
    assert drifts[0].observed_md5 != drifts[0].expected_md5

    rc = run_verify(
        config, source_filter=None, collection_filter=None,
        deep=True, reconform=False, list_drift=True, assume_yes=True,
    )
    assert rc == 0  # drift is informational, not an error
    assert DriftRepo(conn).count("fake-src", "coll1") == 1


def test_verify_deep_reconform_restores_upstream_bytes(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §16.5: --reconform re-fetches drifted files."""
    config, archived = _setup_with_one_item(tmp_roots, content=b"upstream bytes")
    assert FakePlugin.fetch_count["gd-x"] == 1

    archived.write_bytes(b"locally mutated bytes")
    assert archived.read_bytes() != b"upstream bytes"

    rc = run_verify(
        config, source_filter=None, collection_filter=None,
        deep=True, reconform=True, list_drift=False, assume_yes=True,
    )
    assert rc == 0
    assert archived.read_bytes() == b"upstream bytes"
    assert FakePlugin.fetch_count["gd-x"] == 2  # one initial sync, one reconform
