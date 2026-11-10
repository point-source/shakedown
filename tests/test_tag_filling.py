"""Opt-in tag filling (§spec:tag-filling, §req:success-criteria #25)."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.oggvorbis import OggVorbis

from shakedown import tagging
from shakedown.config import CollectionConfig
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.reconcile import run_reconcile
from shakedown.restage import run_restage
from shakedown.state import DriftRepo, ItemRepo, RunRepo, TagRecord, TagRepo
from shakedown.sync import _record_disappeared, run_sync
from shakedown.tagging import (
    TagIndex,
    TagWriteError,
    desired_tags,
    fill_file,
    fingerprint,
    recover_interrupted_tagging,
)
from shakedown.verify import run_verify
from tests.audio_fixtures import make_audio, read_tags
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.serve_child import SPEC_ENV


def test_pruning_a_show_drops_its_tag_records(tmp_path: Path) -> None:
    """A pruned show's archive copy is gone, and with it the bytes the records
    describe."""
    conn = connect(tmp_path / "s.db")
    items, tags = ItemRepo(conn), TagRepo(conn)
    folder = tmp_path / "archive" / "x"
    folder.mkdir(parents=True)
    (folder / "a.flac").write_bytes(b"x")
    item = Item(
        source_name="s", collection_name="c", identifier="x", status=ItemStatus.COMPLETE,
        archive_path=folder, downloaded_at=datetime.now(),
        recorded_manifest=Manifest(files=(ManifestFile("a.flac", 1, None),)),
    )
    items.upsert(item)
    tags.put_file("s", "c", "x", "a.flac", TagRecord("md5", {"album": "A"}))
    tags.set_fingerprint("s", "c", "x", "fp")

    _record_disappeared(items, "s", CollectionConfig(name="c", query="q", prune_disappeared=True), item)

    assert tags.file_records("s", "c", "x") == {}
    assert tags.fingerprints("s", "c") == {}


# --- what is filled (pure computation) ---------------------------------------------

def _manifest(*names: str, **per_file: tuple[str | None, str | None]) -> Manifest:
    return Manifest(
        files=tuple(
            ManifestFile(n, 1, None, title=per_file.get(n, (None, None))[0],
                         track=per_file.get(n, (None, None))[1])
            for n in names
        )
    )


SHOW_META = {"creator": "Grateful Dead", "date": "1977-05-08", "venue": "Barton Hall",
             "title": "Grateful Dead Live at Barton Hall on 1977-05-08"}


def test_show_values_fill_artist_album_date_and_venue() -> None:
    desired = desired_tags("gd77", SHOW_META, _manifest("t1.flac"), labelled=False)
    assert desired == {
        "t1.flac": {
            "artist": "Grateful Dead",
            "albumartist": "Grateful Dead",
            "album": "1977-05-08 \N{EN DASH} Barton Hall",  # en dash, as SPEC writes it
            "date": "1977-05-08",
            "venue": "Barton Hall",
        }
    }


def test_album_falls_back_to_title_then_identifier() -> None:
    no_venue = {**SHOW_META, "venue": None}
    assert desired_tags("gd77", no_venue, _manifest("a.mp3"), labelled=False)["a.mp3"][
        "album"] == SHOW_META["title"]
    bare = {"creator": "Grateful Dead"}
    assert desired_tags("gd77", bare, _manifest("a.mp3"), labelled=False)["a.mp3"][
        "album"] == "gd77"


def test_album_without_a_date_falls_back_like_a_missing_venue() -> None:
    """Q-8-2: a missing date is treated as a missing venue; a tag never says
    `unknown`."""
    no_date = {**SHOW_META, "date": None}
    tags = desired_tags("gd77", no_date, _manifest("a.ogg"), labelled=False)["a.ogg"]
    assert tags["album"] == SHOW_META["title"]
    assert "date" not in tags
    assert desired_tags("gd77", {"venue": "Barton Hall"}, _manifest("a.ogg"),
                        labelled=False)["a.ogg"]["album"] == "gd77"


def test_repeated_metadata_joins_like_library_layout() -> None:
    meta = {**SHOW_META, "creator": ["Jerry Garcia", "Merl Saunders"]}
    tags = desired_tags("x", meta, _manifest("a.flac"), labelled=False)["a.flac"]
    assert tags["artist"] == tags["albumartist"] == "Jerry Garcia, Merl Saunders"


def test_missing_values_are_left_out() -> None:
    tags = desired_tags("x", {"title": "T"}, _manifest("a.flac"), labelled=False)["a.flac"]
    assert tags == {"album": "T"}


def test_only_flac_mp3_and_ogg_files_are_tagged() -> None:
    desired = desired_tags(
        "x", SHOW_META, _manifest("a.flac", "b.MP3", "c.ogg", "d.shn", "e.txt", "f.ffp"),
        labelled=False,
    )
    assert set(desired) == {"a.flac", "b.MP3", "c.ogg"}


def test_track_title_and_number_come_from_the_source_listing() -> None:
    manifest = _manifest("t1.flac", "t2.flac", **{"t1.flac": ("Promised Land", "01")})
    desired = desired_tags("x", SHOW_META, manifest, labelled=False)
    assert (desired["t1.flac"]["title"], desired["t1.flac"]["tracknumber"]) == (
        "Promised Land", "01")
    assert "title" not in desired["t2.flac"] and "tracknumber" not in desired["t2.flac"]


def test_labelled_album_carries_the_identifier() -> None:
    desired = desired_tags("gd77.sbd", SHOW_META, _manifest("a.flac"), labelled=True)
    assert desired["a.flac"]["album"] == "1977-05-08 \N{EN DASH} Barton Hall [gd77.sbd]"


def test_every_clashing_show_is_labelled() -> None:
    """Two recordings that would share album artist, album, and date both get the
    label; a show that clashes with nothing keeps the bare album
    (§spec:tag-filling, every-clasher rule of §spec:library-ownership)."""
    index = TagIndex()
    index.add("gd77.sbd", SHOW_META, _manifest("a.flac"))
    index.add("gd77.aud", SHOW_META, _manifest("a.mp3"))
    index.add("gd77-05-09", {**SHOW_META, "date": "1977-05-09"}, _manifest("a.flac"))
    assert index.labelled("gd77.sbd") and index.labelled("gd77.aud")
    assert not index.labelled("gd77-05-09")
    index.remove("gd77.aud")
    assert not index.labelled("gd77.sbd")


def test_clash_labels_do_not_depend_on_order() -> None:
    shows = [("a", SHOW_META), ("b", SHOW_META), ("c", {**SHOW_META, "creator": "JGB"})]
    forward, backward = TagIndex(), TagIndex()
    for identifier, meta in shows:
        forward.add(identifier, meta, _manifest("x.flac"))
    for identifier, meta in reversed(shows):
        backward.add(identifier, meta, _manifest("x.flac"))
    assert [forward.labelled(i) for i, _ in shows] == [backward.labelled(i) for i, _ in shows]
    assert [forward.labelled(i) for i, _ in shows] == [True, True, False]


def test_a_show_with_nothing_to_tag_never_clashes() -> None:
    """Only shows shakedown tags take part: an SHN-only recording is not an album in a
    tag-driven player."""
    index = TagIndex()
    index.add("gd77.flac", SHOW_META, _manifest("a.flac"))
    index.add("gd77.shn", SHOW_META, _manifest("a.shn"))
    assert not index.labelled("gd77.flac")


def test_fingerprint_changes_only_with_the_computed_values() -> None:
    a = desired_tags("x", SHOW_META, _manifest("a.flac"), labelled=False)
    assert fingerprint(a) == fingerprint(desired_tags("x", dict(SHOW_META), _manifest("a.flac"),
                                                      labelled=False))
    assert fingerprint(a) != fingerprint(desired_tags("x", SHOW_META, _manifest("a.flac"),
                                                      labelled=True))


# --- writing one file (fill_file) ---------------------------------------------------

DESIRED = {"artist": "Grateful Dead", "albumartist": "Grateful Dead",
           "album": "1977-05-08 \N{EN DASH} Barton Hall", "date": "1977-05-08",
           "venue": "Barton Hall", "title": "Promised Land", "tracknumber": "01"}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _archived(tmp_path: Path, name: str, tags: dict[str, str] | None = None,
              **kwargs: int) -> tuple[Path, Path]:
    """An archive file of show `gd77` in collection folder `tmp_path/coll`, and the
    backup path shakedown journals it under while tagging."""
    path = make_audio(tmp_path / "coll" / "gd77" / name, tags, **kwargs)
    return path, tmp_path / "coll" / ".tagging" / "gd77" / name


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.ogg"])
def test_absent_tags_are_filled_and_recorded(tmp_path: Path, name: str) -> None:
    path, backup = _archived(tmp_path, name)
    result = fill_file(path, DESIRED, None, backup)
    assert result.status == "written"
    assert read_tags(path) == {k: [v] for k, v in DESIRED.items()}
    assert result.record == TagRecord(_md5(path), DESIRED)
    assert not backup.exists()  # the journal is cleared once the write is done


def test_tags_are_written_through_the_shared_inode(tmp_path: Path) -> None:
    """Every link to the file carries the tags and no copy is left: the library link
    and any link another tool made (§spec:tag-filling, Q-8-1)."""
    path, backup = _archived(tmp_path, "a.flac")
    library = tmp_path / "library" / "a.flac"
    library.parent.mkdir()
    os.link(path, library)
    inode = path.stat().st_ino
    fill_file(path, DESIRED, None, backup)
    assert path.stat().st_ino == library.stat().st_ino == inode
    assert path.stat().st_nlink == 2
    assert read_tags(library)["album"] == [DESIRED["album"]]


def test_a_file_in_the_fetch_folder_is_tagged_without_a_backup(tmp_path: Path) -> None:
    """A freshly downloaded file is linked nowhere yet, so it needs no journal."""
    path = make_audio(tmp_path / ".tmp-gd77" / "a.mp3")
    result = fill_file(path, DESIRED, None, None)
    assert result.status == "written" and read_tags(path)["venue"] == ["Barton Hall"]


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.ogg"])
def test_a_tag_already_present_is_never_changed(tmp_path: Path, name: str) -> None:
    path, backup = _archived(tmp_path, name, {"album": "Cornell '77", "artist": "GD"})
    result = fill_file(path, DESIRED, None, backup)
    tags = read_tags(path)
    assert (tags["album"], tags["artist"]) == (["Cornell '77"], ["GD"])
    assert tags["venue"] == ["Barton Hall"]
    assert result.record is not None
    assert "album" not in result.record.written and "artist" not in result.record.written
    assert result.record.written["venue"] == "Barton Hall"


def test_an_empty_tag_counts_as_absent_and_is_filled(tmp_path: Path) -> None:
    """Q-8-3: a field holding only empty or blank values carries nobody's work."""
    path, backup = _archived(tmp_path, "a.flac", {"artist": "  ", "album": ""})
    fill_file(path, DESIRED, None, backup)
    tags = read_tags(path)
    assert tags["artist"] == ["Grateful Dead"] and tags["album"] == [DESIRED["album"]]


