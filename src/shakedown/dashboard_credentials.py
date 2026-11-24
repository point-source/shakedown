"""`PUT /api/credentials` and `DELETE /api/credentials` (§spec:dashboard-credentials
"Write-only"): store or forget a source's credentials, and answer the source's
verdict on a stored login.

Values travel only in the request body and never come back: an answer carries the
source, its status, and the verdict. A body that cannot be read answers one code,
never the field-by-field validation detail, which would quote what was entered. Not
under the edit lock: credentials are not in the file.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from shakedown import config_save
from shakedown.config import Config, SourceConfig
from shakedown.credentials import Login, forget, resolve, store
from shakedown.plugins import registry
from shakedown.write_gate import Refusal

LOGIN_FIELDS = {"email", "password"}


def put_credentials(load: Callable[[], Config], body: Any) -> dict[str, Any]:
    """Store the body's login for its source, then ask the source about it. The
    config is read under the lock a save holds, so a save that removes the source or
    moves the database cannot land between the lookup and the store."""
    name, login = _login_of(body)
    with config_save.writing:
        source, state_db = _target(load(), name)
        config_save.write_state(state_db, lambda: store(state_db, name, login))
    # Asked after storing and outside the lock: a slow or unreachable source never
    # holds up a save, and a rejected login stays stored for a retry.
    check = registry.for_source(source, login).check_login()
    return {"source": name, "credentials": resolve(source, state_db)[0], "check": check}


def delete_credentials(load: Callable[[], Config], name: str) -> dict[str, Any]:
    """Forget what is stored for the source."""
    with config_save.writing:
        source, state_db = _target(load(), name)
        config_save.write_state(state_db, lambda: forget(state_db, name))
        return {"source": name, "credentials": resolve(source, state_db)[0]}


def _target(config: Config, name: str) -> tuple[SourceConfig, Path]:
    source = credential_source(config, name)
    assert config.state_db is not None  # filled in by Config validation
    return source, config.state_db


def _login_of(body: Any) -> tuple[str, Login]:
    if not isinstance(body, dict) or set(body) != {"source", *LOGIN_FIELDS}:
        raise Refusal(422, "invalid_body")
    source = body["source"]
    if not (isinstance(source, str) and source.strip()):
        raise Refusal(422, "invalid_body")
    return source, login_of({key: body[key] for key in LOGIN_FIELDS})


def login_of(values: Any) -> Login:
    """Exactly `{email, password}`, both non-empty text; else 422 `invalid_body`,
    never the field-by-field detail, which would quote what was entered. Setup takes
    the same pair (§spec:first-run-setup)."""
    if not isinstance(values, dict) or set(values) != LOGIN_FIELDS:
        raise Refusal(422, "invalid_body")
    if not all(isinstance(v, str) and v.strip() for v in values.values()):
        raise Refusal(422, "invalid_body")
    # Trimmed: a line ending pasted with a value would break the login.
    return Login(values["email"].strip(), values["password"].strip())


def credential_source(config: Config, name: str) -> SourceConfig:
    """The configured source ``name``, when its type takes credentials."""
    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        raise Refusal(404, "unknown_source")
    if not registry.plugin_class(source.type).takes_credentials:
        raise Refusal(404, "takes_no_credentials")
    return source
