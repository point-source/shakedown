"""End to end: the best available and best playable copy through `shakedown sync`
(§spec:quality-selection; §req:success-criteria #26).

The archive holds the best available copy, the best playable copy when it is a
different format, and every non-audio file; the library holds the best playable copy
and the uploader's own non-audio files. An upgrade from the old format filters downloads only what
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


def test_show_whose_checksum_files_name_every_track_syncs_complete(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Shaped like ARU1994-03-18: archive.org lists the .ffp and .md5 it generated as
    derived from every FLAC track. The show syncs, and both are kept in the archive;
    being archive.org's, not the uploader's, they stay out of the library (operator
    decision, 2026-11-11)."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    tracks = ["t01.flac", "t02.flac"]
    _show(
        "aru-1994",
        _flac(1), _flac(2), _vbr(1, original="t01.flac"), _vbr(2, original="t02.flac"),
        FakeFile("show.ffp", b"fingerprints", format="Flac FingerPrint", derived_from=tracks),
        FakeFile("show.md5", b"checksums", format="Checksums", derived_from=tracks),
    )

    assert run_sync(config) == 0

    assert _names(archive, "aru-1994") == ["show.ffp", "show.md5", *tracks]
    assert _names(library, "aru-1994") == tracks
    assert _item(config, "aru-1994").status == ItemStatus.COMPLETE


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


def _seed_mirrored(
    config, identifier: str, files: list[FakeFile], *, archive_only: frozenset[str] = frozenset()
) -> dict[str, int]:
    """Archive copy and record of a show mirrored before the quality rule (e.g. under
    `format_filters: [flac, shn]`): no library flags, unless `archive_only` names the
    files an earlier rule left out of the library. Returns each file's inode."""
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
                files=tuple(
                    ManifestFile(f.name, f.size, f.md5, library=f.name not in archive_only)
                    for f in files
                )
            ),
        )
    )
    return {f.name: (folder / f.name).stat().st_ino for f in files}


