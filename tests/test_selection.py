"""Core-owned file selection: the quality rule (§spec:quality-selection), applied to
every source's raw listing (§spec:per-item-isolation).

The best available copy and the best playable copy are mirrored (one copy when they
are the same format), plus every non-audio file; the library stages the best
playable copy and the uploader's own non-audio files."""
from __future__ import annotations

import json
from pathlib import Path

from shakedown.discovery import validate_lookup
from shakedown.models import Manifest, ManifestFile, SourceFile
from shakedown.plugins.archive_org import file_records
from shakedown.plugins.base import ItemLookup
from shakedown.selection import (
    DOWNLOAD_REFUSED_REASON,
    LOSSLESS_REFUSED_REASON,
    NOT_PLAYABLE_SHN_REASON,
    NOT_PLAYABLE_UNRECOGNISED_REASON,
    select,
)

MB = 1_000_000


def _f(
    name: str,
    fmt: str | None = None,
    *,
    size: int | None = 1,
    refused: bool = False,
    original: str | None = None,
    bitrate: int | None = None,
) -> SourceFile:
    return SourceFile(
        name=name,
        size=size,
        md5=None,
        format=fmt,
        refused=refused,
        derived_from=(original,) if original else (),
        bitrate=bitrate,
    )


NON_AUDIO = (
    _f("info.txt", "Text"),
    _f("show.md5", "Checksums"),
    _f("show.ffp", "Flac FingerPrint"),
    _f("show_meta.xml", "Metadata"),
    _f("cover.jpg", "JPEG"),
    _f("t01_spectrogram.png", "Spectrogram", original="t01.flac"),
)
NON_AUDIO_NAMES = sorted(f.name for f in NON_AUDIO)
# The uploader's own non-audio files: the library stages these, not what archive.org
# generates or keeps for itself (operator decision, 2026-11-11).
STAGED_NON_AUDIO = ["cover.jpg", "info.txt", "show.ffp", "show.md5"]
ARCHIVE_ONLY_NON_AUDIO = ["show_meta.xml", "t01_spectrogram.png"]


def _gd77_shn_show(*, derivatives: bool = True) -> tuple[SourceFile, ...]:
    """Shaped like archive.org's gd77-05-08.sbd.hicks.4982.sbeok.shnf: SHN originals,
    VBR MP3 at ~8.8 MB per track and Ogg Vorbis at ~4.3 MB per track."""
    files = [_f(f"gd77-05-08d1t0{n}.shn", "Shorten", size=30 * MB) for n in (1, 2, 3)]
    if derivatives:
        for n in (1, 2, 3):
            original = f"gd77-05-08d1t0{n}.shn"
            files.append(_f(f"gd77-05-08d1t0{n}_vbr.mp3", "VBR MP3", size=int(8.8 * MB), original=original))
            files.append(_f(f"gd77-05-08d1t0{n}.ogg", "Ogg Vorbis", size=int(4.3 * MB), original=original))
    return (*files, *NON_AUDIO)


def _flac_show() -> tuple[SourceFile, ...]:
    files = []
    for n in (1, 2):
        files.append(_f(f"t0{n}.flac", "Flac", size=40 * MB))
        files.append(_f(f"t0{n}.mp3", "VBR MP3", size=9 * MB, original=f"t0{n}.flac"))
        files.append(_f(f"t0{n}_64kb.mp3", "64Kbps MP3", size=3 * MB, original=f"t0{n}.flac"))
        files.append(_f(f"t0{n}.ogg", "Ogg Vorbis", size=4 * MB, original=f"t0{n}.flac"))
    return (*files, *NON_AUDIO)


def _names(selection, *, library: bool | None = None) -> list[str]:
    return [
        f.name
        for f in selection.manifest.files
        if library is None or f.library is library
    ]


def test_flac_show_keeps_only_the_flac_and_every_non_audio_file() -> None:
    chosen = select(_flac_show(), None)
    assert _names(chosen) == sorted(["t01.flac", "t02.flac", *NON_AUDIO_NAMES])
    assert _names(chosen, library=False) == ARCHIVE_ONLY_NON_AUDIO
    assert chosen.restriction_reason is None
    assert chosen.not_playable_reason is None
    assert chosen.is_restricted is False