def test_a_value_shakedown_wrote_is_rewritten_when_its_computed_value_changes(
    tmp_path: Path,
) -> None:
    """An album gaining its identifier label as a clash appears (§spec:tag-filling)."""
    path, backup = _archived(tmp_path, "a.flac")
    first = fill_file(path, DESIRED, None, backup)
    labelled = {**DESIRED, "album": DESIRED["album"] + " [gd77.sbd]"}
    second = fill_file(path, labelled, first.record, backup)
    assert second.status == "written"
    assert read_tags(path)["album"] == [labelled["album"]]
    assert second.record == TagRecord(_md5(path), labelled)


def test_a_value_another_tool_changed_is_left_alone_and_no_longer_ours(
    tmp_path: Path,
) -> None:
    path, backup = _archived(tmp_path, "a.ogg")
    first = fill_file(path, DESIRED, None, backup)
    f = OggVorbis(path)
    f["ALBUM"] = ["My own album name"]
    f.save()
    changed = {**DESIRED, "album": DESIRED["album"] + " [x]"}
    second = fill_file(path, changed, first.record, backup)
    assert read_tags(path)["album"] == ["My own album name"]
    assert second.record is not None and "album" not in second.record.written


def test_nothing_to_fill_writes_nothing(tmp_path: Path) -> None:
    path, backup = _archived(tmp_path, "a.mp3", DESIRED)
    before = path.stat()
    result = fill_file(path, DESIRED, None, backup)
    after = path.stat()
    assert result.status == "unchanged" and result.record is None
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not backup.exists()


