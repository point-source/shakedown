"""The hand-built audio fixtures parse as the formats they claim, so tests built on
them exercise mutagen's real readers."""
from __future__ import annotations

import io

import pytest
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from tests.audio_fixtures import id3_prefixed_flac_bytes, m4a_bytes, wav_bytes


@pytest.mark.parametrize("moov_last", [True, False])
@pytest.mark.parametrize(("codec", "expected"), [("aac", "mp4a.40.2"), ("alac", "alac")])
def test_m4a_fixture_parses_with_its_codec(codec: str, expected: str, moov_last: bool) -> None:
    info = MP4(io.BytesIO(m4a_bytes(codec, moov_last=moov_last))).info
    assert info.codec == expected
    assert (info.sample_rate, info.channels) == (44100, 2)


def test_m4a_fixture_can_put_the_track_description_after_the_audio() -> None:
    data = m4a_bytes(moov_last=True)
    assert data.index(b"mdat") < data.index(b"moov")


def test_id3_prefixed_flac_fixture_is_flac_behind_an_id3_tag() -> None:
    data = id3_prefixed_flac_bytes()
    assert data.startswith(b"ID3")
    assert FLAC(io.BytesIO(data)).info.sample_rate == 44100


def test_wav_fixture_is_riff_wave() -> None:
    data = wav_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
