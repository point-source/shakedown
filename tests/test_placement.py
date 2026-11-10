"""The library placement engine: ownership, clash separation, cleanup, and adoption
(§spec:library-ownership, §spec:library-cleanup)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from shakedown.config import Config
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile
from shakedown.placement import (
    LibraryPlacement,
    foreign_file_issue,
    removal_issue,
)
from shakedown.state import ItemRepo, PlacementRepo
from tests.conftest import make_config

SRC, COLL = "fake-src", "coll1"


class Lib:
    """A collection's archive + DB, seeded directly, with a placement engine on top."""

    def __init__(self, tmp_path: Path, layout: str = "{title}", **kw) -> None:
        self.archive = tmp_path / "archive"
        self.library = tmp_path / "library"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.library.mkdir(parents=True, exist_ok=True)
        self.layout = layout
        self.kw = kw
        self.config = self._config()
        self.items = ItemRepo(connect(self.config.state_db))  # type: ignore[arg-type]
        self.repo = PlacementRepo(self.items.conn)

    def _config(self) -> Config:
        return make_config(self.archive, self.library, library_layout=self.layout, **self.kw)

    def relayout(self, layout: str) -> None:
        self.layout = layout
        self.config = self._config()

    @property
    def base(self) -> Path:
        return self.library / SRC / COLL

    def show(self, identifier: str, files: dict[str, bytes], **metadata) -> Item:
        path = self.archive / SRC / COLL / identifier
        path.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(content)
        item = Item(
            source_name=SRC, collection_name=COLL, identifier=identifier,
            status=ItemStatus.COMPLETE, archive_path=path,
            source_metadata={"identifier": identifier, **metadata},
            recorded_manifest=Manifest(
                files=tuple(ManifestFile(n, len(c), "x") for n, c in files.items())
            ),
        )
        self.items.upsert(item)
        return item

    def engine(self) -> LibraryPlacement:
        collection = self.config.sources[0].collections[0]
        return LibraryPlacement(self.config, collection, SRC, self.items)

    def finish(self):
        return self.engine().finish()

    def tree(self) -> list[str]:
        """Every library file, relative to the collection folder."""
        return sorted(
            str(p.relative_to(self.base)) for p in self.base.rglob("*") if p.is_file()
        )


# --- WS2: deterministic clash separation -------------------------------------------


def test_clashing_shows_each_get_a_labelled_folder(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="Barton Hall")
    lib.show("gd-b", {"b.flac": b"b"}, title="Barton Hall")
    lib.show("gd-c", {"c.flac": b"c"}, title="Winterland")

    report = lib.finish()

    assert lib.tree() == [
        "Barton Hall [gd-a]/a.flac",
        "Barton Hall [gd-b]/b.flac",
        "Winterland/c.flac",
    ]
    assert report.separated == [
        ("gd-a", lib.base / "Barton Hall [gd-a]"),
        ("gd-b", lib.base / "Barton Hall [gd-b]"),
    ]
    assert report.issues == {}
    assert lib.repo.list_separated(SRC, COLL) == [
        ("gd-a", "Barton Hall [gd-a]"), ("gd-b", "Barton Hall [gd-b]"),
    ]


def test_same_file_names_never_merge_or_drop(tmp_path: Path) -> None:
    """Two tapes of one show with identical track names both reach the library."""
    lib = Lib(tmp_path)
    lib.show("gd-a", {"track.flac": b"a"}, title="1977")
    lib.show("gd-b", {"track.flac": b"b"}, title="1977")

    lib.finish()

    assert (lib.base / "1977 [gd-a]" / "track.flac").read_bytes() == b"a"
    assert (lib.base / "1977 [gd-b]" / "track.flac").read_bytes() == b"b"


def test_label_goes_on_the_last_segment(tmp_path: Path) -> None:
    lib = Lib(tmp_path, layout="{year}/{title}")
    lib.show("gd-a", {"a.flac": b"a"}, year="1977", title="Barton Hall")
    lib.show("gd-b", {"b.flac": b"b"}, year="1977", title="Barton Hall")

    lib.finish()

    assert lib.tree() == ["1977/Barton Hall [gd-a]/a.flac", "1977/Barton Hall [gd-b]/b.flac"]


