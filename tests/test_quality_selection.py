"""End to end: the best available and best playable copy through `shakedown sync`
(§spec:quality-selection; §req:success-criteria #26).

The archive holds the best available copy, the best playable copy when it is a
different format, and every non-audio file; the library holds the best playable copy
and the non-audio files. An upgrade from the old format filters downloads only what
is missing, and no archive audio is deleted by a selection change."""
from __future__ import annotations

from pathlib import Path

from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.restage import run_restage
from shakedown.selection import LOSSLESS_REFUSED_REASON, NOT_PLAYABLE_SHN_REASON
from shakedown.state import ItemRepo
from shakedown.sync import refetch_item, run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

NOTES = FakeFile("info.txt", b"lineage and setlist", format="Text")
FFP = FakeFile("show.ffp", b"fingerprints", format="Flac FingerPrint")


def _flac(n: int = 1, *, refused: bool = False) -> FakeFile:
    return FakeFile(f"t0{n}.flac", b"F" * 400, format="Flac", refused=refused)


def _shn(n: int = 1) -> FakeFile:
    return FakeFile(f"t0{n}.shn", b"S" * 500, format="Shorten")


def _vbr(n: int = 1, original: str | None = None) -> FakeFile:
    return FakeFile(f"t0{n}_vbr.mp3", b"M" * 88, format="VBR MP3", derived_from=original)


def _ogg(n: int = 1, original: str | None = None) -> FakeFile:
    return FakeFile(f"t0{n}.ogg", b"O" * 43, format="Ogg Vorbis", derived_from=original)


def _show(identifier: str, *files: FakeFile) -> FakeItem:
    item = FakeItem(identifier=identifier, files=list(files), metadata={"title": identifier})
    FakePlugin.items[identifier] = item
    return item


def _names(root: Path, identifier: str) -> list[str]:
    folder = root / "fake-src" / "coll1" / identifier
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _item(config, identifier: str) -> Item:
    item = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", identifier)
    assert item is not None
    return item


