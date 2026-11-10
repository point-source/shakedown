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
from collections.abc import Mapping
from typing import Any

from shakedown.models import Manifest

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
