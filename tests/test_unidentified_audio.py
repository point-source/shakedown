"""Unidentified audio in `status` (§spec:unidentified-audio, §req:success-criteria #34):
a file whose content matches none of the recognised formats is listed per collection,
never only in a log."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

import pytest

from shakedown.config import Config
from shakedown.db import connect
from shakedown.realformat import UNIDENTIFIED
from shakedown.state import FormatRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.audio_fixtures import CONTENT
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

SRC, COLL = "fake-src", "coll1"


def _show(identifier: str, files: dict[str, str]) -> FakeItem:
    """A show whose files hold `files[name]` audio (an audio_fixtures CONTENT key),
    listed by the source as FLAC whatever their content."""
    item = FakeItem(
        identifier,
        [FakeFile(name, CONTENT[content](), format="Flac") for name, content in files.items()],
    )
    FakePlugin.items[identifier] = item
    return item


def _status(config: Config, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    capsys.readouterr()
    print_status(config, as_json=True)
    return next(s for s in json.loads(capsys.readouterr().out) if s["collection"] == COLL)


def _text(config: Config, capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    print_status(config, as_json=False)
    return capsys.readouterr().out


def _db(config: Config) -> sqlite3.Connection:
    assert config.state_db is not None
    return connect(config.state_db)


def test_a_wav_named_flac_is_listed_under_its_collection(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _show("wav-show", {"t1.flac": "wav", "t2.flac": "flac"})
    _show("m4a-show", {"t1.flac": "m4a"})  # a recognised mislabel is not listed
    assert run_sync(config) == 0

    assert _status(config, capsys)["unidentified_files"] == [
        {"identifier": "wav-show", "file": "t1.flac"}
    ]
    text = _text(config, capsys)
    assert "audio shakedown cannot identify: 1 file (kept unchanged):" in text
    assert "    - wav-show: 't1.flac'" in text


def test_the_file_leaves_the_list_once_the_source_replaces_it_with_real_audio(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    item = _show("wav-show", {"t1.flac": "wav"})
    assert run_sync(config) == 0
    assert _status(config, capsys)["unidentified_files"] != []

    item.files[0] = FakeFile("t1.flac", CONTENT["flac"](), format="Flac")
    assert run_sync(config) == 0

    assert _status(config, capsys)["unidentified_files"] == []
    assert "cannot identify" not in _text(config, capsys)


def test_a_pruned_show_leaves_the_list(tmp_roots, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(*tmp_roots, prune_disappeared=True)
    _show("wav-show", {"t1.flac": "wav"})
    _show("keep", {"t1.flac": "flac"})
    assert run_sync(config) == 0

    del FakePlugin.items["wav-show"]
    assert run_sync(config) == 0

    assert _status(config, capsys)["unidentified_files"] == []


def test_a_record_made_for_other_bytes_is_not_listed(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    """A check belongs to the file as the manifest records it: one left from other
    bytes (a manifest change without a fetch) says nothing about the stored file."""
    config = make_config(*tmp_roots)
    _show("show", {"t1.flac": "flac"})
    assert run_sync(config) == 0
    with closing(_db(config)) as conn:
        FormatRepo(conn).put(SRC, COLL, "show", "t1.flac", 1, "not-its-md5", UNIDENTIFIED)
        conn.commit()

    assert _status(config, capsys)["unidentified_files"] == []


def test_status_prints_the_first_ten_and_json_keeps_them_all(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    _show("show-b", {f"b{i:02}.flac": "wav" for i in range(7)})
    _show("show-a", {f"a{i:02}.flac": "wav" for i in range(5)})
    assert run_sync(config) == 0

    files = _status(config, capsys)["unidentified_files"]
    assert [(f["identifier"], f["file"]) for f in files] == (
        [("show-a", f"a{i:02}.flac") for i in range(5)]
        + [("show-b", f"b{i:02}.flac") for i in range(7)]
    )
    text = _text(config, capsys)
    assert "audio shakedown cannot identify: 12 files (kept unchanged):" in text
    assert "    - show-b: 'b04.flac'" in text
    assert "    - show-b: 'b05.flac'" not in text
    assert "    ... and 2 more" in text


def test_a_database_from_before_format_records_answers_without_migrating(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status` reads without migrating; an older database has no format records yet."""
    config = make_config(*tmp_roots)
    with closing(_db(config)) as conn:
        conn.execute("DROP TABLE audio_formats")
        conn.execute("UPDATE schema_version SET version = 12")
        conn.commit()

    assert _status(config, capsys)["unidentified_files"] == []
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 12