def test_shn_show_keeps_shn_archive_only_and_stages_the_vbr_mp3() -> None:
    chosen = select(_gd77_shn_show(), None)
    shn = [f"gd77-05-08d1t0{n}.shn" for n in (1, 2, 3)]
    mp3 = [f"gd77-05-08d1t0{n}_vbr.mp3" for n in (1, 2, 3)]
    assert _names(chosen, library=False) == sorted([*shn, *ARCHIVE_ONLY_NON_AUDIO])
    assert _names(chosen, library=True) == sorted([*mp3, *STAGED_NON_AUDIO])
    assert chosen.restriction_reason is None
    assert chosen.not_playable_reason is None


def test_shn_show_without_a_playable_copy_is_staged_as_is_and_not_playable() -> None:
    chosen = select(_gd77_shn_show(derivatives=False), None)
    assert _names(chosen, library=False) == ARCHIVE_ONLY_NON_AUDIO
    assert [n for n in _names(chosen) if n.endswith(".shn")] == [
        f"gd77-05-08d1t0{n}.shn" for n in (1, 2, 3)
    ]
    assert chosen.not_playable_reason == NOT_PLAYABLE_SHN_REASON
    assert chosen.is_restricted is False
    assert chosen.restriction_reason is None


def test_refused_lossless_mirrors_the_best_lossy_copy_with_the_reason() -> None:
    files = (
        _f("t01.flac", "Flac", refused=True),
        _f("t02.flac", "Flac", refused=True),
        _f("t01_64kb.mp3", "64Kbps MP3", size=3 * MB),
        _f("t02_64kb.mp3", "64Kbps MP3", size=3 * MB),
        _f("t01.mp3", "VBR MP3", size=9 * MB),
        _f("t02.mp3", "VBR MP3", size=9 * MB),
        _f("info.txt", "Text"),
    )
    chosen = select(files, None)
    assert _names(chosen) == ["info.txt", "t01.mp3", "t02.mp3"]
    assert _names(chosen, library=False) == []
    assert chosen.is_restricted is False
    assert chosen.restriction_reason == LOSSLESS_REFUSED_REASON


def test_lossless_served_again_is_mirrored_without_the_lossy_copy() -> None:
    files = (
        _f("t01.flac", "Flac"),
        _f("t01.mp3", "VBR MP3", size=9 * MB),
        _f("info.txt", "Text"),
    )
    chosen = select(files, None)
    assert _names(chosen) == ["info.txt", "t01.flac"]
    assert chosen.restriction_reason is None


def test_lossy_copies_rank_by_listed_bitrate_over_size() -> None:
    files = (
        _f("t01_a.mp3", "MP3 A", size=9 * MB, bitrate=128),
        _f("t01_b.mp3", "MP3 B", size=3 * MB, bitrate=320),
    )
    assert _names(select(files, None)) == ["t01_b.mp3"]


def test_lossy_copies_rank_by_size_per_track_when_a_bitrate_is_missing() -> None:
    files = (
        _f("t01_a.mp3", "MP3 A", size=9 * MB, bitrate=128),
        _f("t01_b.mp3", "MP3 B", size=3 * MB),
    )
    assert _names(select(files, None)) == ["t01_a.mp3"]


def test_a_format_missing_a_track_does_not_count() -> None:
    files = (
        _f("t01.shn", "Shorten"),
        _f("t02.shn", "Shorten"),
        _f("t01.mp3", "VBR MP3", size=9 * MB, original="t01.shn"),
        _f("t01.ogg", "Ogg Vorbis", size=4 * MB, original="t01.shn"),
        _f("t02.ogg", "Ogg Vorbis", size=4 * MB, original="t02.shn"),
    )
    chosen = select(files, None)
    assert _names(chosen, library=True) == ["t01.ogg", "t02.ogg"]
    assert _names(chosen, library=False) == ["t01.shn", "t02.shn"]


def test_a_partly_refused_format_does_not_count() -> None:
    files = (
        _f("t01.flac", "Flac"),
        _f("t02.flac", "Flac", refused=True),
        _f("t01.mp3", "VBR MP3"),
        _f("t02.mp3", "VBR MP3"),
    )
    chosen = select(files, None)
    assert _names(chosen) == ["t01.mp3", "t02.mp3"]
    assert chosen.restriction_reason == LOSSLESS_REFUSED_REASON


