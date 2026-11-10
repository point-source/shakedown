"""Opt-in tag filling (§spec:tag-filling, §req:success-criteria #25)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shakedown.config import CollectionConfig
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.state import ItemRepo, TagRecord, TagRepo
from shakedown.sync import _record_disappeared
from shakedown.tagging import TagIndex, desired_tags, fingerprint


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
