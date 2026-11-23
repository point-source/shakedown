"""Audio known by its content, through sync, restage, and verify (§spec:real-format,
§req:success-criteria #34)."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from shakedown import realformat
from shakedown.config import Config
from shakedown.db import connect
from shakedown.realformat import AudioClass, FormatCheck, RealFormat
from shakedown.state import FormatRepo
from shakedown.sync import run_sync
from tests.audio_fixtures import CONTENT
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

AAC = FormatCheck(RealFormat.M4A, AudioClass.LOSSY)
MP3 = FormatCheck(RealFormat.MP3, AudioClass.LOSSY)
FLAC = FormatCheck(RealFormat.FLAC, AudioClass.LOSSLESS)
UNIDENTIFIED = FormatCheck(RealFormat.UNIDENTIFIED, None)

META = {"creator": "Dead & Company", "date": "2022-06-13",
        "venue": "Shoreline Amphitheatre", "title": "Live at Shoreline"}
DEADCO = {"1-01 St. Stephen.flac": "m4a", "1-02 Help on the Way.flac": "m4a",
          "1-03 Slipknot!.flac": "m4a"}


def _show(identifier: str, files: dict[str, str], *, listed_as: str = "Flac") -> FakeItem:
    """A show whose files hold `files[name]` audio (an audio_fixtures CONTENT key)
    whatever their names, listed by the source under `listed_as`, as archive.org
    repeats an uploader's mistake."""
    item = FakeItem(
        identifier,
        [FakeFile(name, CONTENT[content](), format=listed_as, title=f"Track {i}",
                  track=str(i))
         for i, (name, content) in enumerate(files.items(), start=1)],
        metadata=dict(META),
    )
    FakePlugin.items[identifier] = item
    return item


def _archive(config: Config, identifier: str) -> Path:
    return config.archive_root / "fake-src" / "coll1" / identifier


def _library(config: Config, identifier: str) -> Path:
    return config.library_root / "fake-src" / "coll1" / identifier


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_file()}


def _db(config: Config) -> sqlite3.Connection:
    assert config.state_db is not None
    return connect(config.state_db)


def _formats(config: Config, identifier: str) -> dict[str, FormatCheck]:
    records = FormatRepo(_db(config)).for_show("fake-src", "coll1", identifier)
    return {name: record[2] for name, record in records.items()}


# --- checked once, when the file arrives -----------------------------------------------


def test_a_fetched_show_records_each_files_real_format(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    item = _show("deadco2022-06-13", {**DEADCO, "info.txt": "wav", "notes.shn": "wav"})
    assert run_sync(config) == 0

    assert _formats(config, item.identifier) == dict.fromkeys(DEADCO, AAC)
    records = FormatRepo(_db(config)).for_show("fake-src", "coll1", item.identifier)
    by_name = {f.name: f for f in item.files}
    assert {n: r[:2] for n, r in records.items()} == {
        n: (by_name[n].size, by_name[n].md5) for n in DEADCO
    }


def test_the_run_log_has_one_line_per_mislabelled_file(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", {**DEADCO, "1-04 honest.flac": "flac", "wav.flac": "wav"})
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    lines = [r.getMessage() for r in caplog.records
             if "audio" in r.getMessage() and "deadco2022-06-13:" in r.getMessage()]
    assert sorted(lines) == sorted(
        [f"[fake-src/coll1] deadco2022-06-13: {name} holds M4A (AAC) audio, not FLAC"
         for name in DEADCO]
        + ["[fake-src/coll1] deadco2022-06-13: wav.flac is not audio shakedown "
           "recognises; it keeps its name"]
    )


def test_a_file_that_cannot_be_read_for_its_check_fails_the_fetch_for_a_retry(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O fault is not a verdict: nothing is recorded, and the next run fetches
    and checks the show again."""
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    real = realformat.check_file

    def unreadable(path: Path) -> FormatCheck:
        raise OSError("I/O error")

    monkeypatch.setattr(realformat, "check_file", unreadable)
    assert run_sync(config) != 0
    assert not _archive(config, "deadco2022-06-13").exists()
    assert _formats(config, "deadco2022-06-13") == {}

    monkeypatch.setattr(realformat, "check_file", real)
    assert run_sync(config) == 0
    assert _formats(config, "deadco2022-06-13") == dict.fromkeys(DEADCO, AAC)


def test_a_file_the_source_changes_is_checked_again(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    item = _show("deadco2022-06-13", {"t1.flac": "m4a", "t2.flac": "flac"})
    assert run_sync(config) == 0
    assert _formats(config, item.identifier) == {"t1.flac": AAC, "t2.flac": FLAC}

    item.files[0] = FakeFile("t1.flac", CONTENT["flac"](), format="Flac")
    assert run_sync(config) == 0
    assert _formats(config, item.identifier) == {"t1.flac": FLAC, "t2.flac": FLAC}
