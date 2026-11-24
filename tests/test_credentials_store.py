"""Where stored credentials live (§spec:dashboard-credentials "Where they live"): one
record per source in the state database the config names, outside schema versioning
like the edit state (§spec:state), plain text."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from shakedown import db
from shakedown.credentials import Login, forget, forget_others, move, store, stored
from shakedown.edit_lock import move_install_state


def test_no_database_has_none_and_reading_creates_none(tmp_path: Path) -> None:
    state_db = tmp_path / "archive" / ".shakedown" / "state.db"
    assert stored(state_db, "ia") is None
    assert not (tmp_path / "archive").exists()


def test_a_database_without_the_table_has_none(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    with closing(sqlite3.connect(state_db)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (14)")
        conn.commit()
    assert stored(state_db, "ia") is None


def test_storing_creates_the_table_and_keeps_the_schema_version(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    with closing(sqlite3.connect(state_db)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (14)")
        conn.commit()

    store(state_db, "ia", Login("me@example.org", "hunter2"))

    assert stored(state_db, "ia") == Login("me@example.org", "hunter2")
    with closing(sqlite3.connect(state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 14


def test_storing_creates_a_missing_database_and_replaces_per_source(tmp_path: Path) -> None:
    state_db = tmp_path / "new" / "state.db"
    store(state_db, "ia", Login("a@example.org", "one"))
    store(state_db, "ia", Login("b@example.org", "two"))
    store(state_db, "other", Login("c@example.org", "three"))
    assert stored(state_db, "ia") == Login("b@example.org", "two")
    assert stored(state_db, "other") == Login("c@example.org", "three")
    # A fresh database is at the current schema, credentials table included.
    with closing(db.connect(state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == (
            db.SCHEMA_VERSION
        )


def test_forgetting_removes_only_that_source(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    store(state_db, "ia", Login("a@example.org", "one"))
    store(state_db, "other", Login("c@example.org", "three"))
    forget(state_db, "ia")
    forget(state_db, "ia")  # nothing stored: still fine
    assert stored(state_db, "ia") is None
    assert stored(state_db, "other") is not None


def test_forgetting_never_creates_a_database(tmp_path: Path) -> None:
    state_db = tmp_path / "missing" / "state.db"
    forget(state_db, "ia")
    forget_others(state_db, {"ia"})
    assert not state_db.parent.exists()


def test_forgetting_others_keeps_the_named_sources(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    for name in ("ia", "gone", "also-gone"):
        store(state_db, name, Login(f"{name}@example.org", "pw"))
    forget_others(state_db, {"ia"})
    assert stored(state_db, "ia") is not None
    assert stored(state_db, "gone") is None and stored(state_db, "also-gone") is None


def test_moving_copies_every_record(tmp_path: Path) -> None:
    old, new = tmp_path / "old.db", tmp_path / "moved" / "new.db"
    store(old, "ia", Login("a@example.org", "one"))
    store(old, "other", Login("c@example.org", "three"))
    move(old, new)
    assert stored(new, "ia") == Login("a@example.org", "one")
    assert stored(new, "other") == Login("c@example.org", "three")


def test_moving_nothing_creates_no_database(tmp_path: Path) -> None:
    old, new = tmp_path / "old.db", tmp_path / "moved" / "new.db"
    move(old, new)
    assert not new.parent.exists()


def test_moving_the_install_state_carries_the_credentials(tmp_path: Path) -> None:
    old, new = tmp_path / "old.db", tmp_path / "moved" / "new.db"
    store(old, "ia", Login("a@example.org", "one"))
    move_install_state(old, new)
    assert stored(new, "ia") == Login("a@example.org", "one")


def test_a_login_never_shows_its_values() -> None:
    login = Login("secret-email@example.org", "secret-password")
    for text in (repr(login), str(login), f"{login}", f"{login!r}"):
        assert "secret-email" not in text and "secret-password" not in text
