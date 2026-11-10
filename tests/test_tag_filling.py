"""Opt-in tag filling (§spec:tag-filling, §req:success-criteria #25)."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from mutagen.id3 import ID3
from mutagen.oggvorbis import OggVorbis

from shakedown import tagging
from shakedown.config import CollectionConfig
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.state import ItemRepo, TagRecord, TagRepo
from shakedown.sync import _record_disappeared
from shakedown.tagging import TagIndex, TagWriteError, desired_tags, fill_file, fingerprint
from tests.audio_fixtures import make_audio, read_tags


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


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.ogg"])
def test_absent_tags_are_filled_and_recorded(tmp_path: Path, name: str) -> None:
    path = make_audio(tmp_path / "show" / name)
    result = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    assert result.status == "written"
    assert read_tags(path) == {k: [v] for k, v in DESIRED.items()}
    assert result.record == TagRecord(_md5(path), DESIRED)
    assert list((tmp_path / ".tagging").iterdir()) == []  # no temp copy left behind


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.ogg"])
def test_a_tag_already_present_is_never_changed(tmp_path: Path, name: str) -> None:
    path = make_audio(tmp_path / name, {"album": "Cornell '77", "artist": "GD"})
    result = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    tags = read_tags(path)
    assert (tags["album"], tags["artist"]) == (["Cornell '77"], ["GD"])
    assert tags["venue"] == ["Barton Hall"]
    assert result.record is not None
    assert "album" not in result.record.written and "artist" not in result.record.written
    assert result.record.written["venue"] == "Barton Hall"


def test_a_value_shakedown_wrote_is_rewritten_when_its_computed_value_changes(
    tmp_path: Path,
) -> None:
    """An album gaining its identifier label as a clash appears (§spec:tag-filling)."""
    path = make_audio(tmp_path / "a.flac")
    first = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    labelled = {**DESIRED, "album": DESIRED["album"] + " [gd77.sbd]"}
    second = fill_file(path, labelled, first.record, tmp_path / ".tagging")
    assert second.status == "written"
    assert read_tags(path)["album"] == [labelled["album"]]
    assert second.record == TagRecord(_md5(path), labelled)


def test_a_value_another_tool_changed_is_left_alone_and_no_longer_ours(
    tmp_path: Path,
) -> None:
    path = make_audio(tmp_path / "a.ogg")
    first = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    f = OggVorbis(path)
    f["ALBUM"] = ["My own album name"]
    f.save()
    changed = {**DESIRED, "album": DESIRED["album"] + " [x]"}
    second = fill_file(path, changed, first.record, tmp_path / ".tagging")
    assert read_tags(path)["album"] == ["My own album name"]
    assert second.record is not None and "album" not in second.record.written


def test_nothing_to_fill_writes_nothing(tmp_path: Path) -> None:
    path = make_audio(tmp_path / "a.mp3", DESIRED)
    before = path.stat()
    result = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    after = path.stat()
    assert result.status == "unchanged" and result.record is None
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_an_id3v23_file_stays_v23_and_keeps_its_other_frames(tmp_path: Path) -> None:
    """mutagen saves v2.4 by default; converting someone's v2.3 tag would rewrite
    their frames."""
    path = make_audio(tmp_path / "a.mp3", {"artist": "GD", "genre": "Rock"}, id3_version=3)
    fill_file(path, DESIRED, None, tmp_path / ".tagging")
    id3 = ID3(path)
    assert id3.version[:2] == (2, 3)
    assert str(id3["TPE1"]) == "GD" and str(id3["TCON"]) == "Rock"
    assert str(id3["TALB"]) == DESIRED["album"]
    assert str(id3["TXXX:VENUE"]) == "Barton Hall"


def test_a_file_the_library_cannot_parse_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not really a flac file")
    result = fill_file(path, DESIRED, None, tmp_path / ".tagging")
    assert result.status == "untaggable"
    assert path.read_bytes() == b"not really a flac file"


def test_a_crash_before_the_swap_leaves_the_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_audio(tmp_path / "a.flac")
    original = path.read_bytes()

    def crash(stage: str) -> None:
        if stage == "saved":
            raise KeyboardInterrupt

    monkeypatch.setattr(tagging, "_fault_point", crash)
    with pytest.raises(KeyboardInterrupt):
        fill_file(path, DESIRED, None, tmp_path / ".tagging")
    assert path.read_bytes() == original


def test_a_crash_after_the_swap_leaves_the_whole_tagged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_audio(tmp_path / "a.flac")

    def crash(stage: str) -> None:
        if stage == "replaced":
            raise KeyboardInterrupt

    monkeypatch.setattr(tagging, "_fault_point", crash)
    with pytest.raises(KeyboardInterrupt):
        fill_file(path, DESIRED, None, tmp_path / ".tagging")
    assert read_tags(path) == {k: [v] for k, v in DESIRED.items()}


def test_a_disk_error_while_writing_is_reported_and_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_audio(tmp_path / "a.ogg")
    original = path.read_bytes()

    def full(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging.shutil, "copyfile", full)
    with pytest.raises(TagWriteError, match="No space left on device"):
        fill_file(path, DESIRED, None, tmp_path / ".tagging")
    assert path.read_bytes() == original
