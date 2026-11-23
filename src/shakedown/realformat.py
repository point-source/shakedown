"""A file's real audio format, known from its content (§spec:real-format).

Uploaders sometimes name a file in one format while it holds another (AAC in an M4A
container named `.flac`). Every file whose name ends in one of CHECKED_SUFFIXES is
checked once, when it arrives, and the result is recorded beside the manifest
(`FormatRepo`). Everything that acts on a format afterwards (library names, the tag
writer, later the best-copy choice) reads the record, never the file.

Identification goes through mutagen's own format readers (principles: boring edges,
don't rebuild), with two corrections for how they behave on mislabelled files:

- mutagen's format guess (`mutagen.File`) weighs the file name: `FLAC.score` adds 3
  for a `.flac` name, so an M4A named `.flac` would be opened as FLAC and fail. The
  name is never given to mutagen here; each reader is tried in turn.
- mutagen's MP3 reader syncs to the first frame-like bytes anywhere in a file, so it
  can "read" PCM audio as MP3. It runs last, and only when mutagen's MP3 sniff of the
  file's header (without the name) says MP3. The strict readers go first because the
  FLAC reader skips an ID3 tag some taggers put in front of `fLaC`, which the MP3
  sniff alone would take for MP3.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from shakedown.models import Manifest


class RealFormat(StrEnum):
    """What a check found. A recognised format's value is its library extension
    without the dot."""
    FLAC = "flac"
    MP3 = "mp3"
    OGG = "ogg"  # Ogg Vorbis
    M4A = "m4a"  # an MP4 audio container holding AAC or Apple Lossless
    UNIDENTIFIED = "unidentified"


class AudioClass(StrEnum):
    LOSSLESS = "lossless"
    LOSSY = "lossy"


# Files whose name ends in one of these (any letter case) are checked. Other audio
# (SHN, WAV, APE, ...) is taken by its name (§req:constraints: mislabelled audio scope).
CHECKED_SUFFIXES = tuple(f".{f.value}" for f in RealFormat if f != RealFormat.UNIDENTIFIED)

# Bytes of a file's start mutagen's sniffers look at (what mutagen.File reads).
_HEADER_BYTES = 128


@dataclass(frozen=True)
class FormatCheck:
    """One file's real format and its audio class (None when unidentified)."""
    format: RealFormat
    audio_class: AudioClass | None

    @property
    def recognised(self) -> bool:
        return self.format != RealFormat.UNIDENTIFIED

    @property
    def extension(self) -> str | None:
        """The real format's lowercase extension, `.m4a`; None when unidentified."""
        return f".{self.format.value}" if self.recognised else None

    @property
    def label(self) -> str:
        """How the run log names what the check found: `M4A (AAC)`."""
        if self.format == RealFormat.M4A:
            codec = "Apple Lossless" if self.audio_class == AudioClass.LOSSLESS else "AAC"
            return f"M4A ({codec})"
        return format_label(self.format)

    def differs_from(self, name: str) -> bool:
        """The content is not what the name says (an unidentified file differs from
        any checked name)."""
        return named_format(name) != self.format


UNIDENTIFIED = FormatCheck(RealFormat.UNIDENTIFIED, None)


def format_label(fmt: RealFormat) -> str:
    return "Ogg Vorbis" if fmt == RealFormat.OGG else fmt.value.upper()


def is_checked(name: str) -> bool:
    return name.lower().endswith(CHECKED_SUFFIXES)


def named_format(name: str) -> RealFormat | None:
    """The format a checked file's name says; None for a name outside the checked set."""
    lower = name.lower()
    for suffix in CHECKED_SUFFIXES:
        if lower.endswith(suffix):
            return RealFormat(suffix[1:])
    return None


def check_file(path: Path) -> FormatCheck:
    """Identify the file's real format from its content, reading only the parts that
    identify it: the start, and for M4A the track description, which may sit at the
    end (mutagen's MP4 reader seeks over the audio to it).

    Raises OSError when the file cannot be read at all; any content mutagen cannot
    read as one of the four formats is UNIDENTIFIED."""
    with path.open("rb") as f:
        header = f.read(_HEADER_BYTES)
    try:
        FLAC(path)
        return FormatCheck(RealFormat.FLAC, AudioClass.LOSSLESS)
    except MutagenError:
        pass
    try:
        OggVorbis(path)
        return FormatCheck(RealFormat.OGG, AudioClass.LOSSY)
    except MutagenError:
        pass
    try:
        codec = MP4(path).info.codec or ""
    except MutagenError:
        pass
    else:
        if codec.startswith("mp4a.40"):
            return FormatCheck(RealFormat.M4A, AudioClass.LOSSY)  # AAC
        if codec == "alac":
            return FormatCheck(RealFormat.M4A, AudioClass.LOSSLESS)
        return UNIDENTIFIED  # an MP4 holding another codec
    if MP3.score("", None, header) > 0:
        try:
            MP3(path)
            return FormatCheck(RealFormat.MP3, AudioClass.LOSSY)
        except MutagenError:
            pass
    return UNIDENTIFIED


# A recorded check: the manifest file's (size, md5) it was made for, and what it found.
FormatRecord = tuple[int | None, str | None, FormatCheck]


def valid_formats(
    manifest: Manifest, records: Mapping[str, FormatRecord]
) -> dict[str, FormatCheck]:
    """The checks that still describe the manifest's files. A check belongs to the
    file as the manifest records it (name, size, source checksum): one made for other
    bytes says nothing about these."""
    valid: dict[str, FormatCheck] = {}
    for mf in manifest.files:
        record = records.get(mf.name)
        if record is not None and record[:2] == (mf.size, mf.md5):
            valid[mf.name] = record[2]
    return valid
