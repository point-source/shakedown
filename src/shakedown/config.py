"""Configuration loader for shakedown.yaml. Mirrors PRD §11."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)


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
    # Opt-in per-collection fast-path (§spec:incremental-discovery); skips the per-item
    # metadata fetch when the source's cheap change signal is unchanged. Off by default —
    # the correct-by-construction full manifest comparison is what every collection gets
    # unless opted in.
    incremental_discovery: bool = False
    # Opt-in per-collection metadata preservation (§spec:metadata-preservation): when set,
    # the core writes the item's raw source metadata as a `metadata.json` sidecar into the
    # archive item directory and hardlinks it into the library beside the media. Off by
    # default — preservation is context, never coupled to media change detection.
    preserve_source_metadata: bool = False
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
    """Guard `library_layout` templates against fields the plugin can't surface and
    against layouts that cannot distinguish two items (§spec:layout-collision-safety).

    Three checks on every non-passthrough layout, in order of certainty:

    - **Unknown field → hard error.** A placeholder the plugin doesn't expose (the
      first-run `{filename}` footgun) would otherwise raise TemplateError mid-sync.
    - **No plugin field at all → hard error.** A constant template renders every item
      to the same path — a guaranteed total collapse config can prove, so it fails
      fast like every other config fault (§spec:configuration).
    - **No per-item-unique field → warning.** The layout keys only on shared fields
      (e.g. `{year}/{venue}`); two items with the same values collide at staging time.
      Config can't prove an arbitrary *combination* is lossy, so this is a proactive
      heads-up, not a hard error — the runtime collision summary is the correctness
      backstop that fires only on actual loss.
    """
    from shakedown.plugins import registry
    from shakedown.utils.templates import fields_in

    for source in config.sources:
        try:
            plugin_cls = registry.plugin_class(source.type)
        except registry.UnknownPluginError as e:
            raise ConfigError(str(e)) from e
        allowed = set(plugin_cls.template_fields)
        unique = set(plugin_cls.per_item_unique_fields)
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
            if not referenced:
                raise ConfigError(
                    f"{source.name}/{collection.name}: library_layout "
                    f"{collection.library_layout!r} references no source field, so every "
                    f"item renders to the same path; include a per-item field such as "
                    f"{{identifier}}"
                )
            if not (referenced & unique):
                log.warning(
                    "%s/%s: library_layout %r references no per-item-unique field %s; "
                    "two items sharing these fields will collide at staging and the "
                    "loser will be dropped from the library — add a unique component "
                    "such as {identifier} to the leaf",
                    source.name,
                    collection.name,
                    collection.library_layout,
                    sorted(unique) or "(none declared by plugin)",
                )
