"""Deleting a removed collection's files (§spec:collection-removal "Two deletes"):
the `purge` run, under the writer lock. A library delete removes what the
placement records say Shakedown put in the library and keeps the archive; an
everything delete also removes the archive folder and the shows' records. It is
`shakedown purge` on the CLI and shows as kind `purge` in the live view and the
run history."""
from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from shakedown import purge
from shakedown.activity import ActivityRepo
from shakedown.cli import main
from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.history import run_history
from shakedown.lock import writer_lock
from shakedown.models import OperationType
from shakedown.state import (
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    PlacementRepo,
    RescheduledRepo,
)
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

S, C = "fake-src", "coll1"
SHOWS = {"show-a": b"a" * 1000, "show-b": b"b" * 3000}


@pytest.fixture()
def synced(tmp_roots: tuple[Path, Path]) -> Config:
    """coll1 synced: two shows in the archive and linked into the library."""
    config = make_config(*tmp_roots)
    for identifier, content in SHOWS.items():
        FakePlugin.items[identifier] = FakeItem(
            identifier, files=[FakeFile("t1.flac", content, format="Flac")]
        )
    assert run_sync(config) == 0
    return config


def _removed(config: Config) -> Config:
    """The same roots with coll1 gone from the file: another collection remains."""
    return Config(
        archive_root=config.archive_root, library_root=config.library_root,
        state_db=config.state_db,
        sources=[SourceConfig(name=S, type="fake", collections=[
            CollectionConfig(name="other", query="*", library_layout="passthrough"),
        ])],
    )


def _file(config: Config, removed: bool = True) -> Path:
    return write_config(config.archive_root.parent / "shakedown.yaml",
                        _removed(config) if removed else config)


def _purge(config: Config, *, everything: bool = False) -> int:
    removed = _removed(config)
    return purge.run_purge(removed, _file(config), S, C, everything=everything)


def _library(config: Config) -> Path:
    return config.library_root / S / C


def _archive(config: Config) -> Path:
    return config.archive_root / S / C


def _entry(config: Config) -> dict[str, Any]:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        [entry] = [e for e in ActivityRepo(conn).since(datetime.min, datetime.now())
                   if e["kind"] == "purge"]
    return entry


def _unconfigured(config: Config, file: Config | None = None) -> list[dict[str, Any]]:
    """The unconfigured list against ``file``, the config with coll1 removed unless
    given."""
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        return purge.unconfigured(conn, purge.configured_pairs(file or _removed(config)))


def _pending(config: Config) -> None:
    """A rescheduled sync and a handoff notice still owed for coll1."""
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RescheduledRepo(conn).record(S, C, datetime.now())
        PendingHandoffRepo(conn).mark_undelivered(S, C, ["show-a"], "down", datetime.now())


def _owed(config: Config) -> tuple[Any, list[Any]]:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        return RescheduledRepo(conn).get(S, C), PendingHandoffRepo(conn).list(S, C)


def test_a_library_delete_keeps_the_archive_and_files_a_user_added(synced: Config) -> None:
    config = synced
    _pending(config)
    library, archive = _library(config), _archive(config)
    placed = sorted(p for p in library.rglob("*") if p.is_file())
    assert len(placed) == 2
    mine = library / "show-a" / "my-notes.txt"
    mine.write_text("mine")

    assert _purge(config) == 0

    assert [p for p in library.rglob("*") if p.is_file()] == [mine]
    assert not (library / "show-b").exists()
    assert sorted(p.name for p in archive.iterdir() if not p.name.startswith(".")) == [
        "show-a", "show-b"]
    assert (archive / "show-b" / "t1.flac").read_bytes() == SHOWS["show-b"]
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        assert PlacementRepo(conn).placed_identifiers(S, C) == set()
        assert PlacementRepo(conn).folders(S, C) == {}
        assert len(list(ItemRepo(conn).list_for_collection(S, C))) == 2
    assert _owed(config) == (None, [])
    # Still listed, now with nothing in the library.
    [listed] = _unconfigured(config)
    assert (listed["shows"], listed["library_bytes"]) == (2, 0)
    assert listed["archive_bytes"] == sum(len(c) for c in SHOWS.values())
    entry = _entry(config)
    assert (entry["source"], entry["collection"]) == (S, C)
    assert (entry["state"], entry["result"]) == ("ended", "completed")
    # Library files are hardlinks into the archive: deleting them frees no disk.
    assert entry["bytes_freed"] == 0