def test_an_id3v23_file_stays_v23_and_keeps_its_other_frames(tmp_path: Path) -> None:
    """mutagen saves v2.4 by default; converting someone's v2.3 tag would rewrite
    their frames."""
    path, backup = _archived(tmp_path, "a.mp3", {"artist": "GD", "genre": "Rock"},
                             id3_version=3)
    fill_file(path, DESIRED, None, backup)
    id3 = ID3(path)
    assert id3.version[:2] == (2, 3)
    assert str(id3["TPE1"]) == "GD" and str(id3["TCON"]) == "Rock"
    assert str(id3["TALB"]) == DESIRED["album"]
    assert str(id3["TXXX:VENUE"]) == "Barton Hall"


def test_a_file_the_library_cannot_parse_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "coll" / "gd77" / "broken.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not really a flac file")
    result = fill_file(path, DESIRED, None, tmp_path / "coll" / ".tagging" / "gd77" / "b")
    assert result.status == "untaggable"
    assert path.read_bytes() == b"not really a flac file"


# --- crashes mid-tagging (Q-8-1: the next run restores before anything else) ---------

def _crash_at(monkeypatch: pytest.MonkeyPatch, at: str) -> None:
    def crash(stage: str) -> None:
        if stage == at:
            raise KeyboardInterrupt  # the process dies here

    monkeypatch.setattr(tagging, "_fault_point", crash)


