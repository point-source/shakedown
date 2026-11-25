"""Saving the config from the dashboard (§spec:dashboard-settings "Save is a
whole-document replace, checked first"), the path checks without a save, and the
edit-lock override (§spec:edit-lock).

Each refusal is a `Refusal` with its code (§spec:dashboard "Codes, not copy"). Writes
are serialized within serve; a hand edit landing between the revision check and the
replace is caught by re-reading the file just before it is replaced.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shakedown.config import (
    ConfigFile,
    ConfigProblem,
    ConfigRejected,
    check_document,
    default_state_db,
    entries,
    read_config_file,
    revision_of,
)
from shakedown.config_file import WriteFailed, atomic_replace, merge
from shakedown.dashboard_config import document_of
from shakedown.edit_lock import move_install_state, read_edit, record_edit, state_db_for
from shakedown.lock import LockHeld, retire, unretire, writer_lock_at
from shakedown.validate import path_checks, path_warnings
from shakedown.write_gate import Refusal

log = logging.getLogger(__name__)

PATH_KEYS = ("archive_root", "library_root", "state_db")
EXEC_FILE_ONLY = "exec_file_only"

# Serializes every write serve makes to the config file and the install state
# beside it (the edit state).
writing = threading.Lock()


@dataclass(frozen=True)
class Saved:
    revision: str
    warnings: list[dict[str, Any]]


def save(path: Path, revision: str | None, document: Any) -> Saved:
    """Check ``document`` and write it over the file loaded at ``revision``."""
    with writing:
        current = _editable_file(path)
        if revision != current.revision:
            raise Refusal(409, "changed")

        checked = check_document(document)
        errors = (checked.errors + exec_problems(current.raw, document)
                  + path_problems(current.raw, document))
        if errors:
            raise ConfigRejected(errors)
        assert checked.config is not None
        old = document_of(current)
        assert old is not None
        new = checked.config.model_dump(mode="json")

        warnings = _path_warnings(old, new) + [warning_of(w) for w in checked.warnings]
        assert current.data is not None
        merged = merge(current.data.decode(), old, new).encode()
        if merged != current.data:
            if _bytes_now(path) != current.data:
                raise Refusal(409, "changed")
            old_db, new_db = state_db_for(current), Path(new["state_db"])
            if old_db is not None and not _same_db(old_db, new_db):
                _move(path, current.data, merged, old_db, new_db)
            else:
                _replace(path, merged)
        return Saved(revision_of(merged), warnings)


def _replace(path: Path, data: bytes) -> None:
    try:
        atomic_replace(path, data)
    except WriteFailed as e:
        raise Refusal(500, "write_failed", path=str(e.path)) from e


def _move(path: Path, before: bytes, merged: bytes, old_db: Path, new_db: Path) -> None:
    """Save a file that points the config at another state database, and carry the
    edit state there (§spec:edit-lock "Where the state
    lives"). No run may write meanwhile: one holding the old database's lock would
    keep writing while the next took the new one's, two writers on one archive. The
    install state is copied only once the file is written, so a refused write leaves
    the new database as it was; a failed copy puts the file back. Then the old lock
    is retired, so a run that read the old config just before cannot take it."""
    try:
        with (writer_lock_at(old_db, retired_ok=True),
              writer_lock_at(new_db, retired_ok=True)):
            _replace(path, merged)
            try:
                move_install_state(old_db, new_db)
                unretire(new_db)
                retire(old_db)
            except (sqlite3.Error, OSError) as e:
                # Put back only this save's own bytes: a hand edit made meanwhile
                # stays.
                if _bytes_now(path) == merged:
                    try:
                        atomic_replace(path, before)
                    except WriteFailed:
                        log.error("could not put back %s after a failed save", path)
                raise Refusal(500, "write_failed", path=str(new_db)) from e
    except LockHeld:
        raise Refusal(409, "busy") from None
    except OSError as e:  # the lock file beside the new database cannot be made
        raise Refusal(500, "write_failed", path=str(new_db)) from e


def _same_db(a: Path, b: Path) -> bool:
    """Whether two spellings name one database file (a symlink, `..`, a relative
    path): such a save moves nothing."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a.resolve() == b.resolve()


def override(path: Path) -> None:
    """Turn the override on (§spec:edit-lock); idempotent."""
    with writing:
        file = read_config_file(path)
        if file.state in ("missing", "unreadable"):
            raise Refusal(409, file.state)
        state_db = state_db_for(file)
        if state_db is None:
            raise Refusal(409, file.state)
        write_state(state_db, lambda: record_edit(state_db, "override"))


def check_paths(archive_root: Path, library_root: Path, state_db: Path | None) -> list[Any]:
    """The path checks a save runs, without saving."""
    state_db = state_db or default_state_db(archive_root)
    return path_warnings(path_checks(archive_root, library_root, state_db))


def _editable_file(path: Path) -> ConfigFile:
    file = read_config_file(path)
    if file.state in ("missing", "unreadable"):
        # No editor exists for these (§spec:dashboard-settings "A broken file").
        raise Refusal(409, file.state)
    if read_edit(state_db_for(file)) == "locked":
        raise Refusal(409, "locked")
    return file


def _path_warnings(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """`path_moved` for each changed path, then the path checks' failures on the new
    values (§spec:dashboard-settings "Paths warn, never block")."""
    moved = [key for key in PATH_KEYS if old.get(key) != new[key]]
    if not moved:
        return []
    checks = path_checks(
        Path(new["archive_root"]), Path(new["library_root"]), Path(new["state_db"])
    )
    return [{"loc": [key], "code": "path_moved"} for key in moved] + path_warnings(checks)


def warning_of(problem: ConfigProblem) -> dict[str, Any]:
    return {"loc": list(problem.loc), "code": problem.code}


def exec_problems(old_raw: Any, new_raw: Any) -> list[ConfigProblem]:
    """The command handoff stays in the file (§spec:dashboard-settings): a
    collection's `exec` comes back exactly as the file has it, and a new one is never
    set. Removing a whole collection takes its handoff with it."""
    before = {key: command for key, command, _ in _exec_values(old_raw)}
    problems = []
    for key, command, loc in _exec_values(new_raw):
        if command == before.get(key):
            continue
        problems.append(
            ConfigProblem(
                (*loc, "exec") if command is not None else loc,
                EXEC_FILE_ONLY,
                "the command handoff is set in the config file only",
            )
        )
    return problems


def path_problems(old_raw: Any, new_raw: Any) -> list[ConfigProblem]:
    """A library path an `exec` command fills in as `{staging_root}` stays in the
    file too (§spec:dashboard-settings): a command that hands it to a shell would
    run whatever the dashboard saved as the path."""
    if not isinstance(old_raw, dict) or not isinstance(new_raw, dict):
        return []
    if old_raw.get("library_root") == new_raw.get("library_root"):
        return []
    if not any(isinstance(command, str) and "{staging_root}" in command
               for _key, command, _loc in _exec_values(old_raw)):
        return []
    return [ConfigProblem(
        ("library_root",), EXEC_FILE_ONLY,
        "a command handoff uses this path, so it is set in the config file only",
    )]


def _exec_values(raw: Any) -> list[tuple[tuple[Any, Any], Any, tuple[str | int, ...]]]:
    """Per collection: its (source, collection) names, its `exec` command or None,
    and the loc of its `on_complete`."""
    values = []
    for i, source in entries(raw, "sources"):
        for j, collection in entries(source, "collections"):
            handoff = collection.get("on_complete")
            command = handoff.get("exec") if isinstance(handoff, dict) else None
            key = (source.get("name"), collection.get("name"))
            if not all(isinstance(name, str) for name in key):
                continue  # refused by the validation; and never a match below
            values.append((key, command, ("sources", i, "collections", j, "on_complete")))
    return values


def write_state(state_db: Path, write: Any) -> None:
    """Write install state beside the config, answering 500 `write_failed` naming
    the database when it cannot be written."""
    try:
        write()
    except (sqlite3.Error, OSError) as e:
        raise Refusal(500, "write_failed", path=str(state_db)) from e


def _bytes_now(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None