def test_an_emptied_collection_library_folder_goes_too(synced: Config) -> None:
    assert _purge(synced) == 0
    assert not _library(synced).exists()
    assert (synced.library_root / S).is_dir()


def test_show_folders_already_emptied_go_too(synced: Config) -> None:
    """A show whose files are already gone (deleted by hand, or by a purge stopped
    after it) still leaves its folder for a later purge to sweep."""
    for f in (_library(synced) / "show-a").iterdir():
        f.unlink()

    assert _purge(synced) == 0

    assert not _library(synced).exists()


def test_a_purge_stopped_midway_is_finished_by_the_next(
    synced: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = purge.stopping.requested
    calls = iter([False, True])  # stop after the first show
    monkeypatch.setattr(purge.stopping, "requested", lambda: next(calls, True))
    _purge(synced)
    assert _library(synced).exists()

    monkeypatch.setattr(purge.stopping, "requested", real)
    assert _purge(synced) == 0
    assert not _library(synced).exists()


def test_a_file_swapped_in_at_a_placed_path_stays(synced: Config) -> None:
    swapped = _library(synced) / "show-a" / "t1.flac"
    swapped.unlink()
    swapped.write_bytes(b"someone else's")

    assert _purge(synced) == 0

    assert swapped.read_bytes() == b"someone else's"
    assert synced.state_db is not None
    with closing(connect(synced.state_db)) as conn:
        assert PlacementRepo(conn).list_unplaced(S, C) == ["show-a/t1.flac"]


def test_a_linked_folder_inside_the_library_is_never_deleted_through(synced: Config) -> None:
    """The archive copy of a placed file shares its inode: reached through a link in
    the library tree, it would pass the placed-file check."""
    show = _library(synced) / "show-a"
    for f in show.iterdir():
        f.unlink()
    show.rmdir()
    show.symlink_to(_archive(synced) / "show-a")

    assert _purge(synced) == 1

    assert (_archive(synced) / "show-a" / "t1.flac").exists()
    assert "is a link" in " ".join(_errors(synced))


def test_adding_the_collection_back_after_a_library_delete_restages_without_downloading(
    synced: Config,
) -> None:
    assert _purge(synced) == 0
    fetched = dict(FakePlugin.fetch_count)

    assert run_sync(synced) == 0

    assert FakePlugin.fetch_count == fetched
    assert sorted(p.name for p in _library(synced).rglob("*.flac")) == ["t1.flac", "t1.flac"]
    assert _unconfigured(synced, synced) == []  # configured again


def test_an_everything_delete_removes_the_archive_and_the_shows_records(
    synced: Config,
) -> None:
    config = synced
    _pending(config)

    assert _purge(config, everything=True) == 0

    assert not _archive(config).exists() and not _library(config).exists()
    assert config.archive_root.is_dir() and (config.archive_root / S).is_dir()
    assert config.state_db is not None and config.state_db.exists()
    with closing(connect(config.state_db)) as conn:
        assert list(ItemRepo(conn).list_for_collection(S, C)) == []
        assert PlacementRepo(conn).placed_identifiers(S, C) == set()
    assert _owed(config) == (None, [])
    assert _unconfigured(config) == []
    entry = _entry(config)
    assert entry["result"] == "completed"
    # The archive files' disk comes back once their library links are gone.
    assert entry["bytes_freed"] == sum(len(c) for c in SHOWS.values())


def test_a_removal_that_fails_keeps_its_records_and_reports_the_path(
    synced: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stuck = _archive(synced) / "show-b" / "t1.flac"
    real_unlink = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == stuck:
            raise PermissionError(13, "Permission denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    assert _purge(synced, everything=True) == 1

    entry = _entry(synced)
    assert entry["result"] == "completed_with_item_issues"
    assert synced.state_db is not None
    with closing(connect(synced.state_db)) as conn:
        left = [i.identifier for i in ItemRepo(conn).list_for_collection(S, C)]
        [row] = [r for r in run_history(conn, datetime.now(), 30) if r["kind"] == "purge"]
    # show-a is gone; show-b keeps its record, so the collection stays listed.
    assert left == ["show-b"]
    assert [u["shows"] for u in _unconfigured(synced)] == [1]
    assert any(str(stuck) in line and "Permission denied" in line for line in row["errors"])


def test_a_collection_the_file_names_again_is_never_deleted(synced: Config) -> None:
    """The file is read again under the lock: a collection added back between the
    request and the run keeps every file."""
    stale = _removed(synced)
    current = _file(synced, removed=False)

    assert purge.run_purge(stale, current, S, C, everything=True) == 1

    assert (_archive(synced) / "show-a" / "t1.flac").exists()
    assert (_library(synced) / "show-a" / "t1.flac").exists()
    entry = _entry(synced)
    assert entry["result"] == "failed_before_completion"
    assert synced.state_db is not None
    with closing(connect(synced.state_db)) as conn:
        outcome = OperationOutcomeRepo(conn).get(entry["outcome_id"])
    assert outcome is not None and outcome.operation == OperationType.PURGE
    assert outcome.errors == [f"{S}/{C} is in the config file; remove it first"]


def test_a_purge_is_refused_while_another_run_holds_the_lock(synced: Config) -> None:
    with writer_lock(synced):
        assert _purge(synced, everything=True) == 1
    assert (_archive(synced) / "show-a").is_dir()


def test_a_collection_name_that_leaves_its_folder_is_refused(synced: Config) -> None:
    """Names come from the state database, which only validated config wrote; a name
    that is not one folder would aim the delete at the archive root itself."""
    assert synced.state_db is not None
    with closing(connect(synced.state_db)) as conn:
        conn.execute("UPDATE items SET collection_name='..' WHERE collection_name=?", (C,))
    removed = _removed(synced)
    assert purge.run_purge(removed, _file(synced), S, "..", everything=True) == 1
    assert (_archive(synced) / "show-a" / "t1.flac").exists()


@pytest.mark.parametrize("linked", ["source", "collection"])
def test_a_collection_folder_replaced_by_a_link_is_never_deleted_through(
    synced: Config, linked: str,
) -> None:
    """A link in place of the source or collection folder would aim the deletes at
    wherever it points, outside the collection: the purge deletes nothing."""
    folder = _archive(synced) if linked == "collection" else synced.archive_root / S
    elsewhere = synced.archive_root.parent / "elsewhere"
    folder.rename(elsewhere)
    folder.symlink_to(elsewhere)

    assert _purge(synced, everything=True) == 1

    kept = elsewhere if linked == "collection" else elsewhere / C
    assert (kept / "show-a" / "t1.flac").exists()
    assert "is a link" in " ".join(_errors(synced))


def _errors(config: Config) -> list[str]:
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        outcome = OperationOutcomeRepo(conn).get(_entry(config)["outcome_id"])
    assert outcome is not None
    return outcome.errors


# --- the CLI ------------------------------------------------------------------------


def _cli(config: Config, *args: str) -> Any:
    return CliRunner().invoke(main, ["--config", str(_file(config)), "purge", *args])


def test_the_cli_deletes_the_library_or_everything(synced: Config) -> None:
    result = _cli(synced, "--source", S, "--collection", C)
    assert result.exit_code == 0, result.output
    assert not _library(synced).exists() and _archive(synced).exists()

    result = _cli(synced, "--source", S, "--collection", C, "--archive")
    assert result.exit_code == 0, result.output
    assert not _archive(synced).exists()


def test_the_cli_refuses_a_configured_collection_or_one_with_nothing_held(
    synced: Config,
) -> None:
    configured = write_config(synced.archive_root.parent / "shakedown.yaml", synced)
    result = CliRunner().invoke(
        main, ["--config", str(configured), "purge", "--source", S, "--collection", C])
    assert result.exit_code == 2
    assert "is in the config file" in result.output
    assert (_library(synced) / "show-a").is_dir()

    result = _cli(synced, "--source", S, "--collection", "never")
    assert result.exit_code == 2
    assert "nothing is held" in result.output
