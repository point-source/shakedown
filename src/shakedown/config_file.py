"""Writing `shakedown.yaml` (§spec:dashboard-settings "Hand edits survive").

A save changes only the settings that changed: the file is loaded in ruamel.yaml's
round-trip mode, which keeps comments, key order, quoting, and blank lines, and only
the nodes whose effective value differs are touched. Values are never read through
ruamel: what changed is decided by comparing the run's own view of the file (PyYAML
plus `check_document`) with the submitted document, and the merged text is checked
the same way before it is written. The new content replaces the file atomically and
keeps its permissions.
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import ScalarString

from shakedown.config import (
    NAMED_LISTS,
    Config,
    ConfigRejected,
    check_document,
    model_defaults,
    revision_of,
)
from shakedown.edit_lock import record_edit

# Indent styles tried in turn; the first that reproduces the file unchanged wins.
# (2, 4, 2) is the example file's: mappings by 2, list items by 4 with the dash at 2.
_INDENTS = ((2, 4, 2), (2, 2, 0), (4, 4, 2), (4, 6, 4), (4, 4, 0))
_NEW_FILE_INDENT = (2, 4, 2)


class WriteFailed(Exception):
    """The file or its folder cannot be written; the file is untouched."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot write {path}: {reason}")
        self.path = path


class ConfigExists(Exception):
    """A new config was asked for where a file already is."""


class MergeFailed(Exception):
    """The merged file does not read back as the submitted document: a bug, never
    written."""


def _yaml(indent: tuple[int, int, int]) -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 2**16  # never re-wrap a long line, such as a query
    mapping, sequence, offset = indent
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    return y


def _dump(y: YAML, data: Any) -> str:
    out = io.StringIO()
    y.dump(data, out)
    return out.getvalue()


def _round_trip(text: str) -> tuple[YAML, Any]:
    """The file loaded for editing, with the indent style that writes it back as it
    is (else the first style)."""
    for indent in _INDENTS:
        y = _yaml(indent)
        data = y.load(text)
        if _dump(y, data) == text:
            return y, data
    y = _yaml(_INDENTS[0])
    return y, y.load(text)


def merge(text: str, old: dict[str, Any], new: dict[str, Any]) -> str:
    """``text`` with the settings that differ between ``old`` (the file's effective
    document) and ``new`` (the submitted one) changed, everything else as it was.
    Both are `Config.model_dump(mode="json")`."""
    y, data = _round_trip(text)
    _merge_map(data, old, new, Config)
    merged = _dump(y, data)
    effective = _effective(merged)
    if effective is not None and effective["state_db"] != new["state_db"]:
        # A file without `state_db` keeps its database beside `archive_root`; when the
        # archive moves but the database does not, write the database down.
        _insert_after(data, "library_root", "state_db", new["state_db"])
        merged = _dump(y, data)
        effective = _effective(merged)
    if effective != new:
        raise MergeFailed("the merged config does not read back as the saved document")
    return merged


def _effective(text: str) -> dict[str, Any] | None:
    checked = check_document(yaml.safe_load(text))
    return checked.config.model_dump(mode="json") if checked.config else None


def _merge_map(
    node: CommentedMap, old: dict[str, Any], new: dict[str, Any], model: type[BaseModel] | None
) -> None:
    for key in [k for k in node if k not in new]:
        del node[key]  # e.g. a handoff switched from one kind to the other
    defaults = model_defaults(model)
    for key, value in new.items():
        before = old.get(key, defaults.get(key))
        if key in node:
            if value is None:
                del node[key]
            elif value != before:
                node[key] = _merge_value(node[key], before, value, key)
        elif value != before and value is not None:
            node[key] = _fresh(value, NAMED_LISTS.get(key))


def _merge_value(node: Any, old: Any, new: Any, key: str) -> Any:
    if isinstance(node, CommentedMap) and isinstance(old, dict) and isinstance(new, dict):
        _merge_map(node, old, new, None)
        return node
    if key in NAMED_LISTS and isinstance(node, CommentedSeq) and isinstance(new, list):
        _merge_named(node, old if isinstance(old, list) else [], new, NAMED_LISTS[key])
        return node
    if isinstance(node, ScalarString) and isinstance(new, str):
        return type(node)(new)  # keep the value's quoting
    return _fresh(new, None)