@pytest.mark.parametrize("stage", ["backed_up", "saved"])
def test_a_crash_while_tagging_is_undone_by_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path, backup = _archived(tmp_path, "a.flac")
    library = tmp_path / "library" / "a.flac"
    library.parent.mkdir()
    os.link(path, library)
    original = path.read_bytes()
    _crash_at(monkeypatch, stage)
    with pytest.raises(KeyboardInterrupt):
        fill_file(path, DESIRED, None, backup)
    assert backup.read_bytes() == original  # the journal holds the whole original

    monkeypatch.undo()
    assert recover_interrupted_tagging(tmp_path / "coll") == [path]
    assert path.read_bytes() == library.read_bytes() == original
    assert path.stat().st_ino == library.stat().st_ino
    assert not (tmp_path / "coll" / ".tagging").exists()


def test_a_half_written_file_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process dies partway through mutagen's write: the archive file is torn
    until the next run puts the original back."""
    path, backup = _archived(tmp_path, "a.ogg")
    original = path.read_bytes()

    def torn_save(self: object, target: Path) -> None:
        with target.open("r+b") as f:
            f.write(b"\x00" * 100)
        raise KeyboardInterrupt

    monkeypatch.setattr(tagging._Tags, "save", torn_save)
    with pytest.raises(KeyboardInterrupt):
        fill_file(path, DESIRED, None, backup)
    assert path.read_bytes() != original
    recover_interrupted_tagging(tmp_path / "coll")
    assert path.read_bytes() == original


def test_an_unfinished_backup_is_discarded_and_the_file_kept(tmp_path: Path) -> None:
    """A crash while the backup itself was being written: the file was not touched
    yet, so it stays as it is."""
    path, backup = _archived(tmp_path, "a.flac")
    original = path.read_bytes()
    backup.parent.mkdir(parents=True)
    (backup.parent / f".{backup.name}.{'0' * 32}.part").write_bytes(original[:10])
    assert recover_interrupted_tagging(tmp_path / "coll") == []
    assert path.read_bytes() == original
    assert not (tmp_path / "coll" / ".tagging").exists()


def test_a_disk_error_while_writing_is_reported_and_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, backup = _archived(tmp_path, "a.ogg")
    original = path.read_bytes()

    def full(self: object, target: Path) -> None:
        with target.open("r+b") as f:
            f.write(b"\x00" * 50)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging._Tags, "save", full)
    with pytest.raises(TagWriteError, match="No space left on device"):
        fill_file(path, DESIRED, None, backup)
    assert path.read_bytes() == original
    assert not backup.exists()


def test_a_backup_that_cannot_be_made_leaves_the_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, backup = _archived(tmp_path, "a.flac")
    original = path.read_bytes()

    def full(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging.shutil, "copyfile", full)
    with pytest.raises(TagWriteError, match="No space left on device"):
        fill_file(path, DESIRED, None, backup)
    assert path.read_bytes() == original


# --- sync: a newly fetched show is tagged before it enters the archive ---------------

GD_META = {"creator": "Grateful Dead", "date": "1977-05-08", "venue": "Barton Hall",
           "title": "Live at Barton Hall"}


def _fake_show(identifier: str, tmp_path: Path, meta: dict | None = None,
               names: tuple[str, ...] = ("d1t01.flac", "d1t02.flac")) -> FakeItem:
    """A show whose files are real (untagged) audio, as the source serves them."""
    src = tmp_path / "source-bytes" / identifier
    files = [
        FakeFile(n, make_audio(src / n).read_bytes(),
                 format="Flac" if n.endswith(".flac") else "VBR MP3",
                 title=f"Track {i}", track=str(i))
        for i, n in enumerate(names, start=1)
    ]
    item = FakeItem(identifier, files, metadata={**(meta or GD_META)})
    FakePlugin.items[identifier] = item
    return item


def _archive_file(config, identifier: str, name: str) -> Path:
    return config.archive_root / "fake-src" / "coll1" / identifier / name


def _library_file(config, name: str) -> Path:
    [found] = [p for p in config.library_root.rglob(name) if p.is_file()]
    return found


def _tag_repo(config) -> TagRepo:
    return TagRepo(connect(config.state_db))


def test_a_new_show_arrives_tagged_on_a_fill_tags_collection(tmp_roots) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    item = _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0

    flac = _archive_file(config, "gd77", "d1t01.flac")
    tags = read_tags(flac)
    assert tags["artist"] == ["Grateful Dead"] and tags["albumartist"] == ["Grateful Dead"]
    assert tags["album"] == ["1977-05-08 \N{EN DASH} Barton Hall"]
    assert (tags["date"], tags["venue"]) == (["1977-05-08"], ["Barton Hall"])
    assert (tags["title"], tags["tracknumber"]) == (["Track 1"], ["1"])
    assert read_tags(_archive_file(config, "gd77", "d1t02.flac"))["title"] == ["Track 2"]
    # The library holds the same, tagged file.
    assert _library_file(config, "d1t01.flac").stat().st_ino == flac.stat().st_ino

    records = _tag_repo(config).file_records("fake-src", "coll1", "gd77")
    assert set(records) == {"d1t01.flac", "d1t02.flac"}
    assert records["d1t01.flac"].post_tag_md5 == hashlib.md5(flac.read_bytes()).hexdigest()
    assert records["d1t01.flac"].written["album"] == "1977-05-08 \N{EN DASH} Barton Hall"
    assert "gd77" in _tag_repo(config).fingerprints("fake-src", "coll1")
    # The recorded manifest stays the source's word (§spec:sync-identity).
    stored = ItemRepo(connect(config.state_db)).get("fake-src", "coll1", "gd77")
    assert stored is not None and stored.recorded_manifest is not None
    assert {f.md5 for f in stored.recorded_manifest.files} == {f.md5 for f in item.files}


def test_nothing_is_tagged_without_fill_tags(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    item = _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0
    flac = _archive_file(config, "gd77", "d1t01.flac")
    assert flac.read_bytes() == item.files[0].content
    assert _tag_repo(config).file_records("fake-src", "coll1", "gd77") == {}


def test_a_crash_while_tagging_a_download_leaves_nothing_in_the_archive(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _fake_show("gd77", tmp_roots[0].parent)

    def crash(stage: str) -> None:
        raise RuntimeError("killed mid-tagging")

    monkeypatch.setattr(tagging, "_fault_point", crash)
    assert run_sync(config) == 1
    assert not (config.archive_root / "fake-src" / "coll1" / "gd77").exists()
    assert _tag_repo(config).file_records("fake-src", "coll1", "gd77") == {}

    monkeypatch.undo()
    assert run_sync(config) == 0
    assert read_tags(_archive_file(config, "gd77", "d1t01.flac"))["venue"] == ["Barton Hall"]


def test_a_redownload_without_fill_tags_drops_the_files_tag_records(tmp_roots) -> None:
    """The new bytes are the source's: no post-tag checksum may claim them."""
    root = tmp_roots[0].parent
    _fake_show("gd77", root)
    assert run_sync(make_config(*tmp_roots, fill_tags=True)) == 0

    item = FakePlugin.items["gd77"]
    item.files[0] = FakeFile("d1t01.flac", item.files[0].content + b"\x00" * 10,
                             format="Flac", title="Track 1", track="1")
    config = make_config(*tmp_roots)
    assert run_sync(config) == 0
    assert _archive_file(config, "gd77", "d1t01.flac").read_bytes() == item.files[0].content
    records = _tag_repo(config).file_records("fake-src", "coll1", "gd77")
    assert set(records) == {"d1t02.flac"}
    assert _tag_repo(config).fingerprints("fake-src", "coll1") == {}


