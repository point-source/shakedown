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
import re
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

from shakedown.db import transaction
from shakedown.models import Manifest
from shakedown.placement import has_archive_copy
from shakedown.state import ItemRepo, TagRecord, TagRepo

log = logging.getLogger(__name__)

# The venue goes in the format's custom field: Vorbis comment VENUE, ID3 TXXX:VENUE.
EasyID3.RegisterTXXXKey("venue", "VENUE")

# Tags go by format-neutral names (artist, albumartist, album, date, venue, title,
# tracknumber); _Tags maps each to the format's own field.

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


class TagPlanner:
    """One collection run's clash index, seeded from every show with an archive copy
    and fed each show the run fetches, so a download is tagged with the album it gets
    among the collection as known so far. A clash that only shows up later in the run
    is corrected by the run's pass over mirrored shows."""

    def __init__(self, items: ItemRepo, source: str, collection: str) -> None:
        self.index = TagIndex()
        for item in items.list_for_collection(source, collection):
            if has_archive_copy(item):
                assert item.recorded_manifest is not None
                self.index.add(item.identifier, item.source_metadata, item.recorded_manifest)

    def plan(self, identifier: str, metadata: Mapping[str, Any], manifest: Manifest) -> Desired:
        self.index.add(identifier, metadata, manifest)
        return desired_tags(
            identifier, metadata, manifest, labelled=self.index.labelled(identifier)
        )


def tag_failure(file_name: str, error: object) -> str:
    """Why a show's tags are missing after a tag write failed on the disk; the run
    reports it as `<identifier>: <this>` (operator-approved copy, Q-8-4)."""
    return f"could not fill tags in {file_name} ({error}); will retry on the next run"


# --- writing one file ---------------------------------------------------------------

# Under each collection's archive folder (same filesystem, outside the library, ignored
# by the archive walk): backups of files being tagged, `<identifier>/<file name>`.
TAGGING_DIR = ".tagging"

_PART = re.compile(r"^\..*\.[0-9a-f]{32}\.part$")  # a backup still being written


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
    """Test seam for crash injection: called once the backup is complete
    ("backed_up") and once the tags are written ("saved")."""


def _absent(values: list[str] | None) -> bool:
    """A field with no values, or only empty or blank ones, carries nobody's work and
    counts as absent (Q-8-3)."""
    return not values or not any(v.strip() for v in values)


class _Tags:
    """One file's tags behind format-neutral names, through mutagen. MP3 goes through
    EasyID3 and is saved in the ID3 version it already had, so nobody else's frames
    are converted; FLAC and Ogg Vorbis use Vorbis comments. mutagen writes into the
    file itself, so every link to it carries the change."""

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


def backup_path(collection_root: Path, identifier: str, file_name: str) -> Path:
    """Where the original of an archive file is kept while its tags are written."""
    return collection_root / TAGGING_DIR / identifier / file_name


def _make_backup(path: Path, backup: Path) -> None:
    """Copy the whole original beside the archive, made durable under a temporary name
    first: a backup that exists is always complete."""
    backup.parent.mkdir(parents=True, exist_ok=True)
    part = backup.parent / f".{backup.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copyfile(path, part)
        _fsync(part)
        os.replace(part, backup)
        _fsync(backup.parent)
    finally:
        part.unlink(missing_ok=True)


def _restore(backup: Path, target: Path) -> None:
    """Put the original bytes back into the file itself (its inode), so every link to
    it holds them again."""
    with backup.open("rb") as src, target.open("r+b") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.truncate()
        dst.flush()
        os.fsync(dst.fileno())


def _discard(backup: Path) -> None:
    """Drop a backup, then any folders it leaves empty up to the collection's
    TAGGING_DIR."""
    backup.unlink(missing_ok=True)
    for folder in backup.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        if folder.name == TAGGING_DIR:
            return