def test_flac_wins_over_shn_when_both_cover_every_track() -> None:
    files = (
        _f("t01.shn", "Shorten", size=30 * MB),
        _f("t01.flac", "Flac", size=25 * MB),
        _f("t01.mp3", "VBR MP3", size=9 * MB),
    )
    chosen = select(files, None)
    assert _names(chosen) == ["t01.flac"]


def test_unrecognised_audio_ranks_below_recognised_audio() -> None:
    files = (_f("t01.ape", "Monkey's Audio", size=50 * MB), _f("t01.mp3", "VBR MP3"))
    assert _names(select(files, None)) == ["t01.mp3"]


def test_apple_lossless_is_lossless_and_aac_in_m4a_is_lossy() -> None:
    alac = (_f("t01.m4a", "Apple Lossless Audio", size=1), _f("t01.mp3", "VBR MP3", size=9))
    assert _names(select(alac, None)) == ["t01.m4a"]
    aac = (_f("t01.m4a", "AAC", size=1), _f("t01.mp3", "VBR MP3", size=9))
    assert _names(select(aac, None)) == ["t01.mp3"]


def test_flac_fingerprint_is_a_non_audio_file() -> None:
    files = (_f("t01.flac", "Flac"), _f("show.ffp", "Flac FingerPrint"))
    assert _names(select(files, None), library=True) == ["show.ffp", "t01.flac"]


def test_every_audio_file_refused_makes_the_show_unavailable() -> None:
    files = (
        _f("t01.shn", "Shorten", refused=True),
        _f("t01.mp3", "VBR MP3", refused=True),
        _f("info.txt", "Text"),
    )
    chosen = select(files, None)
    assert chosen.is_restricted is True
    assert chosen.restriction_reason == DOWNLOAD_REFUSED_REASON


def test_no_audio_listed_is_an_empty_selection() -> None:
    """Not a refusal: the planner records it as 'lists no files' and keeps the copy."""
    chosen = select((_f("a.txt", "Text"), _f("x_meta.xml", "Metadata")), None)
    assert chosen.manifest.files == ()
    assert chosen.is_restricted is False


def test_refused_non_audio_files_are_left_out() -> None:
    files = (_f("t01.flac", "Flac"), _f("secret.txt", "Text", refused=True))
    assert _names(select(files, None)) == ["t01.flac"]


def test_item_restriction_marks_the_item_restricted() -> None:
    chosen = select((_f("a.flac"),), "access-restricted")
    assert chosen.is_restricted is True
    assert chosen.restriction_reason == "access-restricted"


def test_manifest_carries_size_and_checksum() -> None:
    files = (SourceFile(name="a.flac", size=10, md5="ab" * 16),)
    (entry,) = select(files, None).manifest.files
    assert (entry.size, entry.md5) == (10, "ab" * 16)


def test_library_flag_round_trips_and_defaults_to_staged() -> None:
    manifest = Manifest(
        files=(ManifestFile("a.shn", 1, None, library=False), ManifestFile("a.mp3", 1, None))
    )
    raw = manifest.to_json()
    assert raw == [
        {"name": "a.shn", "size": 1, "md5": None, "library": False},
        {"name": "a.mp3", "size": 1, "md5": None},
    ]
    assert [f.library for f in Manifest.from_json(raw).files] == [False, True]


def test_library_flag_is_not_part_of_manifest_identity() -> None:
    staged = Manifest(files=(ManifestFile("a.shn", 1, None),))
    archive_only = Manifest(files=(ManifestFile("a.shn", 1, None, library=False),))
    assert staged == archive_only


def test_lossless_copies_rank_flac_alac_wav_aiff_then_larger_size_per_track() -> None:
    """Operator decision (Q-6-1): a fixed format order, then larger size per track
    (24-bit over 16-bit FLAC), never WAV over FLAC of the same audio."""
    flac16 = (_f("t01.flac", "Flac", size=30 * MB),)
    flac24 = (_f("t01_24.flac", "24bit Flac", size=60 * MB),)
    wav = (_f("t01.wav", "WAVE", size=90 * MB),)
    alac = (_f("t01.m4a", "Apple Lossless Audio", size=40 * MB),)
    aiff = (_f("t01.aif", "AIFF", size=95 * MB),)
    assert _names(select((*flac16, *flac24), None)) == ["t01_24.flac"]
    assert _names(select((*wav, *flac16), None)) == ["t01.flac"]
    assert _names(select((*aiff, *wav, *alac), None)) == ["t01.m4a"]
    assert _names(select((*aiff, *wav), None)) == ["t01.wav"]


