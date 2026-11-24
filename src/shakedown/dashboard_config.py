"""`GET /api/config`: the config file as it is now (§spec:dashboard-settings "One
config resource"), with its edit state (§spec:edit-lock).

Read at each request, never shared or cached, so a hand edit shows on the next
refresh. Answering writes nothing: the state database is opened read-only and never
created.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from shakedown.config import (
    NAMED_LISTS,
    Config,
    ConfigFile,
    model_defaults,
    raw_state_db,
    read_config_file,
)
from shakedown.credentials import resolve
from shakedown.dashboard import iso
from shakedown.db import open_read_only
from shakedown.edit_lock import read_edit, state_db_for
from shakedown.plugins import registry
from shakedown.purge import configured_pairs, unconfigured
from shakedown.schedule import next_syncs
from shakedown.utils.templates import FILTER_NAMES


def config_answer(path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    file = read_config_file(path)
    config = file.config
    return {
        "generated_at": iso(now),
        "state": file.state,
        "path": str(path),
        "revision": file.revision,
        "edit": read_edit(state_db_for(file)),
        "document": document_of(file),
        "errors": [e.to_dict() for e in file.errors],
        "credentials": credentials_of(config) if config else [],
        "next_syncs": (
            [
                {"source": source, "collection": collection, "next_at": iso(at)}
                for (source, collection), at in next_syncs(config, now).items()
            ]
            if config
            else []
        ),
        "source_types": source_types(),
        "unconfigured": unconfigured_of(config) if config else [],
    }


def unconfigured_of(config: Config) -> list[dict[str, Any]]:
    """The collections the state database holds files for that the file no longer
    names (§spec:collection-removal). Read-only: a missing database is never
    created, and one this release cannot read yet lists nothing."""
    state_db = config.state_db
    if state_db is None or not state_db.exists():
        return []
    try:
        with closing(open_read_only(state_db)) as conn:
            return unconfigured(conn, configured_pairs(config))
    except sqlite3.Error:
        return []


def document_of(file: ConfigFile) -> dict[str, Any] | None:
    """The file's settings as JSON, keyed as the YAML is, with every key the file
    leaves out at its effective default. An invalid file keeps its values as they
    are, so each error's loc points at one. None when missing or unreadable."""
    if file.config is not None:
        return file.config.model_dump(mode="json")
    if not isinstance(file.raw, dict):
        return None
    doc = _filled(file.raw, Config)
    state_db = raw_state_db(file.raw)
    if doc.get("state_db") is None and state_db is not None:
        doc["state_db"] = str(state_db)
    return doc


def _filled(raw: dict[Any, Any], model: type[BaseModel]) -> dict[str, Any]:
    doc = {str(key): _plain(value) for key, value in raw.items()}
    for name, default in model_defaults(model).items():
        doc.setdefault(name, _plain(default))
    for key, nested in NAMED_LISTS.items():
        if key in model.model_fields and isinstance(raw.get(key), list):
            doc[key] = [
                _filled(item, nested) if isinstance(item, dict) else _plain(item)
                for item in raw[key]
            ]
    return doc


def _plain(value: Any) -> Any:
    """A parsed YAML value as JSON: an unquoted date or timestamp becomes its text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def credentials_of(config: Config) -> list[dict[str, str]]:
    """Per source, where its login comes from (§spec:dashboard-credentials):
    `environment`, `dashboard`, or `none`. Never a value. Reads only."""
    return [
        {"source": source.name, "credentials": resolve(source, config.state_db)[0]}
        for source in config.sources
    ]


def source_types() -> list[dict[str, Any]]:
    """Per installed source plugin: the layout fields and filters its
    `library_layout` accepts, and whether it takes credentials."""
    answer = []
    for type_name in registry.known_types():
        plugin = registry.plugin_class(type_name)
        answer.append(
            {
                "type": type_name,
                "layout_fields": list(plugin.template_fields),
                "layout_filters": list(FILTER_NAMES),
                "takes_credentials": plugin.takes_credentials,
            }
        )
    return answer
