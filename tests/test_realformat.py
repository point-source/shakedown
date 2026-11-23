"""A file's real format, known from its content (§spec:real-format)."""
from __future__ import annotations

from pathlib import Path

import pytest

from shakedown.models import Manifest, ManifestFile
from shakedown.realformat import (
    AudioClass,
    FormatCheck,
    RealFormat,
    check_file,
    is_checked,
    named_format,
    valid_formats,
)
from tests.audio_fixtures import id3_prefixed_flac_bytes, make_audio

FLAC = FormatCheck(RealFormat.FLAC, AudioClass.LOSSLESS)
MP3 = FormatCheck(RealFormat.MP3, AudioClass.LOSSY)
OGG = FormatCheck(RealFormat.OGG, AudioClass.LOSSY)
AAC = FormatCheck(RealFormat.M4A, AudioClass.LOSSY)
ALAC = FormatCheck(RealFormat.M4A, AudioClass.LOSSLESS)
UNIDENTIFIED = FormatCheck(RealFormat.UNIDENTIFIED, None)

CONTENTS = {"flac": FLAC, "mp3": MP3, "ogg": OGG, "m4a": AAC, "alac": ALAC}


@pytest.mark.parametrize("name", ["t.flac", "t.mp3", "t.ogg", "t.m4a", "t.FLAC"])
@pytest.mark.parametrize("content", sorted(CONTENTS))
def test_a_file_is_known_by_its_content_whatever_its_name(
    tmp_path: Path, content: str, name: str
) -> None:
    path = make_audio(tmp_path / name, content=content)
    assert check_file(path) == CONTENTS[content]


def test_an_m4a_whose_track_description_comes_first_is_known_too(tmp_path: Path) -> None:
    from tests.audio_fixtures import m4a_bytes

    path = tmp_path / "t.flac"
    path.write_bytes(m4a_bytes(moov_last=False))
    assert check_file(path) == AAC


def test_a_flac_behind_an_id3_tag_is_flac_not_mp3(tmp_path: Path) -> None:
    """mutagen's MP3 sniff scores any ID3 header; the FLAC reader skips the tag."""
    path = tmp_path / "t.mp3"
    path.write_bytes(id3_prefixed_flac_bytes())
    assert check_file(path) == FLAC


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"fLaC\x00", id="truncated-flac"),
        pytest.param(b"OggS" + bytes(23) + b"\x01\x13OpusHead" + bytes(40), id="ogg-opus"),
        pytest.param(b"\x00" * 3000, id="zeros"),
    ],
)
def test_content_that_is_none_of_the_four_formats_is_unidentified(
    tmp_path: Path, data: bytes
) -> None:
    path = tmp_path / "t.flac"
    path.write_bytes(data)
    assert check_file(path) == UNIDENTIFIED


def test_a_corrupt_ogg_is_unidentified_not_a_crash(tmp_path: Path) -> None:
    """mutagen's Ogg Vorbis reader raises IndexError (not a MutagenError) on a page
    with no segments; a broken upload must come out unidentified, or every run would
    download and crash on it again."""
    from tests.audio_fixtures import ogg_bytes

    data = bytearray(ogg_bytes())
    data[84] = 0  # the second page's segment count
    path = tmp_path / "x.flac"
    path.write_bytes(bytes(data))
    assert check_file(path) == UNIDENTIFIED


def test_a_wav_named_flac_is_unidentified(tmp_path: Path) -> None:
    assert check_file(make_audio(tmp_path / "t.flac", content="wav")) == UNIDENTIFIED


def test_a_file_that_cannot_be_read_is_an_error_not_a_verdict(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        check_file(tmp_path / "missing.flac")


@pytest.mark.parametrize(
    ("name", "checked"),
    [("a.flac", True), ("a.MP3", True), ("a.Ogg", True), ("a.m4a", True),
     ("a.shn", False), ("a.wav", False), ("flac", False), ("a.flac.md5", False)],
)
def test_which_files_are_checked(name: str, checked: bool) -> None:
    assert is_checked(name) is checked


def test_named_format_reads_the_suffix_in_any_case() -> None:
    assert named_format("1-01 St. Stephen.FLAC") == RealFormat.FLAC
    assert named_format("a.m4a") == RealFormat.M4A
    assert named_format("a.shn") is None


@pytest.mark.parametrize(
    ("check", "name", "differs"),
    [(FLAC, "a.FLAC", False), (AAC, "a.flac", True), (ALAC, "a.m4a", False),
     (MP3, "a.ogg", True), (UNIDENTIFIED, "a.flac", True)],
)
def test_whether_a_check_differs_from_the_name(
    check: FormatCheck, name: str, differs: bool
) -> None:
    assert check.differs_from(name) is differs


def test_extension_is_the_real_formats_lowercase_suffix() -> None:
    assert AAC.extension == ".m4a"
    assert FLAC.extension == ".flac"
    assert UNIDENTIFIED.extension is None


def test_a_record_counts_only_for_the_file_the_manifest_records() -> None:
    """The record belongs to (name, size, checksum): a file the source changed has no
    valid check until it is checked again."""
    manifest = Manifest(files=(
        ManifestFile("a.flac", 10, "aa"),
        ManifestFile("b.flac", 10, "bb"),
        ManifestFile("c.flac", 10, "cc"),
    ))
    records = {
        "a.flac": (10, "aa", AAC),
        "b.flac": (10, "changed", AAC),
        "c.flac": (11, "cc", AAC),
        "gone.flac": (10, "dd", AAC),
    }
    assert valid_formats(manifest, records) == {"a.flac": AAC}