def test_separation_does_not_depend_on_arrival_order(tmp_path: Path) -> None:
    """A show arriving one at a time (incremental sync) ends where a full pass puts it."""
    one = Lib(tmp_path / "one")
    engine = one.engine()
    engine.begin()
    one.show("gd-b", {"b.flac": b"b"}, title="X")
    engine.update("gd-b")
    assert one.tree() == ["X/b.flac"]
    one.show("gd-a", {"a.flac": b"a"}, title="X")
    engine.update("gd-a")
    # The first recording moved once, to its labelled name, as soon as the second landed.
    assert one.tree() == ["X [gd-a]/a.flac", "X [gd-b]/b.flac"]
    engine.finish()

    two = Lib(tmp_path / "two")
    two.show("gd-a", {"a.flac": b"a"}, title="X")
    two.show("gd-b", {"b.flac": b"b"}, title="X")
    two.finish()

    assert one.tree() == two.tree()


def test_clash_ending_returns_survivor_to_bare_name(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.show("gd-b", {"b.flac": b"b"}, title="X")
    lib.finish()

    lib.items.delete(SRC, COLL, "gd-b")  # forgotten / pruned
    report = lib.finish()

    assert lib.tree() == ["X/a.flac"]
    assert not (lib.base / "X [gd-b]").exists()
    assert not (lib.base / "X [gd-a]").exists()
    assert report.separated == []
    assert lib.repo.list_separated(SRC, COLL) == []


def test_repeated_passes_change_nothing(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.show("gd-b", {"b.flac": b"b"}, title="X")
    lib.finish()
    inodes = {p: (lib.base / p).stat().st_ino for p in lib.tree()}

    writes = lib.items.conn.total_changes
    report = lib.finish()

    assert {p: (lib.base / p).stat().st_ino for p in lib.tree()} == inodes
    assert report.links_created == 0 and report.files_removed == 0
    # A pass over an up-to-date library writes nothing (it runs on every sync).
    assert lib.items.conn.total_changes == writes


def test_passthrough_mirrors_archive(tmp_path: Path) -> None:
    lib = Lib(tmp_path, layout="passthrough")
    lib.show("gd-a", {"a.flac": b"a", "disc2/b.flac": b"b"}, title="X")
    lib.finish()
    assert lib.tree() == ["gd-a/a.flac", "gd-a/disc2/b.flac"]


def test_unrenderable_layout_removes_nothing(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.finish()

    lib.relayout("{title|sanitise}")
    report = lib.finish()

    assert report.render_error is not None
    assert report.render_error[0] == "gd-a"
    assert "sanitise" in report.render_error[1]
    assert lib.tree() == ["X/a.flac"]


def test_unsafe_show_is_refused_and_others_stage(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.show("gd-bad", {"../../escape": b"evil"}, title="Y")

    report = lib.finish()

    assert lib.tree() == ["X/a.flac"]
    assert list(report.issues) == ["gd-bad"]
    assert "gd-bad" not in report.folder_of
    assert report.folder_of["gd-a"] == lib.base / "X"


def test_sidecar_is_placed_when_preserved(tmp_path: Path) -> None:
    lib = Lib(tmp_path, preserve_source_metadata=True)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.finish()
    assert lib.tree() == ["X/a.flac", "X/metadata.json"]
    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == {"X/a.flac", "X/metadata.json"}


# --- WS3: never replace a foreign file ---------------------------------------------


def test_foreign_file_is_left_and_reported(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a", "b.flac": b"b"}, title="X")
    mine = lib.base / "X" / "a.flac"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"mine")
    ino = mine.stat().st_ino

    report = lib.finish()

    assert mine.read_bytes() == b"mine" and mine.stat().st_ino == ino
    assert (lib.base / "X" / "b.flac").read_bytes() == b"b"  # rest of the show staged
    assert report.issues == {"gd-a": [foreign_file_issue("gd-a", mine)]}
    assert foreign_file_issue("gd-a", mine) == (
        f"gd-a: {mine} already holds a file shakedown did not place; "
        "left it and skipped that link"
    )
    assert lib.repo.list_unplaced(SRC, COLL) == ["X/a.flac"]
    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == {"X/b.flac"}


def test_stale_link_after_refetch_is_replaced(tmp_path: Path) -> None:
    """A re-fetch swaps the archive copy (new inode); shakedown's own old link is
    replaced, not reported as foreign."""
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"old"}, title="X")
    lib.finish()
    assert item.archive_path is not None
    (item.archive_path / "a.flac").unlink()
    (item.archive_path / "a.flac").write_bytes(b"new")

    report = lib.finish()

    assert report.issues == {}
    assert (lib.base / "X" / "a.flac").read_bytes() == b"new"


def test_unrecorded_link_to_archive_copy_is_adopted(tmp_path: Path) -> None:
    """A link shakedown made but never recorded (crash between link and record) is the
    archive copy itself: recorded silently, never reported."""
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.engine().finish()  # adoption done; empty library
    lib.repo.remove_placement(SRC, COLL, "X/a.flac")  # never recorded
    assert item.archive_path is not None
    assert (lib.base / "X" / "a.flac").exists()

    report = lib.finish()

    assert report.issues == {}
    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == {"X/a.flac"}


# --- WS4: placement-driven cleanup -------------------------------------------------


def test_layout_change_moves_every_show_and_leaves_nothing_behind(tmp_path: Path) -> None:
    lib = Lib(tmp_path, layout="{title}")
    lib.show("gd-a", {"a.flac": b"a"}, title="X", year="1977")
    lib.show("gd-b", {"b.flac": b"b"}, title="Y", year="1978")
    lib.finish()

    lib.relayout("{year}/{title}")
    lib.finish()

    assert lib.tree() == ["1977/X/a.flac", "1978/Y/b.flac"]
    assert sorted(os.listdir(lib.base)) == ["1977", "1978"]
    single_links = [p for p in lib.base.rglob("*") if p.is_file() and p.stat().st_nlink == 1]
    assert single_links == []


def test_metadata_change_moves_show(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="Old Name")
    lib.finish()
    item.source_metadata["title"] = "New Name"
    lib.items.upsert(item)

    lib.finish()

    assert lib.tree() == ["New Name/a.flac"]


def test_dropped_file_and_dropped_sidecar_are_removed(tmp_path: Path) -> None:
    lib = Lib(tmp_path, preserve_source_metadata=True)
    lib.show("gd-a", {"a.flac": b"a", "b.flac": b"b"}, title="X")
    lib.finish()
    assert lib.tree() == ["X/a.flac", "X/b.flac", "X/metadata.json"]

    lib.kw = {}
    lib.config = lib._config()  # preservation turned off
    lib.show("gd-a", {"a.flac": b"a"}, title="X")  # re-fetch drops b.flac
    lib.finish()

    assert lib.tree() == ["X/a.flac"]


def test_foreign_file_keeps_its_folder_when_show_moves(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="Old")
    lib.finish()
    cover = lib.base / "Old" / "cover.jpg"
    cover.write_bytes(b"art")
    item.source_metadata["title"] = "New"
    lib.items.upsert(item)

    lib.finish()

    assert cover.read_bytes() == b"art"
    assert lib.tree() == ["New/a.flac", "Old/cover.jpg"]
    assert lib.repo.list_unplaced(SRC, COLL) == ["Old/cover.jpg"]

    cover.unlink()  # the operator deletes it
    lib.finish()
    assert lib.repo.list_unplaced(SRC, COLL) == []


def test_file_replaced_at_a_placed_path_is_not_removed(tmp_path: Path) -> None:
    """The operator swapped a placed link for their own file: when the show moves, that
    file is not shakedown's to remove — it stays and is reported."""
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="Old")
    lib.finish()
    edited = lib.base / "Old" / "a.flac"
    edited.unlink()
    edited.write_bytes(b"my edit")
    item.source_metadata["title"] = "New"
    lib.items.upsert(item)

    report = lib.finish()

    assert edited.read_bytes() == b"my edit"
    assert lib.tree() == ["New/a.flac", "Old/a.flac"]
    assert lib.repo.list_unplaced(SRC, COLL) == ["Old/a.flac"]
    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == {"New/a.flac"}
    assert report.issues == {}


def test_file_replaced_at_a_current_path_is_not_relinked(tmp_path: Path) -> None:
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.finish()
    edited = lib.base / "X" / "a.flac"
    edited.unlink()
    edited.write_bytes(b"my edit")

    report = lib.finish()

    assert edited.read_bytes() == b"my edit"
    assert report.issues == {"gd-a": [foreign_file_issue("gd-a", edited)]}
    assert lib.repo.list_unplaced(SRC, COLL) == ["X/a.flac"]


def test_show_without_archive_copy_loses_its_placement(tmp_path: Path) -> None:
    """A pruned show (archive copy gone) has every placed file removed."""
    import shutil

    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.show("gd-b", {"b.flac": b"b"}, title="Y")
    lib.finish()
    assert item.archive_path is not None
    shutil.rmtree(item.archive_path)
    item.archive_path = None
    item.status = ItemStatus.PRUNED
    lib.items.upsert(item)

    lib.finish()

    assert lib.tree() == ["Y/b.flac"]
    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == set()


def test_missing_archive_file_keeps_its_library_copy(tmp_path: Path) -> None:
    """A recorded file whose archive copy is missing is not "no longer placed": its
    library link may be the last copy of those bytes."""
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.finish()
    assert item.archive_path is not None
    (item.archive_path / "a.flac").unlink()

    lib.finish()

    assert (lib.base / "X" / "a.flac").read_bytes() == b"a"


def test_update_tolerates_a_refused_show(tmp_path: Path) -> None:
    """A show whose folder would leave the library is refused during a sync too; the
    run carries on and the full pass reports it."""
    lib = Lib(tmp_path, layout="../{title}")
    engine = lib.engine()
    engine.begin()
    lib.show("gd-a", {"a.flac": b"a"}, title="X")

    engine.update("gd-a")
    report = engine.finish()

    assert list(report.issues) == ["gd-a"]
    assert not (tmp_path / "library" / "fake-src" / "X").exists()


def test_failed_removal_stays_recorded_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = Lib(tmp_path)
    item = lib.show("gd-a", {"a.flac": b"a"}, title="Old")
    lib.finish()
    item.source_metadata["title"] = "New"
    lib.items.upsert(item)
    old = lib.base / "Old" / "a.flac"
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == old:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    report = lib.finish()

    assert old.exists()
    assert (lib.base / "New" / "a.flac").exists()
    assert report.issues == {
        "gd-a": [removal_issue("gd-a", old, PermissionError(13, "Permission denied"))]
    }
    assert lib.repo.owner_of(SRC, COLL, "Old/a.flac") == "gd-a"

    assert report.issues["gd-a"][0] == (
        f"gd-a: could not remove {old} ([Errno 13] Permission denied); "
        "will retry on the next run"
    )

    monkeypatch.undo()
    report = lib.finish()
    assert report.issues == {}
    assert lib.tree() == ["New/a.flac"]


# --- WS5: upgrade / rebuild adoption -----------------------------------------------


def test_first_pass_adopts_existing_links_and_clears_duplicates(tmp_path: Path) -> None:
    """The ARU case: an old version linked one show into two folders. The first pass
    adopts both links by inode, then removes the one not at the current path."""
    lib = Lib(tmp_path)
    item = lib.show("aru-1", {"t1.flac": b"one"}, title="Current")
    assert item.archive_path is not None
    for folder in ("Current", "Old Name"):
        (lib.base / folder).mkdir(parents=True)
        os.link(item.archive_path / "t1.flac", lib.base / folder / "t1.flac")
    stray = lib.base / "Old Name" / "t0.flac"
    stray.write_bytes(b"replaced long ago")  # single link: archive copy since replaced

    report = lib.finish()

    assert lib.tree() == ["Current/t1.flac", "Old Name/t0.flac"]
    assert stray.read_bytes() == b"replaced long ago"
    assert lib.repo.list_unplaced(SRC, COLL) == ["Old Name/t0.flac"]
    assert lib.repo.is_adopted(SRC, COLL)
    assert report.issues == {}


def test_adoption_walks_the_library_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shakedown.placement as placement_module

    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    walks: list[Path] = []
    real_walk = placement_module.os.walk

    def walk(top, *args, **kwargs):
        walks.append(Path(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(placement_module.os, "walk", walk)
    lib.base.mkdir(parents=True)  # an existing library folder
    lib.finish()
    lib.finish()
    assert walks == [lib.base]


def test_rebuilt_database_adopts_again(tmp_path: Path) -> None:
    """A database rebuilt from scratch has no placement record and adopts the existing
    links the same way an upgrade does (§spec:state)."""
    lib = Lib(tmp_path)
    lib.show("gd-a", {"a.flac": b"a"}, title="X")
    lib.finish()
    for table in ("library_placements", "library_folders", "library_adoption"):
        lib.items.conn.execute(f"DELETE FROM {table}")

    lib.finish()

    assert set(lib.repo.placed_inodes(SRC, COLL, "gd-a")) == {"X/a.flac"}
    assert lib.repo.list_unplaced(SRC, COLL) == []