def _merge_named(
    seq: CommentedSeq, old: list[dict[str, Any]], new: list[dict[str, Any]],
    model: type[BaseModel],
) -> None:
    """Entries matched by name: changed in place, removed, added, then put in the
    document's order. A name the list repeats (one source split over two entries)
    is matched by its occurrence."""
    old_by_key = dict(zip(_keys(old), old, strict=True))
    node_by_key = dict(zip(_keys(list(seq)), list(seq), strict=True))
    items = []
    for key, entry in zip(_keys(new), new, strict=True):
        node = node_by_key.get(key)
        if node is None:
            node = _fresh(entry, model)
        else:
            _merge_map(node, old_by_key.get(key, {}), entry, model)
        items.append(node)
    kept = {id(node) for node in items}
    for i in reversed(range(len(seq))):
        if id(seq[i]) not in kept:
            del seq[i]
    present = {id(node) for node in seq}
    seq.extend(node for node in items if id(node) not in present)
    if [id(node) for node in seq] != [id(node) for node in items]:
        seq.clear()
        seq.extend(items)


def _keys(entries: list[Any]) -> list[tuple[Any, int]]:
    """Each entry's name with its occurrence: ("ia", 0), ("etree", 0), ("ia", 1)."""
    seen: dict[Any, int] = {}
    keys = []
    for entry in entries:
        name = _name(entry)
        keys.append((name, seen.get(name, 0)))
        seen[name] = seen.get(name, 0) + 1
    return keys


def _name(item: Any) -> Any:
    return item.get("name") if isinstance(item, dict) else None


def _fresh(value: Any, model: type[BaseModel] | None) -> Any:
    """A new node for ``value``. A new source or collection is written with only
    what differs from the defaults, as a person would write it."""
    if isinstance(value, dict):
        defaults = model_defaults(model)
        node = CommentedMap()
        for key, item in value.items():
            if item is None or (key in defaults and item == defaults[key]):
                continue
            node[key] = _fresh(item, NAMED_LISTS.get(key))
        return node
    if isinstance(value, list):
        return CommentedSeq(_fresh(item, model) for item in value)
    return value


def _insert_after(node: CommentedMap, after: str, key: str, value: Any) -> None:
    if key in node:
        node[key] = value
        return
    keys = list(node)
    node.insert(keys.index(after) + 1 if after in keys else len(keys), key, value)


def atomic_replace(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so no reader, run, or crash sees a half-written
    file, keeping the file's permissions. Raises WriteFailed; the file is untouched
    then."""
    tmp: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
        _sync_folder(path.parent)
    except OSError as e:
        raise WriteFailed(path, e.strerror or type(e).__name__) from e
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _sync_folder(folder: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def create_config(path: Path, document: dict[str, Any]) -> str:
    """Write a brand-new config file from ``document`` and record it `editable`
    (§spec:edit-lock: a file Shakedown's setup wrote is editable from the start).
    Never replaces a file: raises ConfigExists. Raises ConfigRejected for a document
    the validation refuses, WriteFailed when the file cannot be written, and
    sqlite3.Error when the edit state cannot be recorded. Returns the revision."""
    checked = check_document(document)
    if checked.config is None:
        raise ConfigRejected(checked.errors)
    data = _dump(_yaml(_NEW_FILE_INDENT), _fresh(document, None)).encode()
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.urandom(6).hex()}.tmp"
        # Created like any new file, so the umask sets its permissions.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # fails when a file is already there: never replaces one
        _sync_folder(path.parent)
    except FileExistsError as e:
        raise ConfigExists(str(path)) from e
    except OSError as e:
        raise WriteFailed(path, e.strerror or type(e).__name__) from e
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
    assert checked.config.state_db is not None
    record_edit(checked.config.state_db, "editable")
    return revision_of(data)
