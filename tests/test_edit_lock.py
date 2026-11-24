"""Where the edit state lives (§spec:edit-lock): one row in the state database the
config names, outside schema versioning; no record there is `locked`."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

from shakedown import db
from shakedown.config import read_config_file
from shakedown.edit_lock import move_install_state, read_edit, record_edit, state_db_for


def test_no_database_is_locked_and_reading_creates_none(tmp_path: Path) -> None:
    state_db = tmp_path / "archive" / ".shakedown" / "state.db"
    assert read_edit(state_db) == "locked"
    assert not state_db.exists()
    assert not (tmp_path / "archive").exists()
    assert read_edit(None) == "locked"


def test_a_database_without_a_record_is_locked(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    db.connect(state_db).close()
    assert read_edit(state_db) == "locked"


def test_an_older_database_gets_the_table_and_keeps_its_version(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    with closing(sqlite3.connect(state_db)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (3)")
        conn.commit()
    assert read_edit(state_db) == "locked"

    record_edit(state_db, "override")

    assert read_edit(state_db) == "override"
    with closing(sqlite3.connect(state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3


def test_a_record_survives_reopening(tmp_path: Path) -> None:
    state_db = tmp_path / "new" / "state.db"
    record_edit(state_db, "editable")  # creates a missing database
    assert read_edit(state_db) == "editable"
    record_edit(state_db, "override")
    assert read_edit(state_db) == "override"


def test_moving_copies_the_edit_state(tmp_path: Path) -> None:
    old, new = tmp_path / "old.db", tmp_path / "moved" / "new.db"
    record_edit(old, "override")
    move_install_state(old, new)
    assert read_edit(new) == "override"


def test_the_database_comes_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    assert state_db_for(read_config_file(path)) is None  # missing

    doc = {
        "archive_root": "/a",
        "library_root": "/l",
        "sources": [{"name": "s", "type": "ia", "collections": []}],
    }
    path.write_text(yaml.safe_dump(doc))
    assert state_db_for(read_config_file(path)) == Path("/a/.shakedown/state.db")

    path.write_text(yaml.safe_dump({**doc, "state_db": "/db/state.db"}))
    assert state_db_for(read_config_file(path)) == Path("/db/state.db")

    # An invalid file still names its database, so the operator can unlock and fix.
    path.write_text(yaml.safe_dump({**doc, "bogus": 1}))
    file = read_config_file(path)
    assert file.state == "invalid"
    assert state_db_for(file) == Path("/a/.shakedown/state.db")

    path.write_text(yaml.safe_dump({"library_root": "/l"}))
    assert state_db_for(read_config_file(path)) is None

    path.write_text("archive_root: [unclosed\n")
    assert state_db_for(read_config_file(path)) is None
