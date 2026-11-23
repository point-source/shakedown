"""Tiny valid FLAC, MP3, Ogg Vorbis and M4A files (and a WAV) built by hand, so tests
need no encoder. mutagen parses and tags them like real recordings; the audio itself is
silence-shaped filler no player needs to decode."""
from __future__ import annotations

import struct
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4, EasyMP4Tags
from mutagen.flac import FLAC
from mutagen.ogg import OggPage
from mutagen.oggvorbis import OggVorbis

# Venue as shakedown writes it in M4A: the iTunes freeform field VENUE (§spec:m4a-tags).
EasyMP4Tags.RegisterFreeformKey("venue", "VENUE")


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


def _box(name: bytes, *children: bytes) -> bytes:
    body = b"".join(children)
    return (8 + len(body)).to_bytes(4, "big") + name + body


def _full_box(name: bytes, body: bytes, version: int = 0) -> bytes:
    return _box(name, bytes([version, 0, 0, 0]) + body)


def _descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def m4a_bytes(codec: str = "aac", *, moov_last: bool = True, filler: int = 2000) -> bytes:
    """An MP4 audio file, as iTunes writes one: `ftyp`, `mdat` (filler), and a `moov`
    whose one sound track's `stsd` describes AAC-LC (`mp4a` + `esds`, 44.1 kHz stereo)
    or Apple Lossless (`alac`). `moov_last` puts the track description after the
    audio, as in the observed uploads. `filler` sets the size of the audio data, so a
    test can list an M4A set larger per track than another copy."""
    entry_head = bytes(6) + (1).to_bytes(2, "big") + bytes(8)
    entry_head += (2).to_bytes(2, "big") + (16).to_bytes(2, "big") + bytes(4)
    entry_head += (44100 << 16).to_bytes(4, "big")
    if codec == "aac":
        specific = _descriptor(0x05, b"\x12\x10")  # AAC LC, 44.1 kHz, stereo
        config = _descriptor(
            0x04, b"\x40\x15" + bytes(3) + (256000).to_bytes(4, "big") * 2 + specific
        )
        es = _descriptor(0x03, (1).to_bytes(2, "big") + b"\x00" + config
                         + _descriptor(0x06, b"\x02"))
        entry = _box(b"mp4a", entry_head, _full_box(b"esds", es))
    elif codec == "alac":
        cookie = (4096).to_bytes(4, "big") + bytes([0, 16, 40, 10, 14, 2])
        cookie += (255).to_bytes(2, "big") + bytes(4) + (1411200).to_bytes(4, "big")
        cookie += (44100).to_bytes(4, "big")
        entry = _box(b"alac", entry_head, _full_box(b"alac", cookie))
    else:
        raise ValueError(codec)
    stsd = _full_box(b"stsd", (1).to_bytes(4, "big") + entry)
    empty = (0).to_bytes(4, "big")
    stbl = _box(b"stbl", stsd, _full_box(b"stts", empty), _full_box(b"stsc", empty),
                _full_box(b"stsz", empty * 2), _full_box(b"stco", empty))
    minf = _box(b"minf", _full_box(b"smhd", bytes(4)),
                # One data reference, flagged "media in this file".
                _box(b"dinf", _full_box(b"dref", (1).to_bytes(4, "big")
                                        + _box(b"url ", b"\x00\x00\x00\x01"))),
                stbl)
    mdhd = _full_box(b"mdhd", bytes(8) + (44100).to_bytes(4, "big")
                     + (44100).to_bytes(4, "big") + bytes(4))
    hdlr = _full_box(b"hdlr", bytes(4) + b"soun" + bytes(12) + b"SoundHandler\x00")
    tkhd = _full_box(b"tkhd", bytes(80), version=0)
    mvhd = _full_box(b"mvhd", bytes(8) + (44100).to_bytes(4, "big")
                     + (44100).to_bytes(4, "big") + bytes(80))
    moov = _box(b"moov", mvhd, _box(b"trak", tkhd, _box(b"mdia", mdhd, hdlr, minf)))
    ftyp = _box(b"ftyp", b"M4A " + bytes(4) + b"M4A mp42isom")
    mdat = _box(b"mdat", bytes(filler))
    return ftyp + (mdat + moov if moov_last else moov + mdat)


def wav_bytes() -> bytes:
    """A RIFF/WAVE file: 44.1 kHz 16-bit stereo PCM, a little silence."""
    data = bytes(4000)
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 4, 4, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def id3_prefixed_flac_bytes() -> bytes:
    """A FLAC file with an ID3v2.4 tag in front of `fLaC`, as some taggers write it."""
    frame = b"TPE1" + (9).to_bytes(4, "big") + b"\x00\x00" + b"\x03Artist\x00\x00"
    size = bytes((len(frame) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x04\x00\x00" + size + frame + flac_bytes()


CONTENT = {
    "flac": flac_bytes,
    "mp3": mp3_bytes,
    "ogg": ogg_bytes,
    "m4a": m4a_bytes,
    "alac": lambda: m4a_bytes("alac"),
    "wav": wav_bytes,
}


def make_audio(
    path: Path,
    tags: dict[str, str] | None = None,
    *,
    id3_version: int = 4,
    content: str | None = None,
) -> Path:
    """Write a fixture, with these tags already present (format-neutral names: artist,
    album, date, ...). `content` picks the audio (a CONTENT key) independently of the
    file name, for a file named in another format; by default the path's suffix
    decides."""
    suffix = path.suffix.lower()
    kind = content or suffix.lstrip(".")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CONTENT[kind]())
    if tags:
        if kind == "mp3":
            id3 = EasyID3()
            for key, value in tags.items():
                id3[key] = [value]
            id3.save(path, v2_version=id3_version)
        elif kind in ("m4a", "alac"):
            mp4 = EasyMP4(path)
            if mp4.tags is None:
                mp4.add_tags()
            for key, value in tags.items():
                mp4[key] = [value]
            mp4.save()
        else:
            f = FLAC(path) if kind == "flac" else OggVorbis(path)
            if f.tags is None:
                f.add_tags()
            for key, value in tags.items():
                f[key.upper()] = [value]
            f.save()
    return path


def read_tags(path: Path, content: str | None = None) -> dict[str, list[str]]:
    """The file's tags under format-neutral lower-case names. `content` names the
    audio when it differs from the file name (a CONTENT key)."""
    kind = content or path.suffix.lower().lstrip(".")
    if kind == "mp3":
        return {k: list(v) for k, v in EasyID3(path).items()}
    if kind in ("m4a", "alac"):
        return {k: list(v) for k, v in EasyMP4(path).items()}
    f = FLAC(path) if kind == "flac" else OggVorbis(path)
    return {k.lower(): list(v) for k, v in f.items()}


def mp3_with_text_shaped_link_frame(
    text: bytes = b"http://www.archive.org/details/aru1993-08-13.shnf",
) -> bytes:
    """An MP3 whose ID3v2.3 tag holds a LINK frame written like a text frame (an
    encoding byte, then a URL), as archive.org's MP3s of aru1993-08-13.shnf carry.
    mutagen reads it, but cannot save the tag as v2.3: "Invalid frame ID"."""
    def frame(frame_id: bytes, body: bytes) -> bytes:
        return frame_id + len(body).to_bytes(4, "big") + b"\x00\x00" + body

    frames = frame(b"TPE1", b"\x00Aquarium Rescue Unit") + frame(
        b"LINK", b"\x00" + text
    )
    size = bytes((len(frames) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + size + frames + mp3_bytes()
