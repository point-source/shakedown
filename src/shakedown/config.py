"""Configuration loader for shakedown.yaml. Mirrors PRD §11."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class IAAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_env: str | None = None
    password_env: str | None = None


class WebhookHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: str


class ExecHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exec: str


Handoff = Annotated[WebhookHandoff | ExecHandoff, Field(union_mode="left_to_right")]


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    query: str
    format_filters: list[str] = Field(default_factory=list)
    exclude_filters: list[str] = Field(default_factory=list)
    library_layout: str = "passthrough"
    prune_disappeared: bool = False
    on_complete: Handoff | None = None


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str  # validated against the plugin registry at lookup time (sync.py)
    auth: IAAuth | None = None
    collections: list[CollectionConfig]

    # Per-source shared concurrency budget: the hard ceiling on simultaneous
    # connections Shakedown opens to this source's upstream host, spanning
    # discovery metadata calls and file downloads across all its collections
    # (SPEC §spec:source-budget). Falls back to the global max_concurrent_downloads
    # when unset.
    max_concurrent_requests: int | None = Field(default=None, ge=1)


class FailureNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: str | None = None


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_failure: FailureNotification | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_root: Path
    library_root: Path
    state_db: Path | None = None  # defaults to <archive_root>/.shakedown/state.db

    max_concurrent_downloads: int = Field(default=4, ge=1)
    max_concurrent_collections: int = Field(default=2, ge=1)

    sources: list[SourceConfig]
    notifications: NotificationsConfig | None = None

    @model_validator(mode="after")
    def _default_state_db(self) -> Config:
        if self.state_db is None:
            self.state_db = self.archive_root / ".shakedown" / "state.db"
        return self

    @model_validator(mode="after")
    def _unique_collection_names(self) -> Config:
        seen: set[tuple[str, str]] = set()
        for src in self.sources:
            for coll in src.collections:
                key = (src.name, coll.name)
                if key in seen:
                    raise ValueError(
                        f"duplicate (source, collection) pair: ({src.name!r}, {coll.name!r})"
                    )
                seen.add(key)
        return self


class ConfigError(Exception):
    pass


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse YAML at {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    try:
        config = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config validation failed:\n{e}") from e
    _validate_layouts(config)
    return config


def _validate_layouts(config: Config) -> None:
    """Reject library_layout templates that reference fields the source plugin doesn't surface.

    Catches the common first-run footgun where an example layout uses a placeholder
    like `{filename}` that the plugin doesn't expose, which would otherwise raise
    TemplateError mid-sync.
    """
    from shakedown.plugins import registry
    from shakedown.utils.templates import fields_in

    for source in config.sources:
        try:
            plugin_cls = registry.plugin_class(source.type)
        except registry.UnknownPluginError as e:
            raise ConfigError(str(e)) from e
        allowed = set(plugin_cls.template_fields)
        for collection in source.collections:
            if collection.library_layout == "passthrough":
                continue
            referenced = fields_in(collection.library_layout)
            unknown = referenced - allowed
            if unknown:
                raise ConfigError(
                    f"{source.name}/{collection.name}: library_layout references "
                    f"unknown field(s) {sorted(unknown)!r}; plugin {source.type!r} "
                    f"surfaces {sorted(allowed)!r}"
                )
