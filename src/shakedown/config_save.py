"""Saving the config from the dashboard (§spec:dashboard-settings "Save is a
whole-document replace, checked first"), the path checks without a save, and the
edit-lock override (§spec:edit-lock).

Each refusal is a `Refusal` with its code (§spec:dashboard "Codes, not copy"). Writes
are serialized within serve; a hand edit landing between the revision check and the
replace is caught by re-reading the file just before it is replaced.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shakedown.config import (
    ConfigFile,
    ConfigProblem,
    check_document,
    default_state_db,
    entries,
    read_config_file,
    revision_of,
)
from shakedown.config_file import WriteFailed, atomic_replace, merge
from shakedown.dashboard_config import document_of
from shakedown.edit_lock import move_install_state, read_edit, record_edit, state_db_for
from shakedown.validate import path_checks, path_warnings
from shakedown.write_gate import Refusal

PATH_KEYS = ("archive_root", "library_root", "state_db")
EXEC_FILE_ONLY = "exec_file_only"

_writing = threading.Lock()


class Rejected(Exception):
    """A document the save refuses: `422 {errors}`, every error found."""

    def __init__(self, errors: list[ConfigProblem]) -> None:
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


@dataclass(frozen=True)
class Saved:
    revision: str
    warnings: list[dict[str, Any]]


def save(path: Path, revision: str | None, document: Any) -> Saved:
    """Check ``document`` and write it over the file loaded at ``revision``."""
    with _writing:
        current = _editable_file(path)
        if revision != current.revision:
            raise Refusal(409, "changed")

        checked = check_document(document)
        errors = checked.errors + _exec_problems(current.raw, document)
        if errors:
            raise Rejected(errors)
        assert checked.config is not None
        old = document_of(current)
        assert old is not None
        new = checked.config.model_dump(mode="json")

        warnings = _path_warnings(old, new) + [_warning(w) for w in checked.warnings]
        if old["state_db"] != new["state_db"]:
            old_db, new_db = state_db_for(current), Path(new["state_db"])
            if old_db is not None:
                _write_state(new_db, lambda: move_install_state(old_db, new_db))

        assert current.data is not None
        merged = merge(current.data.decode(), old, new).encode()
        if merged != current.data:
            if _bytes_now(path) != current.data:
                raise Refusal(409, "changed")
            try:
                atomic_replace(path, merged)
            except WriteFailed as e:
                raise Refusal(500, "write_failed", path=str(e.path)) from e
        return Saved(revision_of(merged), warnings)


def override(path: Path) -> None:
    """Turn the override on (§spec:edit-lock); idempotent."""
    with _writing:
        file = read_config_file(path)
        if file.state in ("missing", "unreadable"):
            raise Refusal(409, file.state)
        state_db = state_db_for(file)
        if state_db is None:
            raise Refusal(409, file.state)
        _write_state(state_db, lambda: record_edit(state_db, "override"))


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


def _warning(problem: ConfigProblem) -> dict[str, Any]:
    return {"loc": list(problem.loc), "code": problem.code}


def _exec_problems(old_raw: Any, new_raw: Any) -> list[ConfigProblem]:
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


def _exec_values(raw: Any) -> list[tuple[tuple[Any, Any], Any, tuple[str | int, ...]]]:
    """Per collection: its (source, collection) names, its `exec` command or None,
    and the loc of its `on_complete`."""
    values = []
    for i, source in entries(raw, "sources"):
        for j, collection in entries(source, "collections"):
            handoff = collection.get("on_complete")
            command = handoff.get("exec") if isinstance(handoff, dict) else None
            key = (source.get("name"), collection.get("name"))
            values.append((key, command, ("sources", i, "collections", j, "on_complete")))
    return values


def _write_state(state_db: Path, write: Any) -> None:
    try:
        write()
    except (sqlite3.Error, OSError) as e:
        raise Refusal(500, "write_failed", path=str(state_db)) from e


def _bytes_now(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None
