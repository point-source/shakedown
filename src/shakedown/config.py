"""Configuration loader for shakedown.yaml. Mirrors PRD §11."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from cronsim import CronSim, CronSimError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

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


# Validation messages never quote the value: config errors reach anyone who can open
# serve's read endpoints (§spec:dashboard-settings `errors`). Each rule of our own
# raises a coded error, so a save answers `{loc, code, message}`.

def _cron_expression(expr: str) -> str:
    """A standard five-field cron expression (§spec:schedules). cronsim also takes a
    sixth, seconds field, which the spec rules out, so the count is checked first."""
    if len(expr.split()) != 5:
        raise PydanticCustomError(
            "schedule_invalid",
            "must be a five-field cron expression "
            "(minute hour day-of-month month day-of-week)",
        )
    try:
        CronSim(expr, datetime(2000, 1, 1))
    except CronSimError as e:
        raise PydanticCustomError(
            "schedule_invalid", "not a valid cron expression: {reason}", {"reason": str(e)}
        ) from None
    return expr


def _zone_name(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise PydanticCustomError(
            "timezone_unknown", "not a known IANA time zone name, such as Europe/Berlin"
        ) from None
    return name


CronExpression = Annotated[str, AfterValidator(_cron_expression)]
ZoneName = Annotated[str, AfterValidator(_zone_name)]


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    query: str
    # Which files to mirror is not configurable: the core keeps the best available and
    # best playable copy plus every non-audio file (§spec:quality-selection).
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
    # Opt-in per-collection tag filling (§spec:tag-filling): fill in missing basic tags
    # (artist, album, date, venue, title) from the source metadata shakedown holds. Only
    # absent tags are filled. Off by default: the archive keeps the source's bytes.
    fill_tags: bool = False
    on_complete: Handoff | None = None
    # When the scheduler starts this collection's sync (§spec:schedules): a five-field
    # cron expression in the config's `timezone`. None is manual only.
    schedule: CronExpression | None = None


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
    # Days a run keeps its per-show error lines (§spec:run-error-retention). At least
    # 30: run history shows the last 30 days with their errors (§spec:run-history).
    keep_run_errors_days: int = Field(default=180, ge=30)

    sources: list[SourceConfig]
    notifications: NotificationsConfig | None = None
    # The clock schedules follow, an IANA name; None is the container's local time
    # (§spec:schedules).
    timezone: ZoneName | None = None

    @model_validator(mode="after")
    def _default_state_db(self) -> Config:
        if self.state_db is None:
            self.state_db = default_state_db(self.archive_root)
        return self


def default_state_db(archive_root: Path) -> Path:
    """Where the state database lives when the file names none."""
    return archive_root / ".shakedown" / "state.db"


Loc = tuple[str | int, ...]


@dataclass(frozen=True)
class ConfigProblem:
    """One error or warning about the config: ``loc`` is the path to the setting as
    keys and indexes, ``code`` a stable machine code, ``message`` developer-facing
    text that never quotes a value from the file (§spec:dashboard-settings)."""
    loc: Loc
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"loc": list(self.loc), "code": self.code, "message": self.message}

    def __str__(self) -> str:
        where = ".".join(str(part) for part in self.loc)
        return f"{where}: {self.message}" if where else self.message


@dataclass(frozen=True)
class Checked:
    """What `check_document` found: the config when there are no errors."""
    config: Config | None
    errors: list[ConfigProblem] = field(default_factory=list)
    warnings: list[ConfigProblem] = field(default_factory=list)


class ConfigError(Exception):
    def __init__(self, message: str, problems: list[ConfigProblem] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class UnknownScopeError(ConfigError):
    """A --source or --collection filter names nothing in the config."""


def check_scope(
    config: Config, source_filter: str | None, collection_filter: str | None
) -> None:
    """Raise UnknownScopeError, listing the configured names, when a filter selects
    nothing. A misspelled name must fail visibly, never sync nothing and report
    success (REQUIREMENTS: scheduled runs are observable)."""
    if source_filter and source_filter not in {s.name for s in config.sources}:
        names = ", ".join(sorted({s.name for s in config.sources}))
        raise UnknownScopeError(
            f"unknown source {source_filter!r}; configured sources: {names}"
        )
    collections = {
        c.name
        for s in config.sources
        if not source_filter or s.name == source_filter
        for c in s.collections
    }
    if collection_filter and collection_filter not in collections:
        where = f" in source {source_filter!r}" if source_filter else ""
        raise UnknownScopeError(
            f"unknown collection {collection_filter!r}{where}; configured collections: "
            f"{', '.join(sorted(collections))}"
        )


ConfigState = Literal["missing", "unreadable", "invalid", "valid"]


@dataclass(frozen=True)
class ConfigFile:
    """The config file as it is now (§spec:dashboard-settings `state`): its bytes and
    their revision token, the parsed document, and what checking it found. ``raw`` is
    the parsed YAML (None when missing or unreadable); ``checked`` is None unless the
    file parsed to a mapping."""
    path: Path
    state: ConfigState
    data: bytes | None = None
    revision: str | None = None
    raw: Any = None
    checked: Checked | None = None
    errors: list[ConfigProblem] = field(default_factory=list)

    @property
    def config(self) -> Config | None:
        return self.checked.config if self.checked else None


def revision_of(data: bytes) -> str:
    """An opaque token for the exact file content."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_config_file(path: Path) -> ConfigFile:
    """Read and check the config file. Never raises for a missing, unreadable, or
    invalid file: each is a state with its errors."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ConfigFile(path, "missing")
    except OSError as e:
        problem = ConfigProblem((), "read_failed", f"cannot read the file: {e.strerror}")
        return ConfigFile(path, "unreadable", errors=[problem])
    revision = revision_of(data)
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as e:
        problem = ConfigProblem((), "yaml_invalid", _yaml_problem(e))
        return ConfigFile(path, "unreadable", data, revision, errors=[problem])
    if not isinstance(raw, dict):
        problem = ConfigProblem(
            (), "root_not_mapping", f"config root must be a mapping, got {type(raw).__name__}"
        )
        return ConfigFile(path, "unreadable", data, revision, raw, errors=[problem])
    checked = check_document(raw)
    state: ConfigState = "invalid" if checked.errors else "valid"
    return ConfigFile(path, state, data, revision, raw, checked, list(checked.errors))


def load(path: Path) -> Config:
    """The config every run starts from; raises ConfigError naming every problem."""
    file = read_config_file(path)
    if file.state == "missing":
        raise ConfigError(f"config file not found: {path}")
    if file.state == "unreadable":
        problem = file.errors[0]
        if problem.code == "yaml_invalid":
            raise ConfigError(f"failed to parse YAML at {path}: {problem.message}", file.errors)
        raise ConfigError(problem.message, file.errors)
    assert file.checked is not None
    if file.checked.errors:
        lines = [_line(file.raw, p) for p in file.checked.errors]
        raise ConfigError("config validation failed:\n" + "\n".join(lines), file.checked.errors)
    config = file.checked.config
    assert config is not None
    for warning in file.checked.warnings:
        log.warning("%s", _line(file.raw, warning))
    return config


def check_document(raw: Any) -> Checked:
    """The one config validation: run startup (`load`) and a dashboard save both call
    it, so a document a save accepts is one the next run accepts (§spec:dashboard-
    settings). Returns every error, not only the first, and the warnings."""
    if not isinstance(raw, dict):
        return Checked(
            None,
            [ConfigProblem((), "root_not_mapping", "config root must be a mapping")],
        )
    config: Config | None = None
    errors: list[ConfigProblem] = []
    warnings: list[ConfigProblem] = []
    try:
        config = Config.model_validate(raw)
    except ValidationError as e:
        errors += _validation_problems(e)
    # The rules the model cannot hold read the document itself, so a file with a
    # model error still has its names and layouts checked.
    _check_names(raw, errors)
    _check_layouts(raw, errors, warnings)
    return Checked(None if errors else config, errors, warnings)


def entries(node: Any, key: str) -> list[tuple[int, dict[str, Any]]]:
    """The mappings in the list ``node[key]``, with their indexes; anything not shaped
    that way is the model's to report."""
    items = node.get(key) if isinstance(node, dict) else None
    if not isinstance(items, list):
        return []
    return [(i, item) for i, item in enumerate(items) if isinstance(item, dict)]