def test_zips_of_the_audio_are_left_out() -> None:
    """Operator decision (Q-6-5): archive.org's ZIPs of a show's audio repeat the
    music; other ZIPs are kept like any non-audio file."""
    files = (
        _f("t01.mp3", "64Kbps MP3"),
        _f("show_64kb_mp3.zip", "64Kbps MP3 ZIP"),
        _f("show_vbr_mp3.zip", "VBR ZIP"),
        _f("artwork.zip", "ZIP"),
        _f("info.txt", "Text"),
    )
    assert _names(select(files, None)) == ["artwork.zip", "info.txt", "t01.mp3"]


def test_unrecognised_audio_only_is_staged_as_is_and_not_playable() -> None:
    """Operator decision (Q-6-4): kept and staged as is, and listed as not playable."""
    files = (_f("t01.ape", "Monkey's Audio"), _f("t02.ape", "Monkey's Audio"), _f("info.txt", "Text"))
    chosen = select(files, None)
    assert _names(chosen, library=True) == ["info.txt", "t01.ape", "t02.ape"]
    assert chosen.not_playable_reason == NOT_PLAYABLE_UNRECOGNISED_REASON
    assert chosen.is_restricted is False


# --- archive.org labels one show's tracks inconsistently -----------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _real_listing(name: str) -> tuple[SourceFile, ...]:
    """A real archive.org listing, through the same mapping and validator a sync uses."""
    raw = json.loads((FIXTURES / name).read_text())
    identifier = raw["metadata"]["identifier"]
    lookup = ItemLookup(metadata=raw["metadata"], files=file_records(identifier, raw["files"]))
    return validate_lookup(lookup, identifier).files


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def test_flac_tracks_under_two_labels_still_cover_the_show() -> None:
    """aru2015-08-15.24Bit48khzMS: 17 tracks labelled "24bit Flac" and track 07
    labelled "Flac". Together they are the show's lossless copy, which beats the
    VBR MP3 (seen on the NAS, 2026-11-11: the library got the MP3)."""
    chosen = select(_real_listing("aru2015-08-15.24Bit48khzMS.json"), None)
    audio = [n for n in _names(chosen) if _ext(n) in {"flac", "mp3", "ogg"}]
    assert len(audio) == 18 and all(n.endswith(".flac") for n in audio)
    assert "aru2015-08-1524Bit48khzMSd1t07.flac" in _names(chosen, library=True)
    assert not [n for n in _names(chosen) if n.endswith(".mp3")]


def test_two_complete_lossless_sets_are_not_merged() -> None:
    """A 24-bit and a 16-bit set that each cover every track stay two copies, and the
    larger per track wins (Q-6-1)."""
    files = (
        _f("t01.flac", "24bit Flac", size=60 * MB),
        _f("t02.flac", "24bit Flac", size=60 * MB),
        _f("t01_16.flac", "Flac", size=30 * MB),
        _f("t02_16.flac", "Flac", size=30 * MB),
    )
    assert _names(select(files, None)) == ["t01.flac", "t02.flac"]


def test_lossy_copies_of_different_bitrates_are_not_merged() -> None:
    """Each archive.org lossy derivative covers every track on its own; one track's VBR
    and 64 kb copies are never mixed into one copy."""
    chosen = select(_flac_show(), None)
    assert not [n for n in _names(chosen) if n.endswith(".mp3")]
    files = tuple(f for f in _flac_show() if not f.name.endswith(".flac"))
    assert [n for n in _names(select(files, None)) if n.endswith(".mp3")] == [
        "t01.mp3", "t02.mp3"
    ]




