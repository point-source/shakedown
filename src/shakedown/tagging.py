"""Opt-in tag filling (§spec:tag-filling).

A collection with `fill_tags` gets missing basic tags filled from the source metadata
shakedown already holds, so shows group and display correctly in a tag-driven player
such as Navidrome. This module computes what each file's tags should be. Everything
here is a function of recorded state (metadata, manifest, which shows have an archive
copy), so a show's computed tags never require reading its files.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis

from shakedown.models import Manifest
from shakedown.state import TagRecord

log = logging.getLogger(__name__)

# The venue goes in the format's custom field: Vorbis comment VENUE, ID3 TXXX:VENUE.
EasyID3.RegisterTXXXKey("venue", "VENUE")

# Format-neutral tag names; the writer maps each to the format's own field.
TAG_KEYS = ("artist", "albumartist", "album", "date", "venue", "title", "tracknumber")

# The formats shakedown can tag (§spec:tag-filling): FLAC, MP3, Ogg Vorbis. Every other
# file, SHN included, is left untouched.
TAGGABLE_SUFFIXES = (".flac", ".mp3", ".ogg")

# The album separator, exactly as SPEC writes it: an en dash between spaces.
ALBUM_SEPARATOR = " \N{EN DASH} "

Desired = dict[str, dict[str, str]]  # file name -> tag name -> value


def is_taggable(name: str) -> bool:
    return name.lower().endswith(TAGGABLE_SUFFIXES)


def _text(value: Any) -> str | None:
    """One tag value from a metadata field. A repeated field (two creators) joins with
    ", ", as library_layout renders it (utils.templates.render)."""
    if isinstance(value, list | tuple):
        value = ", ".join(str(v) for v in value if v is not None)
    if value is None:
        return None
    return str(value).strip() or None


def _show_values(identifier: str, metadata: Mapping[str, Any]) -> dict[str, str]:
    """The show-level tags, album without any clash label. Album is `<date> <en dash> <venue>`;
    when either is missing, the show's title, else its identifier (Q-8-2: a missing
    date falls back like a missing venue, so a tag never says `unknown`)."""
    creator = _text(metadata.get("creator"))
    date = _text(metadata.get("date"))
    venue = _text(metadata.get("venue"))
    if date and venue:
        album = f"{date}{ALBUM_SEPARATOR}{venue}"
    else:
        album = _text(metadata.get("title")) or identifier
    values = {"artist": creator, "albumartist": creator, "album": album,
              "date": date, "venue": venue}
    return {k: v for k, v in values.items() if v is not None}


def desired_tags(
    identifier: str, metadata: Mapping[str, Any], manifest: Manifest, *, labelled: bool
) -> Desired:
    """Every taggable file's computed tags. `labelled` appends ` [<identifier>]` to the
    album: the show clashes with another (TagIndex)."""
    show = _show_values(identifier, metadata)
    if labelled:
        show["album"] = f"{show['album']} [{identifier}]"
    desired: Desired = {}
    for mf in manifest.files:
        if not is_taggable(mf.name):
            continue
        tags = dict(show)
        if mf.title is not None:
            tags["title"] = mf.title
        if mf.track is not None:
            tags["tracknumber"] = mf.track
        desired[mf.name] = tags
    return desired


def fingerprint(desired: Desired) -> str:
    """A show's computed tags in one comparable value: a sync revisits a show only
    when this changes."""
    return hashlib.sha256(
        json.dumps(desired, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class TagIndex:
    """Which shows of one collection clash: two or more shows shakedown tags that
    would end up with the same album artist, album, and date — which a tag-driven
    player merges into one album. Every clasher is labelled, by the rule library
    folders follow (§spec:library-ownership). Keys come from computed values only, so
    the answer depends on recorded state alone, never on order or on the files."""

    def __init__(self) -> None:
        self._key: dict[str, tuple[str, str, str]] = {}
        self._members: dict[tuple[str, str, str], set[str]] = {}

    def add(self, identifier: str, metadata: Mapping[str, Any], manifest: Manifest) -> None:
        """Index (or re-index) a show that has an archive copy. A show with no
        taggable file is not tagged, so it clashes with nothing."""
        self.remove(identifier)
        if not any(is_taggable(mf.name) for mf in manifest.files):
            return
        show = _show_values(identifier, metadata)
        key = (show.get("albumartist", ""), show["album"], show.get("date", ""))
        self._key[identifier] = key
        self._members.setdefault(key, set()).add(identifier)

    def remove(self, identifier: str) -> None:
        key = self._key.pop(identifier, None)
        if key is not None:
            self._members[key].discard(identifier)

    def labelled(self, identifier: str) -> bool:
        key = self._key.get(identifier)
        return key is not None and len(self._members[key]) > 1


# --- writing one file ---------------------------------------------------------------


class TagWriteError(Exception):
    """Writing a file's tags failed on the disk (no space, permission). The file is
    left exactly as it was."""


@dataclass(frozen=True)
class FillResult:
    """What one visit did to a file. `record` is what shakedown now holds for it:
    the post-tag checksum and the values still its own, or None when it has written
    nothing to this file."""
    status: Literal["written", "unchanged", "untaggable"]
    record: TagRecord | None


def _fault_point(stage: str) -> None:
    """Test seam for crash injection: called after the tagged copy is saved
    ("saved") and after it replaced the file ("replaced")."""


def _absent(values: list[str] | None) -> bool:
    return not values


class _Tags:
    """One file's tags behind format-neutral names, through mutagen. MP3 goes through
    EasyID3 and is saved in the ID3 version it already had, so nobody else's frames
    are converted; FLAC and Ogg Vorbis use Vorbis comments."""

    def __init__(self, path: Path) -> None:
        suffix = path.suffix.lower()
        self._id3: EasyID3 | None = None
        self._vorbis: FLAC | OggVorbis | None = None
        if suffix == ".mp3":
            MP3(path)  # raises unless the file really is MPEG audio
            with path.open("rb") as f:
                header = f.read(4)
            # Save in the ID3v2 version the file has (its header's major version byte);
            # an untagged file gets v2.4, mutagen's default.
            self._v2 = 3 if header[:3] == b"ID3" and header[3] == 3 else 4
            try:
                self._id3 = EasyID3(path)
            except ID3NoHeaderError:
                self._id3 = EasyID3()
        else:
            self._vorbis = FLAC(path) if suffix == ".flac" else OggVorbis(path)
            if self._vorbis.tags is None:
                self._vorbis.add_tags()

    def get(self, key: str) -> list[str] | None:
        if self._id3 is not None:
            return list(self._id3[key]) if key in self._id3 else None
        assert self._vorbis is not None
        return list(self._vorbis[key]) if key in self._vorbis else None

    def set(self, key: str, value: str) -> None:
        if self._id3 is not None:
            self._id3[key] = [value]
        else:
            assert self._vorbis is not None
            self._vorbis[key.upper()] = [value]

    def save(self, path: Path) -> None:
        if self._id3 is not None:
            self._id3.save(path, v2_version=self._v2)
        else:
            assert self._vorbis is not None
            self._vorbis.save(path)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fill_file(
    path: Path, desired: Mapping[str, str], record: TagRecord | None, temp_dir: Path
) -> FillResult:
    """Fill the file's absent tags with the desired values (§spec:tag-filling).

    A tag already present is never changed — except a value shakedown wrote itself
    (in `record`) whose computed value changed, and only while the file still holds
    exactly that value; one anything else changed is no longer shakedown's. A file
    with nothing to change is not written. A change is made on a copy in `temp_dir`
    (same filesystem) that then replaces the file in one rename, so a crash leaves
    either the old bytes or the new ones, never a half-written file.
    """
    try:
        tags = _Tags(path)
    except MutagenError as e:
        log.warning("not filling tags in %s: the file cannot be read as %s: %s",
                    path, path.suffix.lstrip(".").upper(), e)
        return FillResult("untaggable", record)

    ours = dict(record.written) if record is not None else {}
    changes: dict[str, str] = {}
    for key, value in desired.items():
        current = tags.get(key)
        if _absent(current):
            changes[key] = value
        elif key in ours:
            if current != [ours[key]]:
                del ours[key]  # someone else's value now
            elif ours[key] != value:
                changes[key] = value
    for key in list(ours):
        if key not in desired and tags.get(key) != [ours[key]]:
            del ours[key]

    if not changes:
        if record is None:
            return FillResult("unchanged", None)
        return FillResult("unchanged", replace(record, written=ours))

    for key, value in changes.items():
        tags.set(key, value)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = temp_dir / f"{uuid.uuid4().hex}{path.suffix}"
    try:
        shutil.copyfile(path, temp)
        shutil.copymode(path, temp)
        tags.save(temp)
        _fsync(temp)
        post_tag_md5 = _md5(temp)
        _fault_point("saved")
        os.replace(temp, path)
    except (OSError, MutagenError) as e:
        raise TagWriteError(str(e)) from e
    finally:
        temp.unlink(missing_ok=True)
    _fsync(path.parent)
    _fault_point("replaced")
    return FillResult("written", TagRecord(post_tag_md5, {**ours, **changes}))
