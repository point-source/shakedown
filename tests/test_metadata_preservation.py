"""Source metadata preservation: the metadata.json sidecar vertical slice.

Covers §spec:metadata-preservation end-to-end: the sidecar is written at fetch into the
archive (excluded from the recorded manifest), hardlinked into the library beside the
media, regenerated network-free on restage, and rewritten by `sync --refresh-metadata`
without re-downloading media.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from shakedown.db import connect
from shakedown.restage import run_restage
from shakedown.staging import METADATA_SIDECAR
from shakedown.state import ItemRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _add_item(identifier: str, metadata: dict) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=f"{identifier}.flac", content=f"audio-{identifier}".encode())],
        metadata=metadata,
    )


def _read_item(config, identifier: str):
    conn = connect(config.state_db)  # type: ignore[arg-type]
    try:
        return ItemRepo(conn).get("fake-src", "coll1", identifier)
    finally:
        conn.close()


# --- §road:sidecar-write ---------------------------------------------------------------


def test_sidecar_written_to_archive_and_absent_from_manifest(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-1", {"year": "1977", "venue": "Barton Hall"})

    assert run_sync(config) == 0

    sidecar = archive / "fake-src" / "coll1" / "gd-1" / METADATA_SIDECAR
    assert sidecar.is_file()
    written = json.loads(sidecar.read_text())
    assert written["year"] == "1977"
    assert written["venue"] == "Barton Hall"
    assert written["identifier"] == "gd-1"

    # The sidecar is context, not media: it never appears in the recorded manifest.
    item = _read_item(config, "gd-1")
    assert item is not None and item.recorded_manifest is not None
    assert METADATA_SIDECAR not in {mf.name for mf in item.recorded_manifest.files}


def test_sidecar_not_written_when_opt_out(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=False)
    _add_item("gd-2", {"year": "1977"})

    assert run_sync(config) == 0
    assert not (archive / "fake-src" / "coll1" / "gd-2" / METADATA_SIDECAR).exists()


def test_metadata_edit_does_not_trigger_media_refetch(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-3", {"notes": "original notes"})

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == {"gd-3": 1}

    # An upstream metadata-only edit must not flip the item to changed-upstream nor
    # trigger a media re-download — change detection is manifest-vs-manifest over media.
    FakePlugin.items["gd-3"].metadata = {"notes": "edited notes"}
    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == {"gd-3": 1}  # no re-download


def test_media_file_named_metadata_json_is_not_clobbered(
    tmp_roots: tuple[Path, Path],
) -> None:
    # A media file legitimately named metadata.json must win over the sidecar, or the
    # archive would silently disagree with its recorded manifest (§spec:sync-identity).
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    FakePlugin.items["gd-clash"] = FakeItem(
        identifier="gd-clash",
        files=[FakeFile(name=METADATA_SIDECAR, content=b"real audio payload")],
        metadata={"notes": "context"},
    )

    assert run_sync(config) == 0

    arc = archive / "fake-src" / "coll1" / "gd-clash" / METADATA_SIDECAR
    assert arc.read_bytes() == b"real audio payload"  # media bytes preserved, not JSON
    # And it stays a normal manifest-staged file hardlinked into the library.
    lib = library / "fake-src" / "coll1" / "gd-clash" / METADATA_SIDECAR
    assert lib.stat().st_ino == arc.stat().st_ino


# --- §road:sidecar-stage ---------------------------------------------------------------


def test_sidecar_hardlinked_into_library(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-4", {"year": "1980"})

    assert run_sync(config) == 0

    arc = archive / "fake-src" / "coll1" / "gd-4" / METADATA_SIDECAR
    lib = library / "fake-src" / "coll1" / "gd-4" / METADATA_SIDECAR
    assert arc.is_file() and lib.is_file()
    assert arc.stat().st_ino == lib.stat().st_ino  # same inode: hardlinked


def test_restage_regenerates_sidecar_network_free(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-5", {"year": "1972", "venue": "Academy of Music"})

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == {"gd-5": 1}

    # Wipe the library and restage: the sidecar reappears with no network traffic.
    shutil.rmtree(library / "fake-src")
    assert run_restage(config) == 0

    lib = library / "fake-src" / "coll1" / "gd-5" / METADATA_SIDECAR
    assert lib.is_file()
    assert json.loads(lib.read_text())["venue"] == "Academy of Music"
    assert FakePlugin.fetch_count == {"gd-5": 1}  # restage never fetches


def test_restage_regenerates_sidecar_from_db_when_archive_copy_absent(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-6", {"year": "1969"})

    assert run_sync(config) == 0

    # Simulate an item mirrored before the feature existed: archive sidecar gone, but the
    # DB still records source_metadata. restage rebuilds it from recorded state.
    (archive / "fake-src" / "coll1" / "gd-6" / METADATA_SIDECAR).unlink()
    shutil.rmtree(library / "fake-src")

    assert run_restage(config) == 0
    assert FakePlugin.fetch_count == {"gd-6": 1}
    # Regenerated into both the archive (self-describing) and the library (hardlink).
    arc = archive / "fake-src" / "coll1" / "gd-6" / METADATA_SIDECAR
    lib = library / "fake-src" / "coll1" / "gd-6" / METADATA_SIDECAR
    assert arc.is_file() and lib.is_file()
    assert arc.stat().st_ino == lib.stat().st_ino
    assert json.loads(lib.read_text())["year"] == "1969"


# --- §road:metadata-refresh ------------------------------------------------------------


def test_refresh_metadata_rewrites_sidecar_and_db_without_refetch(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=True)
    _add_item("gd-7", {"notes": "v1"})

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == {"gd-7": 1}

    # Upstream metadata changes; the operator explicitly refreshes.
    FakePlugin.items["gd-7"].metadata = {"notes": "v2", "lineage": "SBD"}
    assert run_sync(config, refresh_metadata=True) == 0

    # metadata.json and the DB source_metadata both update; no media re-download.
    assert FakePlugin.fetch_count == {"gd-7": 1}
    arc = archive / "fake-src" / "coll1" / "gd-7" / METADATA_SIDECAR
    lib = library / "fake-src" / "coll1" / "gd-7" / METADATA_SIDECAR
    assert json.loads(arc.read_text())["notes"] == "v2"
    assert json.loads(arc.read_text())["lineage"] == "SBD"
    assert arc.stat().st_ino == lib.stat().st_ino  # hardlink still intact

    item = _read_item(config, "gd-7")
    assert item is not None
    assert item.source_metadata["notes"] == "v2"
    assert item.source_metadata["lineage"] == "SBD"


def test_refresh_metadata_skips_opt_out_collections(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, preserve_source_metadata=False)
    _add_item("gd-8", {"notes": "v1"})

    assert run_sync(config) == 0
    FakePlugin.items["gd-8"].metadata = {"notes": "v2"}

    # A refresh over a non-opted collection is a no-op: no sidecar, no re-download.
    assert run_sync(config, refresh_metadata=True) == 0
    assert FakePlugin.fetch_count == {"gd-8": 1}
    assert not (archive / "fake-src" / "coll1" / "gd-8" / METADATA_SIDECAR).exists()
    item = _read_item(config, "gd-8")
    assert item is not None and item.source_metadata["notes"] == "v1"  # DB unchanged
