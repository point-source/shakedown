"""Stored shows checked once, without downloading (§spec:real-format-catch-up,
§req:success-criteria #34)."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import shakedown.activity as activity
from shakedown import realformat
from shakedown.config import Config
from shakedown.realformat import FormatCheck
from shakedown.state import TagRepo
from shakedown.sync import run_sync
from tests.audio_fixtures import read_tags
from tests.conftest import make_config
from tests.fake_plugin import FakePlugin
from tests.test_real_format import (
    AAC,
    DEADCO,
    FLAC,
    META,
    MP3,
    _archive,
    _db,
    _formats,
    _library,
    _names,
    _show,
)

HONEST = {"t1.flac": "flac", "t2.flac": "flac"}


def _filled(title: str, track: str) -> dict[str, list[str]]:
    """Every tag shakedown fills in an untagged file of a show with META."""
    return {
        "artist": [META["creator"]], "albumartist": [META["creator"]],
        "album": [f"{META['date']} \N{EN DASH} {META['venue']}"],
        "date": [META["date"]], "venue": [META["venue"]],
        "title": [title], "tracknumber": [track],
    }


def _mirror_before_the_check(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync as shakedown did before it checked content, then put the database back at
    v12: no file was checked, a mislabelled file was staged under its source name and
    tagged by that name (a FLAC writer on M4A content writes nothing), and every show
    kept its tag fingerprint."""
    with monkeypatch.context() as m:
        m.setattr("shakedown.sync.is_checked", lambda name: False)
        assert run_sync(config) == 0
    with closing(_db(config)) as conn:
        conn.execute("DROP TABLE audio_formats")
        conn.execute("UPDATE schema_version SET version = 12")


def _refuse_downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any audio request fails the test's expectations: it is recorded and refused."""
    asked: list[str] = []

    def fetch(self, identifier, files, dest_dir):
        asked.append(identifier)
        raise AssertionError(f"downloaded {identifier}")

    monkeypatch.setattr(FakePlugin, "fetch", fetch)
    return asked


def _tree(root: Path) -> dict[str, int]:
    """Every file under root by relative path, with its inode."""
    return {str(p.relative_to(root)): p.stat().st_ino for p in root.rglob("*") if p.is_file()}