def test_partly_derived_lossy_copies_never_outweigh_a_complete_lossless_copy() -> None:
    """One track failed to derive: VBR and 64 kb MP3 each miss it. Their files have
    distinct names but are two copies of the same tracks, so they never merge into one
    oversized copy that pushes out the complete FLAC (independent review, 2026-11-11)."""
    files = [_f(f"t{n:02}.flac", "Flac", size=40 * MB) for n in range(1, 18)]
    for n in range(1, 17):
        files.append(_f(f"t{n:02}.mp3", "VBR MP3", size=9 * MB, original=f"t{n:02}.flac"))
        files.append(
            _f(f"t{n:02}_64kb.mp3", "64Kbps MP3", size=3 * MB, original=f"t{n:02}.flac")
        )
    chosen = select(files, None)
    assert _names(chosen) == [f"t{n:02}.flac" for n in range(1, 18)]


def test_split_labels_merge_across_disc_folders() -> None:
    """Tracks are told apart by their whole path, so disc folders that repeat track
    names still form one copy."""
    files = (
        _f("cd1/t01.flac", "24bit Flac", size=60 * MB),
        _f("cd1/t02.flac", "24bit Flac", size=60 * MB),
        _f("cd2/t01.flac", "24bit Flac", size=60 * MB),
        _f("cd2/t02.flac", "Flac", size=30 * MB),
        *(
            _f(f"cd{d}/t0{n}.mp3", "VBR MP3", size=9 * MB, original=f"cd{d}/t0{n}.flac")
            for d in (1, 2) for n in (1, 2)
        ),
    )
    assert [n for n in _names(select(files, None)) if n.endswith(".mp3")] == []

# --- which non-audio files the library gets ------------------------------------------


def test_the_library_gets_the_uploaders_files_and_the_archive_gets_everything() -> None:
    """Operator decision, 2026-11-11: every non-audio file is mirrored, but only the
    uploader's own (notes, checksums, artwork) is staged; what archive.org generates
    (spectrograms, fingerprints, thumbnails, index files, torrents) stays archive-only."""
    chosen = select(_real_listing("aru2015-08-15.24Bit48khzMS.json"), None)
    staged_other = sorted(
        n for n in _names(chosen, library=True) if not n.endswith(".flac")
    )
    assert staged_other == [
        "aru2015-08-15.24Bit48khzMS_01.jpg",
        "aru2015-08-15.txt",
        "aru2015-08-15MS.flac24.ffp",
    ]
    archive_only = _names(chosen, library=False)
    for kept in (
        "__ia_thumb.jpg",
        "aru2015-08-15.24Bit48khzMS_01_thumb.jpg",
        "aru2015-08-15.24Bit48khzMS_archive.torrent",
        "aru2015-08-15.24Bit48khzMS_files.xml",
        "aru2015-08-15.24Bit48khzMS_meta.xml",
        "aru2015-08-1524Bit48khzMSd1t01.afpk",
        "aru2015-08-1524Bit48khzMSd1t01.png",
    ):
        assert kept in archive_only
    assert [n for n in archive_only if "spectrogram" in n]


def test_an_shn_show_stages_its_mp3_with_the_notes_and_checksums() -> None:
    chosen = select(_real_listing("aru1993-08-13.shnf.json"), None)
    assert _names(chosen, library=True) == [
        "aru1993-08-13.md5", "aru1993-08-13.txt", "aru1993-08-13t01.mp3"
    ]
    assert "aru1993-08-13t01.shn" in _names(chosen, library=False)
    assert "aru1993-08-13t01.png" in _names(chosen, library=False)


def test_uploaded_extras_in_subfolders_are_staged() -> None:
    """Booklet scans, PDFs and documents an uploader adds are part of the show; the
    files archive.org derives from them are not."""
    files = (
        _f("t01.flac", "Flac"),
        _f("extras/Booklet/center.jwl", "Unknown"),
        _f("extras/Covers/cover.pdf", "PDF"),
        _f("extras/Comments/story.doc", "Word Document"),
        _f("extras/Comments/story.pdf", "Text PDF", original="extras/Comments/story.doc"),
        _f("extras/Comments/story_djvu.txt", "DjVuTXT", original="extras/Comments/story.pdf"),
        _f("show_reviews.xml", "Metadata"),
        _f("show_meta.sqlite", "Metadata"),
        _f("show_archive.torrent", "Archive BitTorrent"),
    )
    chosen = select(files, None)
    assert _names(chosen, library=True) == [
        "extras/Booklet/center.jwl",
        "extras/Comments/story.doc",
        "extras/Covers/cover.pdf",
        "t01.flac",
    ]
    assert len(_names(chosen, library=False)) == 5