def _line(raw: Any, problem: ConfigProblem) -> str:
    """A problem as the run reports it: its place, the `source/collection` it sits in
    when the document names them, and its message."""
    collection = _collection_name(raw, problem.loc)
    return f"{problem} ({collection})" if collection else str(problem)


def _collection_name(raw: Any, loc: Loc) -> str | None:
    if len(loc) < 4 or loc[0] != "sources" or loc[2] != "collections":
        return None
    try:
        source = raw["sources"][loc[1]]
        name = source["collections"][loc[3]]["name"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(source.get("name"), str) and isinstance(name, str):
        return f"{source['name']}/{name}"
    return None


def _validation_problems(e: ValidationError) -> list[ConfigProblem]:
    return [
        ConfigProblem(tuple(err["loc"]), err["type"], err["msg"])
        for err in e.errors(include_url=False, include_input=False, include_context=False)
    ]


def _yaml_problem(e: yaml.YAMLError) -> str:
    if not isinstance(e, yaml.MarkedYAMLError):
        return type(e).__name__

    # Only where, never the parser's own words: those quote the input (an undefined
    # alias or unknown tag names its text), and the text can be a secret. Where the
    # broken construct starts, then where the parser noticed.
    places = []
    for mark in (e.context_mark, e.problem_mark):
        place = f"line {mark.line + 1}, column {mark.column + 1}" if mark else None
        if place and place not in places:
            places.append(place)
    if not places:
        return "invalid YAML"
    return f"invalid YAML at {places[0]}" + "".join(f" (found at {p})" for p in places[1:])


def _check_names(raw: dict[str, Any], errors: list[ConfigProblem]) -> None:
    """Collection names are unique within their source: the archive is keyed by them."""
    for i, source in entries(raw, "sources"):
        seen: set[str] = set()
        for j, collection in entries(source, "collections"):
            name = collection.get("name")
            if not isinstance(name, str):
                continue
            if name in seen:
                errors.append(
                    ConfigProblem(
                        ("sources", i, "collections", j, "name"),
                        "duplicate_name",
                        "another collection in this source has the same name",
                    )
                )
            seen.add(name)


def _check_layouts(
    raw: dict[str, Any], errors: list[ConfigProblem], warnings: list[ConfigProblem]
) -> None:
    """Guard `library_layout` templates against fields the plugin can't surface and
    against layouts that cannot distinguish two items (§spec:layout-collision-safety).

    Four checks on every non-passthrough layout, in order of certainty:

    - **Unknown filter → error.** A misspelled filter (`|sanitise`) would otherwise
      raise TemplateError mid-sync, or mid-restage after the library was cleared.
    - **Unknown field → error.** A placeholder the plugin doesn't expose (the
      first-run `{filename}` footgun) would otherwise raise TemplateError mid-sync.
    - **No plugin field at all → error.** A constant template renders every item
      to the same path — a guaranteed total collapse config can prove, so it fails
      fast like every other config fault (§spec:configuration).
    - **No per-item-unique field → warning.** The layout keys only on shared fields
      (e.g. `{year}/{venue}`); two items with the same values render the same folder
      and are separated into folders labelled with their identifier at staging time
      (§spec:library-ownership). Config can't prove an arbitrary *combination* clashes,
      so this is a proactive heads-up, not an error.
    """
    from shakedown.plugins import registry
    from shakedown.utils.templates import FILTER_NAMES, fields_in, unknown_filters

    for i, source in entries(raw, "sources"):
        source_type = source.get("type")
        if not isinstance(source_type, str):
            continue
        try:
            plugin_cls = registry.plugin_class(source_type)
        except registry.UnknownPluginError:
            errors.append(
                ConfigProblem(
                    ("sources", i, "type"),
                    "source_type_unknown",
                    f"unknown source type; available: {', '.join(registry.known_types())}",
                )
            )
            continue
        allowed = set(plugin_cls.template_fields)
        unique = set(plugin_cls.per_item_unique_fields)
        for j, collection in entries(source, "collections"):
            layout = collection.get("library_layout", "passthrough")
            if not isinstance(layout, str) or layout == "passthrough":
                continue
            loc: Loc = ("sources", i, "collections", j, "library_layout")
            if unknown_filters(layout):
                errors.append(
                    ConfigProblem(
                        loc,
                        "layout_unknown_filter",
                        "library_layout uses a filter the template engine does not "
                        f"define; valid filters: {', '.join(FILTER_NAMES)}",
                    )
                )
            referenced = fields_in(layout)
            if referenced - allowed:
                errors.append(
                    ConfigProblem(
                        loc,
                        "layout_unknown_field",
                        "library_layout uses a field this source does not provide; it "
                        f"provides: {', '.join(sorted(allowed))}",
                    )
                )
            elif not referenced:
                errors.append(
                    ConfigProblem(
                        loc,
                        "layout_no_field",
                        "library_layout references no source field, so every item "
                        "renders to the same path; include a per-item field such as "
                        "{identifier}",
                    )
                )
            elif not (referenced & unique):
                warnings.append(
                    ConfigProblem(
                        loc,
                        "layout_not_unique",
                        "library_layout references no per-item-unique field "
                        f"{sorted(unique) or '(none declared by plugin)'}; two items "
                        "sharing these fields get folders labelled with their identifier "
                        "— add a unique component such as {identifier} to the leaf",
                    )
                )