def test_a_disk_error_tagging_a_download_fails_the_show_for_a_retry(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _fake_show("gd77", tmp_roots[0].parent)

    def full(self: object, target: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging._Tags, "save", full)
    assert run_sync(config) == 1
    assert not (config.archive_root / "fake-src" / "coll1" / "gd77").exists()
    latest = RunRepo(connect(config.state_db)).latest("fake-src", "coll1")
    assert latest is not None
    assert latest.errors == [
        "gd77: could not fill tags in d1t01.flac ([Errno 28] No space left on device);"
        " will retry on the next run"
    ]

    monkeypatch.undo()
    assert run_sync(config) == 0
    assert read_tags(_archive_file(config, "gd77", "d1t01.flac"))["album"]


# --- verify --deep: shakedown's own tagging is not drift ------------------------------

def _verify(config, *, reconform: bool = False) -> list[tuple[str, str]]:
    """Run verify --deep (and reconform, confirmed) and return the drift it found."""
    code = run_verify(config, source_filter=None, collection_filter=None, deep=True,
                      reconform=reconform, list_drift=True, assume_yes=True)
    assert code == 0
    return sorted(
        (row["identifier"], row["file_name"])
        for row in DriftRepo(connect(config.state_db)).list("fake-src", "coll1")
    )


def _retag(path: Path) -> None:
    """Another tool edits a tag in place."""
    f = FLAC(path)
    assert f.tags is not None
    f.tags["GENRE"] = ["Rock"]
    f.save()


def test_files_shakedown_tagged_are_not_drift(tmp_roots) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0
    assert _verify(config) == []


def test_a_file_another_tool_retagged_is_reported_alone(tmp_roots) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0
    _retag(_library_file(config, "d1t02.flac"))
    assert _verify(config) == [("gd77", "d1t02.flac")]


def test_reconform_restores_source_bytes_then_refills_on_fill_tags(tmp_roots) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    item = _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0
    flac = _archive_file(config, "gd77", "d1t02.flac")
    tagged = flac.read_bytes()
    _retag(flac)
    assert _verify(config, reconform=True) == [("gd77", "d1t02.flac")]

    refilled = _archive_file(config, "gd77", "d1t02.flac")
    assert refilled.read_bytes() == tagged  # source bytes, gaps filled again
    assert "genre" not in read_tags(refilled)
    assert refilled.read_bytes() != item.files[1].content
    assert _verify(config) == []


def test_reconform_without_fill_tags_restores_source_bytes_only(tmp_roots) -> None:
    item = _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(make_config(*tmp_roots, fill_tags=True)) == 0
    config = make_config(*tmp_roots)  # fill_tags turned off
    _retag(_archive_file(config, "gd77", "d1t02.flac"))
    assert _verify(config, reconform=True) == [("gd77", "d1t02.flac")]

    assert _archive_file(config, "gd77", "d1t02.flac").read_bytes() == item.files[1].content
    assert set(_tag_repo(config).file_records("fake-src", "coll1", "gd77")) == {"d1t01.flac"}
    assert _verify(config) == []


def test_turning_fill_tags_off_keeps_tags_and_records(tmp_roots) -> None:
    _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(make_config(*tmp_roots, fill_tags=True)) == 0
    config = make_config(*tmp_roots)
    assert run_sync(config) == 0
    assert read_tags(_archive_file(config, "gd77", "d1t01.flac"))["venue"] == ["Barton Hall"]
    assert len(_tag_repo(config).file_records("fake-src", "coll1", "gd77")) == 2
    assert _verify(config) == []


def test_after_a_rebuilt_database_tagged_files_report_drift(tmp_roots) -> None:
    """Tag records are not recoverable from disk; guessing would hide other tools'
    changes (§spec:state)."""
    config = make_config(*tmp_roots, fill_tags=True)
    _fake_show("gd77", tmp_roots[0].parent)
    assert run_sync(config) == 0
    assert config.state_db is not None
    for leftover in config.state_db.parent.glob(config.state_db.name + "*"):
        leftover.unlink()
    assert run_reconcile(config) == 0
    assert _verify(config) == [("gd77", "d1t01.flac"), ("gd77", "d1t02.flac")]


# --- sync: shows already mirrored are tagged once, then revisited only on change -----

def _mirror_untagged(tmp_roots, *shows: tuple[str, dict]) -> None:
    """Shows mirrored before the collection opted in."""
    root = tmp_roots[0].parent
    for identifier, meta in shows:
        _fake_show(identifier, root, meta)
    assert run_sync(make_config(*tmp_roots)) == 0


def _album(config, identifier: str, name: str = "d1t01.flac") -> list[str]:
    return read_tags(_archive_file(config, identifier, name))["album"]


ALBUM = "1977-05-08 \N{EN DASH} Barton Hall"


def test_opting_in_tags_mirrored_shows_and_labels_both_recordings(tmp_roots) -> None:
    _mirror_untagged(tmp_roots, ("gd77.sbd", GD_META), ("gd77.aud", GD_META),
                     ("gd77-05-09", {**GD_META, "date": "1977-05-09"}))
    # One file already carries its uploader's tags.
    mine = _archive_file(make_config(*tmp_roots), "gd77-05-09", "d1t02.flac")
    f = FLAC(mine)
    f.add_tags()
    assert f.tags is not None
    f.tags["ALBUM"], f.tags["ARTIST"] = ["Uploader's album"], ["The Dead"]
    f.save()
    inode = mine.stat().st_ino
    downloads = dict(FakePlugin.fetch_count)

    config = make_config(*tmp_roots, fill_tags=True)
    assert run_sync(config) == 0

    assert FakePlugin.fetch_count == downloads  # tagging never re-downloads
    assert _album(config, "gd77.sbd") == [f"{ALBUM} [gd77.sbd]"]
    assert _album(config, "gd77.aud") == [f"{ALBUM} [gd77.aud]"]
    assert _album(config, "gd77-05-09") == ["1977-05-09 \N{EN DASH} Barton Hall"]
    tags = read_tags(mine)
    assert (tags["album"], tags["artist"]) == (["Uploader's album"], ["The Dead"])
    assert tags["venue"] == ["Barton Hall"]
    # Written through the shared inode: the library link carries the tags, no copies.
    assert mine.stat().st_ino == inode and mine.stat().st_nlink == 2
    for path in config.library_root.rglob("*.flac"):
        assert path.stat().st_nlink == 2
    assert not (config.archive_root / "fake-src" / "coll1" / ".tagging").exists()
    assert _verify(config) == []


def test_a_second_sync_reads_no_file_of_a_tagged_show(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mirror_untagged(tmp_roots, ("gd77.sbd", GD_META), ("gd77.aud", GD_META))
    config = make_config(*tmp_roots, fill_tags=True)
    assert run_sync(config) == 0
    downloads = dict(FakePlugin.fetch_count)

    opened: list[Path] = []
    real_init = tagging._Tags.__init__

    def counting_init(self: tagging._Tags, path: Path) -> None:
        opened.append(path)
        real_init(self, path)

    monkeypatch.setattr(tagging._Tags, "__init__", counting_init)
    assert run_sync(config) == 0
    assert opened == [] and FakePlugin.fetch_count == downloads
    assert _verify(config) == []


def test_a_clash_ending_takes_the_label_off_the_survivor(tmp_roots) -> None:
    _mirror_untagged(tmp_roots, ("gd77.sbd", GD_META), ("gd77.aud", GD_META))
    config = make_config(*tmp_roots, fill_tags=True, prune_disappeared=True)
    assert run_sync(config) == 0
    assert _album(config, "gd77.sbd") == [f"{ALBUM} [gd77.sbd]"]

    del FakePlugin.items["gd77.aud"]
    assert run_sync(config) == 0
    assert _album(config, "gd77.sbd") == [ALBUM]
    assert _verify(config) == []


def test_an_album_someone_else_changed_is_left_alone(tmp_roots) -> None:
    _mirror_untagged(tmp_roots, ("gd77.sbd", GD_META))
    config = make_config(*tmp_roots, fill_tags=True)
    assert run_sync(config) == 0
    mine = _library_file(config, "d1t01.flac")
    f = FLAC(mine)
    assert f.tags is not None
    f.tags["ALBUM"] = ["Cornell '77 (my edit)"]
    f.save()

    _fake_show("gd77.aud", tmp_roots[0].parent)  # a clash appears
    assert run_sync(config) == 0
    assert _album(config, "gd77.sbd") == ["Cornell '77 (my edit)"]
    assert _album(config, "gd77.sbd", "d1t02.flac") == [f"{ALBUM} [gd77.sbd]"]
    assert _album(config, "gd77.aud") == [f"{ALBUM} [gd77.aud]"]
    assert _verify(config) == [("gd77.sbd", "d1t01.flac")]  # still reported as drift


def test_a_metadata_refresh_rewrites_the_venue_and_album(tmp_roots) -> None:
    """`sync --refresh-metadata` re-resolves a preserve-opted collection's metadata
    (§spec:metadata-preservation); the tags shakedown filled follow it."""
    _mirror_untagged(tmp_roots, ("gd77", GD_META))
    config = make_config(*tmp_roots, fill_tags=True, preserve_source_metadata=True)
    assert run_sync(config) == 0
    FakePlugin.items["gd77"].metadata["venue"] = "Barton Hall, Cornell University"
    assert run_sync(config, refresh_metadata=True) == 0
    tags = read_tags(_archive_file(config, "gd77", "d1t01.flac"))
    assert tags["venue"] == ["Barton Hall, Cornell University"]
    assert tags["album"] == ["1977-05-08 \N{EN DASH} Barton Hall, Cornell University"]
    assert _verify(config) == []


def test_a_disk_error_tagging_a_mirrored_show_is_an_issue_retried_next_run(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mirror_untagged(tmp_roots, ("gd77", GD_META))
    config = make_config(*tmp_roots, fill_tags=True)
    original = _archive_file(config, "gd77", "d1t01.flac").read_bytes()

    def full(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging.shutil, "copyfile", full)
    assert run_sync(config) == 1
    assert _archive_file(config, "gd77", "d1t01.flac").read_bytes() == original
    latest = RunRepo(connect(config.state_db)).latest("fake-src", "coll1")
    assert latest is not None and latest.items_failed == 1
    assert latest.errors == [
        "gd77: could not fill tags in d1t01.flac ([Errno 28] No space left on device);"
        " will retry on the next run",
        "gd77: could not fill tags in d1t02.flac ([Errno 28] No space left on device);"
        " will retry on the next run",
    ]

    monkeypatch.undo()
    assert run_sync(config) == 0
    assert _album(config, "gd77") == [ALBUM]


def test_a_backup_left_by_a_crash_is_restored_before_the_run_goes_on(tmp_roots) -> None:
    """The next run puts the original back before anything else, then tags again."""
    _mirror_untagged(tmp_roots, ("gd77", GD_META))
    config = make_config(*tmp_roots, fill_tags=True)
    archive_file = _archive_file(config, "gd77", "d1t01.flac")
    original = archive_file.read_bytes()
    backup = config.archive_root / "fake-src" / "coll1" / ".tagging" / "gd77" / "d1t01.flac"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original)
    with archive_file.open("r+b") as torn:
        torn.write(b"\x00" * 64)  # a write the crash cut short

    assert run_sync(config) == 0
    assert _album(config, "gd77") == [ALBUM]
    assert not backup.parent.parent.exists()
    assert _verify(config) == []


def test_restage_restores_a_backup_left_by_a_crash(tmp_roots) -> None:
    _mirror_untagged(tmp_roots, ("gd77", GD_META))
    config = make_config(*tmp_roots)
    archive_file = _archive_file(config, "gd77", "d1t01.flac")
    original = archive_file.read_bytes()
    backup = config.archive_root / "fake-src" / "coll1" / ".tagging" / "gd77" / "d1t01.flac"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original)
    with archive_file.open("r+b") as torn:
        torn.write(b"\x00" * 64)
    assert run_restage(config, source_filter=None, collection_filter=None) == 0
    assert archive_file.read_bytes() == original
    assert _library_file(config, "d1t01.flac").read_bytes() == original


# --- a real kill mid-tagging (fault injection in a child process) --------------------

@pytest.mark.parametrize("stage", ["backed_up", "torn", "saved"])
def test_killing_sync_mid_tagging_leaves_no_half_written_file(
    tmp_roots, tmp_path: Path, stage: str
) -> None:
    """SIGKILL during a tag write: every archive file and its library link stay one
    file; the original survives in the backup; the next run restores it before
    anything else and then tags it (§spec:tag-filling, Q-8-1)."""
    _mirror_untagged(tmp_roots, ("gd77", GD_META))
    config = make_config(*tmp_roots, fill_tags=True)
    write_config(tmp_path / "cfg.yaml", config)
    originals = {
        name: _archive_file(config, "gd77", name).read_bytes()
        for name in ("d1t01.flac", "d1t02.flac")
    }
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({
        "audio_items": {"gd77": {"metadata": GD_META, "files": sorted(originals)}},
        "kill_at": stage,
    }))
    child = subprocess.run(
        [sys.executable, "-m", "tests.serve_child", "--config", str(tmp_path / "cfg.yaml"),
         "sync"],
        env={**os.environ, SPEC_ENV: str(spec)}, cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, timeout=120,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()

    backup = config.archive_root / "fake-src" / "coll1" / ".tagging" / "gd77" / "d1t01.flac"
    assert backup.read_bytes() == originals["d1t01.flac"]  # the whole original is kept
    first = _archive_file(config, "gd77", "d1t01.flac")
    assert first.stat().st_ino == _library_file(config, "d1t01.flac").stat().st_ino

    assert run_sync(config) == 0
    assert not backup.parent.parent.exists()
    for name in originals:
        archive_file = _archive_file(config, "gd77", name)
        assert read_tags(archive_file)["album"] == [ALBUM]
        assert archive_file.stat().st_ino == _library_file(config, name).stat().st_ino
        assert archive_file.stat().st_nlink == 2
    assert _verify(config) == []