def _bytes(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


# --- the first sync after the upgrade ----------------------------------------------------


def test_the_first_sync_after_the_upgrade_renames_and_tags_without_downloading(
    tmp_roots, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§spec:real-format-catch-up user-level test path."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    honest = _show("gd1977-05-08", HONEST)
    honest.metadata["date"] = "1977-05-08"  # its own album: no clash label on either
    _mirror_before_the_check(config, monkeypatch)
    assert _names(_library(config, "deadco2022-06-13")) == set(DEADCO)
    honest_archive = _bytes(_archive(config, "gd1977-05-08"))
    honest_library = _tree(_library(config, "gd1977-05-08"))
    honest_tags = TagRepo(_db(config)).file_records("fake-src", "coll1", "gd1977-05-08")
    asked = _refuse_downloads(monkeypatch)
    caplog.clear()

    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    assert asked == []
    library = _library(config, "deadco2022-06-13")
    m4a = {name.replace(".flac", ".m4a") for name in DEADCO}
    assert _names(library) == m4a
    assert _names(_archive(config, "deadco2022-06-13")) == set(DEADCO)
    for i, name in enumerate(DEADCO, start=1):
        assert read_tags(library / name.replace(".flac", ".m4a"), "m4a") == _filled(
            f"Track {i}", str(i)
        )
    assert _formats(config, "deadco2022-06-13") == dict.fromkeys(DEADCO, AAC)
    assert _formats(config, "gd1977-05-08") == dict.fromkeys(HONEST, FLAC)
    # The honest show is untouched: same bytes, same links, same tag records.
    assert _bytes(_archive(config, "gd1977-05-08")) == honest_archive
    assert _tree(_library(config, "gd1977-05-08")) == honest_library
    assert TagRepo(_db(config)).file_records(
        "fake-src", "coll1", "gd1977-05-08"
    ) == honest_tags
    assert "cannot be read as FLAC" not in caplog.text


def test_an_mp3_named_flac_stored_show_becomes_mp3(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like deadco2022-06-18.deadco2022-06-18: 320 kbps MP3 named `.flac`."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-18", {"t1.flac": "mp3", "t2.flac": "mp3"})
    _mirror_before_the_check(config, monkeypatch)
    asked = _refuse_downloads(monkeypatch)

    assert run_sync(config) == 0

    assert asked == []
    library = _library(config, "deadco2022-06-18")
    assert _names(library) == {"t1.mp3", "t2.mp3"}
    assert read_tags(library / "t1.mp3", "mp3")["venue"] == [META["venue"]]
    assert _formats(config, "deadco2022-06-18") == {"t1.flac": MP3, "t2.flac": MP3}


def test_a_collection_without_tag_filling_is_checked_and_renamed_but_not_tagged(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    _mirror_before_the_check(config, monkeypatch)
    archived = _bytes(_archive(config, "deadco2022-06-13"))
    asked = _refuse_downloads(monkeypatch)

    assert run_sync(config) == 0

    assert asked == []
    assert _names(_library(config, "deadco2022-06-13")) == {
        name.replace(".flac", ".m4a") for name in DEADCO
    }
    assert _bytes(_archive(config, "deadco2022-06-13")) == archived


def test_a_later_sync_opens_no_file_and_downloads_nothing(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    _mirror_before_the_check(config, monkeypatch)
    assert run_sync(config) == 0
    library = _tree(config.library_root)
    asked = _refuse_downloads(monkeypatch)

    def opened(path: Path) -> FormatCheck:
        raise AssertionError(f"opened {path}")

    monkeypatch.setattr(realformat, "check_file", opened)
    assert run_sync(config) == 0

    assert asked == []
    assert _tree(config.library_root) == library


def test_a_stored_show_the_source_no_longer_lists_is_checked_too(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept in the archive and staged, so its library names are fixed as well."""
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    _mirror_before_the_check(config, monkeypatch)
    del FakePlugin.items["deadco2022-06-13"]

    assert run_sync(config) == 0

    assert _names(_library(config, "deadco2022-06-13")) == {
        name.replace(".flac", ".m4a") for name in DEADCO
    }


def test_the_run_log_has_one_line_per_mislabel_found(
    tmp_roots, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", {**DEADCO, "honest.flac": "flac"})
    _mirror_before_the_check(config, monkeypatch)
    caplog.clear()

    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    lines = [r.getMessage() for r in caplog.records if " holds " in r.getMessage()]
    assert sorted(lines) == sorted(
        f"[fake-src/coll1] deadco2022-06-13: {name} holds M4A (AAC) audio, not FLAC"
        for name in DEADCO
    )


# --- in the Discovering phase, before the lookups -----------------------------------------


def _activity(config: Config) -> sqlite3.Row:
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        conn.row_factory = sqlite3.Row
        [row] = conn.execute(
            "SELECT * FROM run_activity WHERE state = 'running'"
        ).fetchall()
        return row


@pytest.mark.parametrize("incremental", [False, True])
def test_the_check_runs_in_the_discovering_phase_before_any_lookup(
    tmp_roots, monkeypatch: pytest.MonkeyPatch, incremental: bool
) -> None:
    """The run shows the work as Discovering, with the show in hand, before the
    show's copy is chosen; an incremental collection, which looks nothing up for an
    unchanged show, is checked as well."""
    monkeypatch.setattr(activity, "TICK_S", 0.05)
    config = make_config(*tmp_roots, incremental_discovery=incremental)
    item = _show("deadco2022-06-13", DEADCO)
    item.change_signal = "v1"
    _mirror_before_the_check(config, monkeypatch)
    FakePlugin.lookup_calls.clear()
    real = realformat.check_file
    seen: list[tuple[str, str, int]] = []

    def check(path: Path) -> FormatCheck:
        import json
        import time

        deadline = time.monotonic() + 5
        while True:
            row = _activity(config)
            if json.loads(row["current_items"] or "[]") == ["deadco2022-06-13"]:
                break
            assert time.monotonic() < deadline, "the show never appeared in hand"
            time.sleep(0.02)
        seen.append((row["phase"], path.name, len(FakePlugin.lookup_calls)))
        return real(path)

    monkeypatch.setattr(realformat, "check_file", check)
    assert run_sync(config) == 0

    assert sorted(seen) == sorted(("discovering", name, 0) for name in DEADCO)
    assert _names(_library(config, "deadco2022-06-13")) == {
        name.replace(".flac", ".m4a") for name in DEADCO
    }


def test_a_failed_listing_checks_nothing_until_a_listing_succeeds(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    _mirror_before_the_check(config, monkeypatch)
    FakePlugin.unreachable_sources.add("fake-src")

    assert run_sync(config) != 0
    assert _formats(config, "deadco2022-06-13") == {}

    FakePlugin.unreachable_sources.clear()
    assert run_sync(config) == 0
    assert _formats(config, "deadco2022-06-13") == dict.fromkeys(DEADCO, AAC)


def test_each_checked_show_counts_as_progress(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No counter moves while stored shows are checked, so each checked show marks the
    run's progress: a long first sync is not shown as making none
    (§spec:run-activity "Is it stuck?")."""
    monkeypatch.setattr(activity, "TICK_S", 0.05)
    config = make_config(*tmp_roots)
    for n in range(3):
        _show(f"show{n}", {"t1.flac": "m4a"})
    _mirror_before_the_check(config, monkeypatch)
    real = realformat.check_file
    progress_at: list[str] = []

    def check(path: Path) -> FormatCheck:
        import time

        if path.parent.name == "show2":
            deadline = time.monotonic() + 5
            while _activity(config)["last_progress_at"] == progress_at[0]:
                assert time.monotonic() < deadline, "checked shows did not count"
                time.sleep(0.02)
        progress_at.append(_activity(config)["last_progress_at"])
        return real(path)

    monkeypatch.setattr(realformat, "check_file", check)
    assert run_sync(config) == 0

    assert progress_at[2] > progress_at[0]


# --- interruptible, and a file that cannot be read --------------------------------------

SHOWS = tuple(f"show{n}" for n in range(5))


def _five_mislabelled_shows(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    for identifier in SHOWS:
        _show(identifier, {"t1.flac": "m4a", "t2.flac": "m4a"})
    _mirror_before_the_check(config, monkeypatch)


def _checked_shows(config: Config) -> list[str]:
    return [identifier for identifier in SHOWS if _formats(config, identifier)]


def _opened(monkeypatch: pytest.MonkeyPatch, *, on=None) -> list[str]:
    """Every file the check opens, as `<show>/<file>`; `on(path)` runs first."""
    real = realformat.check_file
    opened: list[str] = []

    def check(path: Path) -> FormatCheck:
        opened.append(f"{path.parent.name}/{path.name}")
        if on is not None:
            on(path)
        return real(path)

    monkeypatch.setattr(realformat, "check_file", check)
    return opened


def test_a_stopped_check_resumes_with_the_shows_not_yet_checked(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from shakedown import stopping

    config = make_config(*tmp_roots)
    _five_mislabelled_shows(config, monkeypatch)
    stopping.reset()

    def stop_in_show2(path: Path) -> None:
        if path.parent.name == "show2":
            stopping.request(signal.SIGTERM)

    try:
        with monkeypatch.context() as m:
            _opened(m, on=stop_in_show2)
            assert run_sync(config) == 143
        # The show in hand finished; the rest wait.
        assert _checked_shows(config) == ["show0", "show1", "show2"]
    finally:
        stopping.reset()

    opened = _opened(monkeypatch)
    assert run_sync(config) == 0
    assert opened == ["show3/t1.flac", "show3/t2.flac", "show4/t1.flac", "show4/t2.flac"]
    for identifier in SHOWS:
        assert _names(_library(config, identifier)) == {"t1.m4a", "t2.m4a"}


def test_a_killed_check_keeps_the_shows_it_finished(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots)
    _five_mislabelled_shows(config, monkeypatch)

    def die_in_show2(path: Path) -> None:
        if path.parent.name == "show2" and path.name == "t2.flac":
            raise KeyboardInterrupt  # the process dies here

    with monkeypatch.context() as m:
        _opened(m, on=die_in_show2)
        with pytest.raises(KeyboardInterrupt):
            run_sync(config)
    assert _checked_shows(config) == ["show0", "show1"]

    opened = _opened(monkeypatch)
    assert run_sync(config) == 0
    assert [name for name in opened if name.startswith(("show0", "show1"))] == []
    assert _checked_shows(config) == list(SHOWS)


@pytest.mark.parametrize("fault", ["io-error", "reader-bug"])
def test_a_file_that_cannot_be_checked_stays_unchecked_and_fails_nothing(
    tmp_roots, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fault: str
) -> None:
    """An I/O error leaves that file unchecked until the next run; so does a reader
    failing on odd content, which must not stop discovery of the whole collection. The
    show is not failed, and its other files are checked."""
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", {"t1.flac": "m4a", "t2.flac": "m4a"})
    _mirror_before_the_check(config, monkeypatch)
    broken = _archive(config, "deadco2022-06-13") / "t2.flac"
    real = realformat.check_file

    def check(path: Path) -> FormatCheck:
        if path == broken and fault == "io-error":
            raise OSError(5, "Input/output error")
        if path == broken and fault == "reader-bug":
            raise ValueError("unexpected box")
        return real(path)

    caplog.clear()
    with monkeypatch.context() as m:
        m.setattr(realformat, "check_file", check)
        with caplog.at_level(logging.WARNING):
            assert run_sync(config) == 0

    assert _formats(config, "deadco2022-06-13") == {"t1.flac": AAC}
    assert "t2.flac" in caplog.text and "next run" in caplog.text
    from shakedown.state import RunRepo

    run = RunRepo(_db(config)).latest("fake-src", "coll1")
    assert run is not None and run.items_failed == 0

    opened = _opened(monkeypatch)
    assert run_sync(config) == 0
    assert opened == ["deadco2022-06-13/t2.flac"]
    assert _formats(config, "deadco2022-06-13") == {"t1.flac": AAC, "t2.flac": AAC}


def test_a_missing_file_is_left_to_the_syncs_own_repair(
    tmp_roots, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check skips a stored file that is gone, and fails nothing. The sync already
    fetches a show's missing files again (§spec:sync-workflow), and that download is
    checked when it arrives (§spec:real-format)."""
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", {"t1.flac": "m4a", "t2.flac": "m4a"})
    _mirror_before_the_check(config, monkeypatch)
    (_archive(config, "deadco2022-06-13") / "t2.flac").unlink()
    caplog.clear()

    with caplog.at_level(logging.WARNING):
        assert run_sync(config) == 0

    assert "could not check the format of t2.flac" in caplog.text
    assert FakePlugin.fetched_files["deadco2022-06-13"][-1:] == ["t2.flac"]
    assert _formats(config, "deadco2022-06-13") == {"t1.flac": AAC, "t2.flac": AAC}
    assert _names(_library(config, "deadco2022-06-13")) == {"t1.m4a", "t2.m4a"}


# --- reconcile, verify, reconform ---------------------------------------------------------


def test_after_reconcile_the_names_stay_real(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database rebuilt by `reconcile` holds no checks; the next sync makes them again
    from the files, so the library keeps its real names (§spec:state)."""
    from shakedown.reconcile import run_reconcile

    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    _show("deadco2022-06-18", {"t1.flac": "mp3"})
    _mirror_before_the_check(config, monkeypatch)
    assert run_sync(config) == 0
    library = _tree(config.library_root)
    assert config.state_db is not None
    config.state_db.unlink()
    assert run_reconcile(config) == 0
    assert _formats(config, "deadco2022-06-13") == {}
    asked = _refuse_downloads(monkeypatch)

    assert run_sync(config) == 0

    assert asked == []
    assert _formats(config, "deadco2022-06-13") == dict.fromkeys(DEADCO, AAC)
    assert _formats(config, "deadco2022-06-18") == {"t1.flac": MP3}
    assert _tree(config.library_root) == library


def test_verify_finds_no_drift_and_reconform_keeps_the_real_name(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the stored shows are fixed: a later sync downloads nothing, `verify --deep`
    finds no drift, and `verify --deep --reconform` restores the source bytes, keeps
    the real names, and fills the tags again."""
    from tests.test_real_format import _verify

    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    _show("deadco2022-06-18", {"t1.flac": "mp3"}).metadata["date"] = "2022-06-18"
    _mirror_before_the_check(config, monkeypatch)
    assert run_sync(config) == 0
    downloads = dict(FakePlugin.fetch_count)

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == downloads
    assert _verify(config) == []

    aac = _archive(config, "deadco2022-06-13") / "1-01 St. Stephen.flac"
    mp3 = _archive(config, "deadco2022-06-18") / "t1.flac"
    for path in (aac, mp3):
        with path.open("r+b") as f:  # another tool changes the bytes in place
            f.seek(-1, 2)
            f.write(b"\x01")
    drifted = [("deadco2022-06-13", "1-01 St. Stephen.flac"), ("deadco2022-06-18", "t1.flac")]
    assert _verify(config, reconform=True) == drifted  # what it found, then repaired
    assert _verify(config) == []

    assert _names(_library(config, "deadco2022-06-13")) == {
        name.replace(".flac", ".m4a") for name in DEADCO
    }
    assert _names(_library(config, "deadco2022-06-18")) == {"t1.mp3"}
    assert read_tags(aac, "m4a") == _filled("Track 1", "1")
    assert read_tags(mp3, "mp3")["venue"] == [META["venue"]]