def test_flac_show_archive_and_library_hold_the_flac_and_every_non_audio_file(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _show("gd-flac", _flac(), _vbr(original="t01.flac"), _ogg(original="t01.flac"), NOTES, FFP)

    assert run_sync(config) == 0

    expected = ["info.txt", "show.ffp", "t01.flac"]
    assert _names(archive, "gd-flac") == expected
    assert _names(library, "gd-flac") == expected


def test_shn_show_keeps_shn_archive_only_and_stages_the_vbr_mp3(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Shaped like gd77-05-08.sbd.hicks.4982.sbeok.shnf: SHN originals with VBR MP3
    and smaller Ogg Vorbis derivatives."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _show(
        "gd77-05-08",
        _shn(1), _shn(2),
        _vbr(1, "t01.shn"), _vbr(2, "t02.shn"),
        _ogg(1, "t01.shn"), _ogg(2, "t02.shn"),
        NOTES,
    )

    assert run_sync(config) == 0

    assert _names(archive, "gd77-05-08") == [
        "info.txt", "t01.shn", "t01_vbr.mp3", "t02.shn", "t02_vbr.mp3"
    ]
    assert _names(library, "gd77-05-08") == ["info.txt", "t01_vbr.mp3", "t02_vbr.mp3"]
    item = _item(config, "gd77-05-08")
    assert item.status == ItemStatus.COMPLETE
    assert item.not_playable_reason is None


def test_shn_show_without_a_playable_copy_is_staged_as_is_and_not_playable(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _show("gd-shn-only", _shn(), NOTES)

    assert run_sync(config) == 0

    assert _names(archive, "gd-shn-only") == ["info.txt", "t01.shn"]
    assert _names(library, "gd-shn-only") == ["info.txt", "t01.shn"]
    item = _item(config, "gd-shn-only")
    assert item.status == ItemStatus.COMPLETE
    assert item.not_playable_reason == NOT_PLAYABLE_SHN_REASON
    assert item.restriction_reason is None


def test_lossless_served_again_is_fetched_alone_and_the_mp3_stays_archive_only(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _show("gd-sbd", _flac(refused=True), _vbr(original="t01.flac"), NOTES)
    assert run_sync(config) == 0
    assert _item(config, "gd-sbd").restriction_reason == LOSSLESS_REFUSED_REASON
    mp3 = archive / "fake-src" / "coll1" / "gd-sbd" / "t01_vbr.mp3"
    mp3_inode = mp3.stat().st_ino

    show.files[0] = _flac()
    FakePlugin.fetched_files.clear()
    assert run_sync(config) == 0

    assert FakePlugin.fetched_files["gd-sbd"] == ["t01.flac"]
    assert _names(archive, "gd-sbd") == ["info.txt", "t01.flac", "t01_vbr.mp3"]
    assert mp3.stat().st_ino == mp3_inode  # kept, never re-downloaded
    assert _names(library, "gd-sbd") == ["info.txt", "t01.flac"]
    item = _item(config, "gd-sbd")
    assert item.restriction_reason is None
    assert item.recorded_manifest is not None
    assert {f.name: f.library for f in item.recorded_manifest.files} == {
        "info.txt": True, "t01.flac": True, "t01_vbr.mp3": False,
    }

    FakePlugin.fetched_files.clear()
    assert run_sync(config) == 0
    assert FakePlugin.fetched_files == {}  # idempotent


def _seed_mirrored(config, identifier: str, files: list[FakeFile]) -> dict[str, int]:
    """Archive copy and record of a show mirrored before the quality rule (e.g. under
    `format_filters: [flac, shn]`): no library flags. Returns each file's inode."""
    folder = config.archive_root / "fake-src" / "coll1" / identifier
    folder.mkdir(parents=True)
    for f in files:
        (folder / f.name).write_bytes(f.content)
    ItemRepo(connect(config.state_db)).upsert(
        Item(
            source_name="fake-src",
            collection_name="coll1",
            identifier=identifier,
            status=ItemStatus.COMPLETE,
            archive_path=folder,
            recorded_manifest=Manifest(
                files=tuple(ManifestFile(f.name, f.size, f.md5) for f in files)
            ),
        )
    )
    return {f.name: (folder / f.name).stat().st_ino for f in files}


def test_upgrade_from_format_filters_downloads_only_what_is_missing(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    flac_inodes = _seed_mirrored(config, "gd-flac", [_flac()])
    shn_inodes = _seed_mirrored(config, "gd-shn", [_shn()])
    both_inodes = _seed_mirrored(config, "gd-both", [_flac(), _shn()])
    assert run_restage(config) == 0
    assert _names(library, "gd-both") == ["t01.flac", "t01.shn"]

    _show("gd-flac", _flac(), _vbr(original="t01.flac"), NOTES)
    _show("gd-shn", _shn(), _vbr(original="t01.shn"), _ogg(original="t01.shn"), NOTES)
    _show("gd-both", _flac(), _shn(), _vbr(original="t01.flac"), NOTES)

    assert run_sync(config) == 0

    # Only the added non-audio files and playable copies are downloaded.
    assert FakePlugin.fetched_files == {
        "gd-flac": ["info.txt"],
        "gd-shn": ["info.txt", "t01_vbr.mp3"],
        "gd-both": ["info.txt"],
    }
    # The files already mirrored are the same files: kept, not re-downloaded.
    for identifier, inodes in (
        ("gd-flac", flac_inodes), ("gd-shn", shn_inodes), ("gd-both", both_inodes)
    ):
        folder = archive / "fake-src" / "coll1" / identifier
        assert {n: (folder / n).stat().st_ino for n in inodes} == inodes, identifier

    # SHN stays archive-only; the SHN the rule no longer selects stays in the archive.
    assert _names(archive, "gd-shn") == ["info.txt", "t01.shn", "t01_vbr.mp3"]
    assert _names(library, "gd-shn") == ["info.txt", "t01_vbr.mp3"]
    assert _names(archive, "gd-both") == ["info.txt", "t01.flac", "t01.shn"]
    assert _names(library, "gd-both") == ["info.txt", "t01.flac"]
    assert _names(library, "gd-flac") == ["info.txt", "t01.flac"]

    FakePlugin.fetched_files.clear()
    assert run_sync(config) == 0
    assert FakePlugin.fetched_files == {}  # idempotent
    assert _names(archive, "gd-both") == ["info.txt", "t01.flac", "t01.shn"]


def test_reselection_without_downloads_updates_the_library(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A show mirrored with everything (no filters) already holds the files the rule
    selects: nothing is downloaded, and the copies it no longer selects leave the
    library but stay in the archive."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_mirrored(config, "gd-all", [_flac(), _vbr(original="t01.flac"), NOTES])
    assert run_restage(config) == 0
    _show("gd-all", _flac(), _vbr(original="t01.flac"), NOTES)

    assert run_sync(config) == 0

    assert FakePlugin.fetched_files == {}
    assert _names(archive, "gd-all") == ["info.txt", "t01.flac", "t01_vbr.mp3"]
    assert _names(library, "gd-all") == ["info.txt", "t01.flac"]


def test_refetch_keeps_archive_audio_the_rule_no_longer_selects(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_mirrored(config, "gd-both", [_flac(), _shn()])
    _show("gd-both", _flac(), _shn(), NOTES)
    assert run_sync(config) == 0

    assert refetch_item(config, "gd-both") == 0

    assert _names(archive, "gd-both") == ["info.txt", "t01.flac", "t01.shn"]
    assert _names(library, "gd-both") == ["info.txt", "t01.flac"]


def test_a_retained_file_deleted_from_the_archive_is_dropped_not_refetched(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_mirrored(config, "gd-both", [_flac(), _shn()])
    _show("gd-both", _flac(), _shn(), NOTES)
    assert run_sync(config) == 0
    (archive / "fake-src" / "coll1" / "gd-both" / "t01.shn").unlink()

    FakePlugin.fetched_files.clear()
    assert run_sync(config) == 0

    assert FakePlugin.fetched_files == {}
    manifest = _item(config, "gd-both").recorded_manifest
    assert manifest is not None
    assert [f.name for f in manifest.files] == ["info.txt", "t01.flac"]


def test_reconcile_records_archive_files_the_rule_no_longer_selects(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A rebuilt record keeps the archive-only files on disk, so a later download for
    the show still keeps them (§spec:state, §spec:quality-selection)."""
    from shakedown.reconcile import run_reconcile

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_mirrored(config, "gd-both", [_flac(), _shn()])
    show = _show("gd-both", _flac(), _shn(), NOTES)
    _show("gd-shn-only", _shn())
    assert run_sync(config) == 0
    config.state_db.unlink()

    assert run_reconcile(config) == 0

    manifest = _item(config, "gd-both").recorded_manifest
    assert manifest is not None
    assert {f.name: f.library for f in manifest.files} == {
        "info.txt": True, "t01.flac": True, "t01.shn": False,
    }
    assert _item(config, "gd-shn-only").not_playable_reason == NOT_PLAYABLE_SHN_REASON

    show.files.append(FakeFile("setlist.txt", b"setlist", format="Text"))
    FakePlugin.fetched_files.clear()
    assert run_sync(config) == 0
    assert FakePlugin.fetched_files == {"gd-both": ["setlist.txt"]}
    assert _names(archive, "gd-both") == ["info.txt", "setlist.txt", "t01.flac", "t01.shn"]
    assert _names(library, "gd-both") == ["info.txt", "setlist.txt", "t01.flac"]