def test_a_show_v2_0_1_staged_as_mp3_gets_its_flac_back_without_downloads(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Seen on the NAS (aru2015-08-15.24Bit48khzMS, 2026-11-11): archive.org labels one
    FLAC track "Flac" and the rest "24bit Flac", and v2.0.1 staged the VBR MP3 and a
    spectrogram. The next sync stages the archived FLACs and the notes, downloads
    nothing, and takes the MP3 and the spectrogram out of the library; the archive
    keeps every file."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    t01 = FakeFile("t01.flac", b"F" * 600, format="24bit Flac")
    t02 = FakeFile("t02.flac", b"G" * 300, format="Flac")
    t03 = FakeFile("t03.flac", b"H" * 600, format="24bit Flac")
    flacs = [t01, t02, t03]
    mp3s = [_vbr(n, original=f.name) for n, f in enumerate(flacs, start=1)]
    spectrogram = FakeFile(
        "t01_spectrogram.png", b"P", format="Spectrogram", derived_from="t01.flac"
    )
    everything = [*flacs, *mp3s, spectrogram, NOTES]
    inodes = _seed_mirrored(
        config, "aru", everything, archive_only=frozenset(f.name for f in flacs)
    )
    assert run_restage(config) == 0
    assert _names(library, "aru") == [
        "info.txt", "t01_spectrogram.png", "t01_vbr.mp3", "t02_vbr.mp3", "t03_vbr.mp3"
    ]
    _show("aru", *everything)

    assert run_sync(config) == 0

    assert FakePlugin.fetched_files == {}
    assert _names(library, "aru") == ["info.txt", "t01.flac", "t02.flac", "t03.flac"]
    folder = archive / "fake-src" / "coll1" / "aru"
    assert {n: (folder / n).stat().st_ino for n in inodes} == inodes
    staged = library / "fake-src" / "coll1" / "aru"
    assert all((staged / n).stat().st_ino == inodes[n] for n in _names(library, "aru"))


def test_a_zip_v2_0_2_staged_leaves_the_library_and_stays_archived(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Seen on the NAS (aadam-jacobs, 2026-11-11): v2.0.2 staged the uploader's raw
    recording zip beside the FLACs. The next sync takes it out of the library,
    downloads nothing, and the archive keeps the same file."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    flacs = [_flac(1), _flac(2)]
    rawfile = FakeFile("show_16bit48khz_rawfile.zip", b"Z" * 900, format="ZIP")
    everything = [*flacs, rawfile, NOTES]
    inodes = _seed_mirrored(config, "ajc", everything)
    assert run_restage(config) == 0
    assert _names(library, "ajc") == [
        "info.txt", "show_16bit48khz_rawfile.zip", "t01.flac", "t02.flac"
    ]
    _show("ajc", *everything)

    assert run_sync(config) == 0

    assert FakePlugin.fetched_files == {}
    assert _names(library, "ajc") == ["info.txt", "t01.flac", "t02.flac"]
    folder = archive / "fake-src" / "coll1" / "ajc"
    assert {n: (folder / n).stat().st_ino for n in inodes} == inodes
    staged = library / "fake-src" / "coll1" / "ajc"
    assert all((staged / n).stat().st_ino == inodes[n] for n in _names(library, "ajc"))


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
    assert config.state_db is not None
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


def test_a_listing_without_audio_keeps_the_copy_and_says_why(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A darked or glitched listing that shows only archive.org's own files is never
    read as the show changing: the copy is kept and the show is unavailable with the
    reason (operator-approved copy, Q-6-3)."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _show("gd-dark", _flac(), NOTES)
    assert run_sync(config) == 0

    show.files = [FakeFile("gd-dark_meta.xml", b"<meta/>", format="Metadata")]
    assert run_sync(config) == 0

    item = _item(config, "gd-dark")
    assert item.status == ItemStatus.UNAVAILABLE
    assert item.restriction_reason == "source lists no audio files"
    assert _names(archive, "gd-dark") == ["info.txt", "t01.flac"]
    assert _names(library, "gd-dark") == ["info.txt", "t01.flac"]


def _status(config, *, as_json: bool) -> str:
    import io
    from contextlib import redirect_stdout

    from shakedown.status import print_status

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_status(config, as_json=as_json)
    return buf.getvalue()


def test_status_lists_shn_only_shows_under_not_playable(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Its own heading, not under `restricted`; lossless-refused shows stay in the
    `restricted` list with their reason (operator-approved copy, 2026-11-10)."""
    import json

    archive, library = tmp_roots
    config = make_config(archive, library)
    _show("gd-shn-a", _shn())
    _show("gd-shn-b", _shn(), NOTES)
    _show("gd-sbd", _flac(refused=True), _vbr(original="t01.flac"))
    _show("gd-flac", _flac())
    assert run_sync(config) == 0

    text = _status(config, as_json=False)
    assert (
        "  restricted (1):\n"
        f"    - gd-sbd: {LOSSLESS_REFUSED_REASON}\n"
        "  not playable (2):\n"
        "    - gd-shn-a: only in SHN; archive.org offers no playable copy\n"
        "    - gd-shn-b: only in SHN; archive.org offers no playable copy\n"
    ) in text

    (summary,) = json.loads(_status(config, as_json=True))
    assert summary["not_playable"] == [
        {"identifier": "gd-shn-a", "reason": NOT_PLAYABLE_SHN_REASON},
        {"identifier": "gd-shn-b", "reason": NOT_PLAYABLE_SHN_REASON},
    ]
    assert summary["restricted"] == [{"identifier": "gd-sbd", "reason": LOSSLESS_REFUSED_REASON}]
    assert summary["counts"]["complete"] == 4


def test_status_caps_the_not_playable_list(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    for n in range(12):
        _show(f"gd-shn-{n:02d}", _shn())
    assert run_sync(config) == 0

    text = _status(config, as_json=False)
    assert "  not playable (12):\n" in text
    assert "    - gd-shn-09: " in text
    assert "    - gd-shn-10: " not in text
    assert "    ... and 2 more\n" in text


def test_status_is_quiet_when_every_show_plays(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _show("gd-flac", _flac())
    assert run_sync(config) == 0
    assert "not playable" not in _status(config, as_json=False)


def test_an_unchanged_show_regains_its_change_signal_after_the_upgrade(
    tmp_roots: tuple[Path, Path],
) -> None:
    """The upgrade clears every change signal so each show is looked up once; an
    unchanged show must store the signal again, or an incremental collection would
    look it up on every run (§spec:incremental-discovery)."""
    archive, library = tmp_roots
    config = make_config(archive, library, incremental_discovery=True)
    show = _show("gd-flac", _flac(), NOTES)
    show.change_signal = "v1"
    assert run_sync(config) == 0
    assert config.state_db is not None
    connect(config.state_db).execute("UPDATE items SET change_signal = NULL").connection.commit()

    assert run_sync(config) == 0  # the one lookup after the upgrade
    looked_up = FakePlugin.lookups("gd-flac")
    assert run_sync(config) == 0

    assert FakePlugin.lookups("gd-flac") == looked_up
    assert _item(config, "gd-flac").change_signal == "v1"


def test_reconcile_keeps_a_refused_shows_copy_and_library(tmp_roots: tuple[Path, Path]) -> None:
    """A show whose audio the source now refuses is rebuilt `unavailable` with its
    recorded files, like sync records it — never `complete` with nothing to stage
    (§spec:failure-behavior: the local copy is retained)."""
    from shakedown.reconcile import run_reconcile
    from shakedown.selection import DOWNLOAD_REFUSED_REASON

    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _show("gd-dark", _flac(), NOTES)
    assert run_sync(config) == 0
    show.files = [_flac(refused=True), NOTES]

    assert run_reconcile(config) == 0

    item = _item(config, "gd-dark")
    assert item.status == ItemStatus.UNAVAILABLE
    assert item.restriction_reason == DOWNLOAD_REFUSED_REASON
    assert item.recorded_manifest is not None
    assert [f.name for f in item.recorded_manifest.files if f.library] == ["info.txt", "t01.flac"]
    assert run_restage(config) == 0
    assert _names(library, "gd-dark") == ["info.txt", "t01.flac"]


def test_reconcile_without_a_lookup_records_the_files_on_disk(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A show with no record whose lookup fails is rebuilt from what is on disk, so
    the next sync keeps the copies the rule no longer selects."""
    from shakedown.reconcile import run_reconcile

    archive, library = tmp_roots
    config = make_config(archive, library)
    folder = archive / "fake-src" / "coll1" / "gd-both"
    folder.mkdir(parents=True)
    for f in (_flac(), _shn()):
        (folder / f.name).write_bytes(f.content)
    _show("gd-both", _flac(), _shn(), NOTES)
    FakePlugin.lookup_faults["gd-both"] = [ConnectionResetError("connection reset")]

    assert run_reconcile(config) == 1
    del FakePlugin.lookup_faults["gd-both"]
    assert run_sync(config) == 0

    assert _names(archive, "gd-both") == ["info.txt", "t01.flac", "t01.shn"]
    assert _names(library, "gd-both") == ["info.txt", "t01.flac"]


# --- reconform repairs drifted files, never the whole show (§spec:verify-drift) ---


def _reconform(config) -> int:
    from shakedown.verify import run_verify

    return run_verify(
        config, source_filter=None, collection_filter=None,
        deep=True, reconform=True, list_drift=False, assume_yes=True,
    )


def _mirror_flac_and_shn(config, identifier: str = "gd-both") -> FakeItem:
    _seed_mirrored(config, identifier, [_flac(), _shn()])
    show = _show(identifier, _flac(), _shn(), NOTES)
    assert run_sync(config) == 0
    FakePlugin.fetched_files.clear()
    return show


def _last_run(config) -> dict:
    from shakedown.state import DriftRepo, OperationOutcomeRepo, RunRepo
    from shakedown.status import _collection_summary

    conn = connect(config.state_db)
    return _collection_summary(
        config, "fake-src", "coll1", ItemRepo(conn), RunRepo(conn), DriftRepo(conn),
        OperationOutcomeRepo(conn),
    )["last_run"]


def test_reconform_refetches_a_drifted_archive_only_copy_and_nothing_else(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _mirror_flac_and_shn(config)
    folder = archive / "fake-src" / "coll1" / "gd-both"
    flac_inode = (folder / "t01.flac").stat().st_ino
    (folder / "t01.shn").write_bytes(b"retagged")

    assert _reconform(config) == 0

    assert FakePlugin.fetched_files == {"gd-both": ["t01.shn"]}
    assert (folder / "t01.shn").read_bytes() == _shn().content
    assert (folder / "t01.flac").stat().st_ino == flac_inode, "the FLAC was not downloaded"
    assert _names(archive, "gd-both") == ["info.txt", "t01.flac", "t01.shn"]
    assert _names(library, "gd-both") == ["info.txt", "t01.flac"]


def test_reconform_refetches_only_the_drifted_picked_file(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _mirror_flac_and_shn(config)
    folder = archive / "fake-src" / "coll1" / "gd-both"
    (folder / "t01.flac").write_bytes(b"retagged")

    assert _reconform(config) == 0

    assert FakePlugin.fetched_files == {"gd-both": ["t01.flac"]}
    assert (folder / "t01.flac").read_bytes() == _flac().content
    staged = library / "fake-src" / "coll1" / "gd-both" / "t01.flac"
    assert staged.stat().st_ino == (folder / "t01.flac").stat().st_ino


def test_reconform_reports_a_drifted_file_the_source_no_longer_lists(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Not repairable: listed in the run's item issues, and nothing else downloads."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _mirror_flac_and_shn(config)
    show.files = [_flac(), NOTES]  # the source dropped the SHN
    folder = archive / "fake-src" / "coll1" / "gd-both"
    (folder / "t01.shn").write_bytes(b"retagged")

    assert _reconform(config) == 1

    assert FakePlugin.fetched_files == {}
    assert (folder / "t01.shn").read_bytes() == b"retagged"
    last = _last_run(config)
    assert (last["kind"], last["items_failed"]) == ("reconform", 1)
    assert "gd-both: t01.shn drifted and the source no longer serves it; not repaired" in last["errors"]


def test_reconform_repairs_what_the_source_lists_beside_a_lost_file(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A drifted FLAC is repaired while a deleted SHN the source no longer lists is
    reported; the missing SHN does not stop the FLAC's repair."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _mirror_flac_and_shn(config)
    show.files = [_flac(), NOTES]
    folder = archive / "fake-src" / "coll1" / "gd-both"
    (folder / "t01.flac").write_bytes(b"retagged")
    (folder / "t01.shn").unlink()

    assert _reconform(config) == 1

    assert FakePlugin.fetched_files == {"gd-both": ["t01.flac"]}
    assert (folder / "t01.flac").read_bytes() == _flac().content
    last = _last_run(config)
    assert (last["items_updated"], last["items_failed"]) == (1, 1)
    assert "gd-both: t01.shn drifted and the source no longer serves it; not repaired" in last["errors"]


def test_reconform_does_not_download_a_drifted_file_the_source_refuses(
    tmp_roots: tuple[Path, Path],
) -> None:
    """archive.org made the FLAC private after it was mirrored: the drift cannot be
    repaired, nothing downloads, and the show keeps its complete archive copy."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _show("gd-flac", _flac(), _vbr(original="t01.flac"))
    assert run_sync(config) == 0
    show.files = [_flac(refused=True), _vbr(original="t01.flac")]
    folder = archive / "fake-src" / "coll1" / "gd-flac"
    (folder / "t01.flac").write_bytes(b"retagged")
    FakePlugin.fetched_files.clear()

    assert _reconform(config) == 1

    assert FakePlugin.fetched_files == {}
    assert (folder / "t01.flac").read_bytes() == b"retagged"
    assert _item(config, "gd-flac").status == ItemStatus.COMPLETE
    last = _last_run(config)
    assert last["items_failed"] == 1
    assert "gd-flac: t01.flac drifted and the source no longer serves it; not repaired" in last["errors"]


def test_reconform_drops_a_lost_file_gone_from_disk_from_the_record(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A deleted SHN the source no longer lists is reported once and leaves the record,
    as any archive-only file gone from disk does, so later checks stop reporting it."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _mirror_flac_and_shn(config)
    show.files = [_flac(), NOTES]
    (archive / "fake-src" / "coll1" / "gd-both" / "t01.shn").unlink()

    assert _reconform(config) == 1

    manifest = _item(config, "gd-both").recorded_manifest
    assert manifest is not None
    assert [f.name for f in manifest.files] == ["info.txt", "t01.flac"]
    assert _reconform(config) == 0


def test_reconform_counts_a_show_failed_once(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A show with a lost file whose other repair also fails is one failed show."""
    from shakedown.plugins.base import FetchResult

    archive, library = tmp_roots
    config = make_config(archive, library)
    show = _mirror_flac_and_shn(config)
    show.files = [_flac(), NOTES]
    folder = archive / "fake-src" / "coll1" / "gd-both"
    (folder / "t01.flac").write_bytes(b"retagged")
    (folder / "t01.shn").write_bytes(b"retagged")
    monkeypatch.setattr(
        FakePlugin, "fetch",
        lambda self, identifier, files, dest_dir: FetchResult(
            success=False, error="connection reset"
        ),
    )

    assert _reconform(config) == 1

    assert _last_run(config)["items_failed"] == 1
