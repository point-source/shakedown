"""Core-owned file selection: the quality rule (§spec:quality-selection), applied to
every source's raw listing (§spec:per-item-isolation).

The best available copy and the best playable copy are mirrored (one copy when they
are the same format), plus every non-audio file; the library stages the best
playable copy and the non-audio files."""
from __future__ import annotations

from shakedown.models import Manifest, ManifestFile, SourceFile
from shakedown.selection import (
    DOWNLOAD_REFUSED_REASON,
    LOSSLESS_REFUSED_REASON,
    NOT_PLAYABLE_SHN_REASON,
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
        derived_from=original,
        bitrate=bitrate,
    )


NON_AUDIO = (
    _f("info.txt", "Text"),
    _f("show.md5", "Checksums"),
    _f("show.ffp", "Flac FingerPrint"),
    _f("show_meta.xml", "Metadata"),
    _f("cover.jpg", "JPEG"),
    _f("show_spectrogram.png", "PNG"),
)
NON_AUDIO_NAMES = sorted(f.name for f in NON_AUDIO)


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
    assert _names(chosen, library=False) == []
    assert chosen.restriction_reason is None
    assert chosen.not_playable_reason is None
    assert chosen.is_restricted is False


def test_shn_show_keeps_shn_archive_only_and_stages_the_vbr_mp3() -> None:
    chosen = select(_gd77_shn_show(), None)
    shn = [f"gd77-05-08d1t0{n}.shn" for n in (1, 2, 3)]
    mp3 = [f"gd77-05-08d1t0{n}_vbr.mp3" for n in (1, 2, 3)]
    assert _names(chosen, library=False) == shn
    assert _names(chosen, library=True) == sorted([*mp3, *NON_AUDIO_NAMES])
    assert chosen.restriction_reason is None
    assert chosen.not_playable_reason is None


def test_shn_show_without_a_playable_copy_is_staged_as_is_and_not_playable() -> None:
    chosen = select(_gd77_shn_show(derivatives=False), None)
    assert _names(chosen, library=False) == []
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
