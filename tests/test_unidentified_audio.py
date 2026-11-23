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


# --- GET /api/attention ------------------------------------------------------------


def _attention(config: Config) -> dict[str, Any]:
    pytest.importorskip("fastapi")
    from tests.test_api_attention import _attention as answer

    return answer(config)


def _unidentified_entries(config: Config) -> list[dict[str, Any]]:
    return [e for e in _attention(config)["entries"] if e["kind"] == "unidentified_files"]


def test_the_attention_list_has_one_entry_naming_the_show_and_file(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    """§spec:unidentified-audio's user-level path: a WAV named `.flac` is one entry,
    gone once the source replaces it with real FLAC."""
    config = make_config(*tmp_roots, fill_tags=True)
    item = _show("wav-show", {"t1.flac": "wav", "t2.flac": "flac"})
    _show("ok", {"t1.flac": "flac"})
    assert run_sync(config) == 0

    assert _unidentified_entries(config) == [{
        "kind": "unidentified_files", "source": SRC, "collection": COLL, "since": None,
        "identifiers": ["wav-show"], "count": 1,
        "detail": {"unidentified_files": _status(config, capsys)["unidentified_files"]},
    }]

    item.files[0] = FakeFile("t1.flac", CONTENT["flac"](), format="Flac")
    assert run_sync(config) == 0
    assert _unidentified_entries(config) == []


def test_the_entry_counts_files_and_carries_the_first_ten(
    tmp_roots, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    for s in range(12):
        _show(f"show-{s:02}", {"t1.flac": "wav", "t2.flac": "wav"})
    assert run_sync(config) == 0

    [entry] = _unidentified_entries(config)
    files = _status(config, capsys)["unidentified_files"]
    assert len(files) == 24
    assert entry["count"] == 24
    assert entry["identifiers"] == [f"show-{s:02}" for s in range(10)]
    assert entry["detail"] == {"unidentified_files": files[:10]}
