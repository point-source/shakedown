"""Credentials entered in the dashboard (§spec:dashboard-credentials).

One record per source in the state database the config names, in plain text,
protected as that file is (operator decision, 2026-11-24). The table sits outside
schema versioning like the edit state (§spec:state): `serve` writes it, and `serve`
never holds the writer lock that migrates. Reading never creates the database; no
database, table, or row is no stored credentials.

A value never leaves this module in a form that prints: `Login` hides both fields
from `repr` and `str`, so a log line or error that formats one shows nothing.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from shakedown import db

if TYPE_CHECKING:
    from shakedown.config import IAAuth, SourceConfig

# Where a source's login comes from (§spec:dashboard-credentials "Status, never
# values"), and the source's verdict on a login ("Write-only").
Status = Literal["environment", "dashboard", "none"]
LoginCheck = Literal["accepted", "rejected", "unchecked"]


@dataclass(frozen=True)
class Login:
    email: str
    password: str

    def __repr__(self) -> str:
        return "Login(<hidden>)"

    __str__ = __repr__


def resolve(source: SourceConfig, state_db: Path | None) -> tuple[Status, Login | None]:
    """The login a run of ``source`` uses, and where it comes from
    (§spec:dashboard-credentials "Which wins"): the environment variables when both
    are set — the names its `auth` gives, each falling back to the type's standard
    name — otherwise its stored credentials, otherwise none. Values are trimmed: a
    line ending left by an env file would break the auth header. A type that takes
    no credentials has none."""
    from shakedown.plugins import registry

    plugin = registry.plugin_class(source.type)
    if not plugin.takes_credentials:
        return "none", None
    email_name, password_name = env_names(source.auth, plugin.credential_env)
    email, password = env_value(email_name), env_value(password_name)
    if email and password:
        return "environment", Login(email, password)
    login = stored(state_db, source.name)
    if login is not None:
        return "dashboard", login
    return "none", None


def env_names(
    auth: IAAuth | None, standard: tuple[str, str] | None
) -> tuple[str | None, str | None]:
    """The (email, password) environment variables a source reads: the names its
    `auth` gives, each falling back to the type's standard name."""
    email_name, password_name = standard or (None, None)
    if auth is not None:
        email_name = auth.email_env or email_name
        password_name = auth.password_env or password_name
    return email_name, password_name


def env_value(name: str | None) -> str:
    """A credential variable's value, trimmed; empty when unset or unnamed."""
    return os.environ.get(name, "").strip() if name else ""


def login_for(source: SourceConfig, state_db: Path | None) -> Login | None:
    """The login a run of ``source`` uses, or None (`resolve` without the status)."""
    return resolve(source, state_db)[1]


def stored(state_db: Path | None, source: str) -> Login | None:
    """The credentials stored for ``source``, or None. Reads only."""
    if state_db is None or not state_db.exists():
        return None
    try:
        with closing(db.open_read_only(state_db)) as conn:
            row = conn.execute(
                "SELECT email, password FROM source_credentials WHERE source = ?", (source,)
            ).fetchone()
    except sqlite3.Error:
        return None  # an older database without the table, or one we cannot read
    return Login(row["email"], row["password"]) if row else None


def store(state_db: Path, source: str, login: Login) -> None:
    """Store ``login`` for ``source``, replacing what was there and creating a
    missing database. Raises sqlite3.Error when it cannot be written."""
    db.write_unversioned(
        state_db,
        db.CREDENTIALS_TABLE,
        [(_INSERT, (source, login.email, login.password, _now()))],
        create=True,
    )


def forget(state_db: Path, source: str) -> None:
    """Forget what is stored for ``source``. A missing database stays missing."""
    db.write_unversioned(
        state_db,
        db.CREDENTIALS_TABLE,
        [("DELETE FROM source_credentials WHERE source = ?", (source,))],
        create=False,
    )


def forget_others(state_db: Path, keep: set[str]) -> None:
    """Forget what is stored for every source not in ``keep``: the sources a saved
    config no longer has. A missing database stays missing."""
    marks = ", ".join("?" * len(keep))
    db.write_unversioned(
        state_db,
        db.CREDENTIALS_TABLE,
        [(f"DELETE FROM source_credentials WHERE source NOT IN ({marks})", tuple(keep))],
        create=False,
    )


def move(old_db: Path, new_db: Path) -> None:
    """Copy every stored record into ``new_db``, the database a save points the
    config at. Nothing stored: nothing written, and no database created."""
    if not old_db.exists():
        return
    try:
        with closing(db.open_read_only(old_db)) as conn:
            rows = conn.execute(
                "SELECT source, email, password, at FROM source_credentials"
            ).fetchall()
    except sqlite3.Error:
        return
    if rows:
        db.write_unversioned(
            new_db,
            db.CREDENTIALS_TABLE,
            [(_INSERT, tuple(row)) for row in rows],
            create=True,
        )


_INSERT = (
    "INSERT OR REPLACE INTO source_credentials (source, email, password, at) "
    "VALUES (?, ?, ?, ?)"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()
