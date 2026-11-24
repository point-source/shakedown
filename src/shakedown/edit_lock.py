"""The config file's edit lock (§spec:edit-lock).

A file Shakedown's setup wrote is `editable`; a file that came from outside starts
`locked` until the operator turns on the `override`, which stays on. The state is one
row in the state database the config names, outside schema versioning like the
scheduler's heartbeat (§spec:state): `serve` writes it, and `serve` never holds the
writer lock that migrates. No row is `locked`, so a rebuilt or newly pointed-at
database starts locked again.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from shakedown import db
from shakedown.config import ConfigFile, raw_state_db

EditState = Literal["editable", "locked", "override"]
Recorded = Literal["editable", "override"]


def read_edit(state_db: Path | None) -> EditState:
    """The edit state recorded in ``state_db``. Reads only: a missing database is
    never created. No database, no table, or no row is `locked`."""
    if state_db is None or not state_db.exists():
        return "locked"
    try:
        with closing(db.open_read_only(state_db)) as conn:
            row = conn.execute("SELECT state FROM config_edit WHERE id = 1").fetchone()
    except sqlite3.Error:
        return "locked"  # an older database without the table, or one we cannot read
    return row["state"] if row else "locked"


def record_edit(state_db: Path, state: Recorded) -> None:
    """Record the edit state, creating a missing database. Raises sqlite3.Error
    when it cannot be written."""
    db.replace_row(
        state_db,
        db.CONFIG_EDIT_TABLE,
        "INSERT OR REPLACE INTO config_edit (id, state, at) VALUES (1, ?, ?)",
        (state, datetime.now(UTC).isoformat()),
        create=True,
    )


def move_install_state(old_db: Path, new_db: Path) -> None:
    """Carry what Shakedown keeps for this install into the database a save points
    the config at: the edit state (§spec:edit-lock "Where the state lives")."""
    state = read_edit(old_db)
    if state != "locked":
        record_edit(new_db, state)


def state_db_for(file: ConfigFile) -> Path | None:
    """The state database a config file names: the loaded config's, or for an
    invalid file the one its `state_db` or `archive_root` names. None when the file
    is missing or unreadable, or names neither."""
    if file.config is not None:
        return file.config.state_db
    return raw_state_db(file.raw)
