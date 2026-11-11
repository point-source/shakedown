"""Tiny valid FLAC, MP3 and Ogg Vorbis files built by hand, so tag-filling tests need
no encoder. mutagen parses and tags them like real recordings; the audio itself is
silence-shaped filler no player needs to decode."""
from __future__ import annotations

import struct
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.ogg import OggPage
from mutagen.oggvorbis import OggVorbis


def flac_bytes() -> bytes:
    """`fLaC`, one STREAMINFO block (44.1 kHz, stereo, 16 bit, one second), filler."""
    info = struct.pack(">HH", 4096, 4096) + bytes(6)
    info += ((44100 << 44) | (1 << 41) | (15 << 36) | 44100).to_bytes(8, "big") + bytes(16)
    return b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info + b"\xff\xf8" + bytes(2000)


def mp3_bytes() -> bytes:
    """Twenty MPEG-1 Layer III frames (128 kbps, 44.1 kHz), no ID3 tag."""
    return (b"\xff\xfb\x90\x00" + bytes(413)) * 20


def ogg_bytes() -> bytes:
    """Vorbis identification, comment (no fields) and setup packets, then filler."""
    ident = b"\x01vorbis" + struct.pack("<IBIiiiBB", 0, 2, 44100, 0, 128000, 0, 0xB8, 1)
    comment = b"\x03vorbis" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0) + b"\x01"
    setup = b"\x05vorbis" + bytes(40)
    pages = []
    for sequence, packets in enumerate(([ident], [comment, setup], [bytes(3000)])):
        page = OggPage()
        page.packets, page.serial, page.sequence = packets, 1, sequence
        page.first = sequence == 0
        page.last = sequence == 2
        page.position = 44100 if sequence == 2 else 0
        pages.append(page.write())
    return b"".join(pages)


def make_audio(path: Path, tags: dict[str, str] | None = None, *, id3_version: int = 4) -> Path:
    """Write a fixture chosen by the path's suffix, with these tags already present
    (format-neutral names: artist, album, date, ...)."""
    suffix = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes({".flac": flac_bytes, ".mp3": mp3_bytes, ".ogg": ogg_bytes}[suffix]())
    if tags:
        if suffix == ".mp3":
            id3 = EasyID3()
            for key, value in tags.items():
                id3[key] = [value]
            id3.save(path, v2_version=id3_version)
        else:
            f = FLAC(path) if suffix == ".flac" else OggVorbis(path)
            if f.tags is None:
                f.add_tags()
            for key, value in tags.items():
                f[key.upper()] = [value]
            f.save()
    return path


def read_tags(path: Path) -> dict[str, list[str]]:
    """The file's tags under format-neutral lower-case names."""
    if path.suffix.lower() == ".mp3":
        return {k: list(v) for k, v in EasyID3(path).items()}
    f = FLAC(path) if path.suffix.lower() == ".flac" else OggVorbis(path)
    return {k.lower(): list(v) for k, v in f.items()}


def mp3_with_text_shaped_link_frame() -> bytes:
    """An MP3 whose ID3v2.3 tag holds a LINK frame written like a text frame (an
    encoding byte, then a URL), as archive.org's MP3s of aru1993-08-13.shnf carry.
    mutagen reads it, but cannot save the tag as v2.3: "Invalid frame ID"."""
    def frame(frame_id: bytes, body: bytes) -> bytes:
        return frame_id + len(body).to_bytes(4, "big") + b"\x00\x00" + body

    frames = frame(b"TPE1", b"\x00Aquarium Rescue Unit") + frame(
        b"LINK", b"\x00http://www.archive.org/details/aru1993-08-13.shnf"
    )
    size = bytes((len(frames) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + size + frames + mp3_bytes()
