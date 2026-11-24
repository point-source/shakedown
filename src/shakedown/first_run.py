"""First-run setup (§spec:first-run-setup): the defaults setup offers, and finishing
it.

Setup writes the same `shakedown.yaml` Settings edits, and only while no file exists:
it never replaces one. The defaults are the paths of the compose example's single
`/data` mount (§spec:deployment), so a new user can accept them as they are.
Finishing checks the document like any save, writes the file (recorded `editable`),
and stores the credentials; `serve` then starts the collection's first sync.

Order: the file first, then the edit state, then the credentials. A file that fails
to write leaves no `editable` record behind to unlock a file written later by hand
(§spec:edit-lock); a state database that fails after the file is written answers
`write_failed` naming it, and the file stays.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shakedown import config_save, credentials
from shakedown.config import (
    ConfigProblem,
    ConfigRejected,
    check_document,
    default_state_db,
)
from shakedown.config_file import ConfigExists, WriteFailed, create_config
from shakedown.credentials import Login
from shakedown.dashboard import iso
from shakedown.dashboard_config import source_types
from shakedown.dashboard_credentials import credential_source, login_of
from shakedown.schedule import staggered_default
from shakedown.validate import path_checks, path_warnings
from shakedown.write_gate import Refusal

DEFAULT_ARCHIVE_ROOT = Path("/data/archive")
DEFAULT_LIBRARY_ROOT = Path("/data/library")


def setup_defaults(now: datetime | None = None) -> dict[str, Any]:
    """`GET /api/setup`: the paths, the source types, and the first collection's
    schedule setup offers. The page fills `timezone` from the browser."""
    return {
        "generated_at": iso(now or datetime.now(UTC)),
        "archive_root": str(DEFAULT_ARCHIVE_ROOT),
        "library_root": str(DEFAULT_LIBRARY_ROOT),
        "state_db": str(default_state_db(DEFAULT_ARCHIVE_ROOT)),
        "source_types": source_types(),
        # No other collection exists yet, so the staggered default's first hour.
        "schedule": staggered_default(None),
    }


SETUP_ONE_COLLECTION = "setup_one_collection"
BODY_KEYS = {"document", "credentials"}


@dataclass(frozen=True)
class Finished:
    revision: str
    warnings: list[dict[str, Any]]
    source: str
    collection: str


def finish(path: Path, body: Any) -> Finished:
    """`POST /api/setup`: write the config at ``path`` from the body's document and
    store its credentials. Raises Refusal (`invalid_body`, `exists`,
    `takes_no_credentials`, `write_failed`) or ConfigRejected; nothing is written for
    either, except a `write_failed` naming the state database, which keeps the file."""
    document, login = _read_body(body)
    with config_save.writing:
        if path.exists():
            raise Refusal(409, "exists")
        checked = check_document(document)
        errors = (
            checked.errors
            + config_save.exec_problems({}, document)
            + _shape_problems(document)
        )
        if errors:
            raise ConfigRejected(errors)
        config = checked.config
        assert config is not None and config.state_db is not None
        state_db = config.state_db
        source = config.sources[0]
        collection = source.collections[0]
        if login is not None:
            credential_source(config, source.name)
        # Every path is new: the checks' failures warn, and nothing moved.
        warnings = path_warnings(
            path_checks(config.archive_root, config.library_root, state_db)
        ) + [config_save.warning_of(w) for w in checked.warnings]
        try:
            revision = create_config(path, document)
        except ConfigExists as e:
            raise Refusal(409, "exists") from e
        except WriteFailed as e:
            raise Refusal(500, "write_failed", path=str(e.path)) from e
        except (sqlite3.Error, OSError) as e:
            raise Refusal(500, "write_failed", path=str(state_db)) from e
        if login is not None:
            config_save.write_state(
                state_db, lambda: credentials.store(state_db, source.name, login)
            )
    return Finished(revision, warnings, source.name, collection.name)


def _read_body(body: Any) -> tuple[Any, Login | None]:
    """The document and the optional credentials. A body of any other shape is one
    code, never the field-by-field detail, which would quote the credentials."""
    if not isinstance(body, dict) or "document" not in body or not set(body) <= BODY_KEYS:
        raise Refusal(422, "invalid_body")
    values = body.get("credentials")
    return body["document"], None if values is None else login_of(values)


def _shape_problems(document: Any) -> list[ConfigProblem]:
    """Setup takes exactly one source holding exactly one collection; more are added
    in Settings. A list of another type is the validation's to report."""
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, list):
        return []
    if len(sources) != 1:
        return [ConfigProblem(("sources",), SETUP_ONE_COLLECTION,
                              "setup takes exactly one source")]
    collections = sources[0].get("collections") if isinstance(sources[0], dict) else None
    if isinstance(collections, list) and len(collections) != 1:
        return [ConfigProblem(("sources", 0, "collections"), SETUP_ONE_COLLECTION,
                              "setup takes exactly one collection")]
    return []