def fill_file(
    path: Path, desired: Mapping[str, str], record: TagRecord | None, backup: Path | None
) -> FillResult:
    """Fill the file's absent tags with the desired values (§spec:tag-filling).

    A tag already present is never changed — except a value shakedown wrote itself
    (in `record`) whose computed value changed, and only while the file still holds
    exactly that value; one anything else changed is no longer shakedown's. A file
    with nothing to change is not written.

    Tags are written into the file itself, so the archive copy and every link to it
    carry them and no copy is left. For a file in the archive, `backup` journals the
    write: the whole original is kept there until the tags are written, and a crash
    in between is undone by recover_interrupted_tagging at the start of the next run.
    A file in a fetch's temp folder is linked nowhere yet and takes no backup.
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
            ours.pop(key, None)
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
    if backup is not None:
        try:
            _make_backup(path, backup)
        except OSError as e:
            raise TagWriteError(str(e)) from e
        _fault_point("backed_up")
    try:
        tags.save(path)
        _fsync(path)
    except (OSError, MutagenError) as e:
        if backup is not None:
            try:
                _restore(backup, path)
                _discard(backup)
            except OSError:
                log.exception("could not restore %s; the next run restores it", path)
        raise TagWriteError(str(e)) from e
    post_tag_md5 = _md5(path)
    _fault_point("saved")
    if backup is not None:
        _discard(backup)
    return FillResult("written", TagRecord(post_tag_md5, {**ours, **changes}))


def recover_interrupted_tagging(collection_root: Path) -> list[Path]:
    """Undo tag writes a crash interrupted, before a run does anything else (Q-8-1):
    every backup left under the collection's TAGGING_DIR goes back into its archive
    file, through the inode, so neither the archive nor any library link keeps a
    half-written file. A backup still being written means its file was not touched
    yet; it is discarded. Returns the files restored."""
    root = collection_root / TAGGING_DIR
    if not root.is_dir():
        return []
    restored: list[Path] = []
    for backup in sorted(p for p in root.rglob("*") if p.is_file()):
        if _PART.match(backup.name):
            backup.unlink()
            continue
        target = collection_root / backup.relative_to(root)
        if target.is_file():
            log.warning("restoring %s after an interrupted tag write", target)
            _restore(backup, target)
            restored.append(target)
        backup.unlink()
    shutil.rmtree(root)
    return restored


# --- a collection's shows ------------------------------------------------------------


def fill_collection_tags(
    collection_root: Path, items: ItemRepo, source: str, collection: str
) -> dict[str, list[str]]:
    """Fill the tags of every show with an archive copy whose computed tags changed
    since it was last filled: once after the collection opts in, then on a clash change
    or a metadata refresh (§spec:tag-filling). A show whose fingerprint still matches
    is skipped without opening any of its files. Each file's record is stored as soon
    as it is written, and the show's fingerprint once all its files are done, so a
    run that stops partway picks the show up again. Returns, per show, the issues of
    tag writes that failed on the disk; such a show is retried on the next run."""
    repo = TagRepo(items.conn)
    shows = [item for item in items.list_for_collection(source, collection)
             if has_archive_copy(item)]
    index = TagIndex()
    for item in shows:
        assert item.recorded_manifest is not None
        index.add(item.identifier, item.source_metadata, item.recorded_manifest)
    done = repo.fingerprints(source, collection)
    issues: dict[str, list[str]] = {}
    for item in shows:
        assert item.recorded_manifest is not None and item.archive_path is not None
        identifier = item.identifier
        desired = desired_tags(identifier, item.source_metadata, item.recorded_manifest,
                               labelled=index.labelled(identifier))
        show_fingerprint = fingerprint(desired)
        if done.get(identifier) == show_fingerprint:
            continue
        records = repo.file_records(source, collection, identifier)
        for name, wanted in sorted(desired.items()):
            path = item.archive_path / name
            if not path.is_file():
                continue  # nothing to tag; a refetch brings it back, tagged
            try:
                result = fill_file(path, wanted, records.get(name),
                                   backup_path(collection_root, identifier, name))
            except TagWriteError as e:
                issues.setdefault(identifier, []).append(
                    f"{identifier}: {tag_failure(name, e)}"
                )
                continue
            if result.record is not None and result.record != records.get(name):
                with transaction(items.conn):
                    repo.put_file(source, collection, identifier, name, result.record)
        if identifier not in issues:
            with transaction(items.conn):
                repo.set_fingerprint(source, collection, identifier, show_fingerprint)
    return issues
