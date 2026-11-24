"""`PUT /api/credentials` and `DELETE /api/credentials` (§spec:dashboard-credentials
"Write-only"): store or forget a source's credentials, and answer the source's
verdict on a stored login.

Values travel only in the request body and never come back: an answer carries the
source, its status, and the verdict. A body that cannot be read answers one code,
never the field-by-field validation detail, which would quote what was entered. Not
under the edit lock: credentials are not in the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from shakedown import config_save
from shakedown.config import Config, SourceConfig
from shakedown.credentials import Login, forget, resolve, store
from shakedown.plugins import registry
from shakedown.write_gate import Refusal

FIELDS = {"source", "email", "password"}


def put_credentials(config: Config, body: Any) -> dict[str, Any]:
    """Store the body's login for its source, then ask the source about it."""
    name, login = _login_of(body)
    source = _credential_source(config, name)
    state_db = _state_db(config)
    with config_save.writing:
        config_save.write_state(state_db, lambda: store(state_db, name, login))
    # Asked after storing and outside the lock: a slow or unreachable source never
    # holds up a save, and a rejected login stays stored for a retry.
    check = registry.for_source(source, login).check_login()
    return {"source": name, "credentials": resolve(source, state_db)[0], "check": check}


def delete_credentials(config: Config, name: str) -> dict[str, Any]:
    """Forget what is stored for the source."""
    source = _credential_source(config, name)
    state_db = _state_db(config)
    with config_save.writing:
        config_save.write_state(state_db, lambda: forget(state_db, name))
    return {"source": name, "credentials": resolve(source, state_db)[0]}


def _login_of(body: Any) -> tuple[str, Login]:
    if not isinstance(body, dict) or set(body) != FIELDS:
        raise Refusal(422, "invalid_body")
    values = {key: body[key] for key in FIELDS}
    if not all(isinstance(v, str) and v.strip() for v in values.values()):
        raise Refusal(422, "invalid_body")
    # Trimmed: a line ending pasted with a value would break the login.
    return values["source"], Login(values["email"].strip(), values["password"].strip())


def _credential_source(config: Config, name: str) -> SourceConfig:
    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        raise Refusal(404, "unknown_source")
    if not registry.plugin_class(source.type).takes_credentials:
        raise Refusal(404, "takes_no_credentials")
    return source


def _state_db(config: Config) -> Path:
    assert config.state_db is not None  # filled in by Config validation
    return config.state_db

