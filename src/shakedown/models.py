"""Core data model. Mirrors PRD §6."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class ItemStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    DISAPPEARED = "disappeared"
    PRUNED = "pruned"


class OperationType(StrEnum):
    SYNC = "sync"
    RESTAGE = "restage"
    RECONCILE = "reconcile"
    VALIDATE = "validate"
    REFETCH = "refetch"
    RECONFORM = "reconform"


class RunKind(StrEnum):
    """What a run did: a sync of the whole collection, or a single-show run that
    fetched only the shows it acts on (§spec:single-show-runs)."""
    SYNC = "sync"
    REFETCH = "refetch"
    RECONFORM = "reconform"


class OperationStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_ITEM_ISSUES = "completed_with_item_issues"
    FAILED_BEFORE_COMPLETION = "failed_before_completion"
    STALE_ENUMERATION = "stale_enumeration"


@dataclass(frozen=True)
class ManifestFile:
    """One file in a source's manifest: the identity-bearing fields (name, size, md5)
    plus whether the library stages it (§spec:quality-selection). `library` is not
    part of the identity: an archive-only copy is still a copy the archive holds."""
    name: str
    size: int | None
    md5: str | None
    library: bool = True
    # The track title and number the source lists for this file, when it lists them:
    # the source's word, used to fill tags (§spec:tag-filling), never identity.
    title: str | None = None
    track: str | None = None

    def to_json(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"name": self.name, "size": self.size, "md5": self.md5}
        if not self.library:
            raw["library"] = False
        if self.title is not None:
            raw["title"] = self.title
        if self.track is not None:
            raw["track"] = self.track
        return raw

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ManifestFile:
        # Records written before the quality rule carry no flag: every file is staged.
        return cls(
            name=raw["name"],
            size=raw.get("size"),
            md5=raw.get("md5"),
            library=raw.get("library", True),
            title=raw.get("title"),
            track=raw.get("track"),
        )


@dataclass(frozen=True)
class SourceFile:
    """One file a source lists for an item, as validated by the core
    (shakedown.discovery.validate_lookup); file selection chooses among these."""
    name: str
    size: int | None
    md5: str | None
    format: str | None = None
    refused: bool = False  # the source will not serve this file
    derived_from: str | None = None  # the original file, for a derivative
    bitrate: int | None = None  # kbps, when the source lists it
    title: str | None = None  # the file's track title, when the source lists it
    track: str | None = None  # the file's track number, as the source writes it


@dataclass(frozen=True)
class Manifest:
    """Source's reported file list for an item, as recorded at fetch time.

    Identity for change detection: equality of the (name, size, md5) tuple set.
    """
    files: tuple[ManifestFile, ...]

    def fingerprint(self) -> frozenset[tuple[str, int | None, str | None]]:
        return frozenset((f.name, f.size, f.md5) for f in self.files)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Manifest) and self.fingerprint() == other.fingerprint()

    def __hash__(self) -> int:
        return hash(self.fingerprint())

    def to_json(self) -> list[dict[str, Any]]:
        return [f.to_json() for f in self.files]

    @classmethod
    def from_json(cls, raw: list[dict[str, Any]]) -> Manifest:
        return cls(files=tuple(ManifestFile.from_json(f) for f in raw))


@dataclass
class Item:
    """An item in a source collection."""
    source_name: str
    collection_name: str
    identifier: str
    status: ItemStatus
    archive_path: Path | None = None
    discovered_at: datetime | None = None
    downloaded_at: datetime | None = None
    last_verified_at: datetime | None = None
    restriction_reason: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    recorded_manifest: Manifest | None = None
    # last-seen source change signal for incremental discovery (§spec:incremental-discovery)
    change_signal: str | None = None
    # set when the show has no copy a common player reads (§spec:quality-selection)
    not_playable_reason: str | None = None
    # when the show was first found missing from a complete listing; cleared when it
    # is listed again (§spec:item-lifecycle)
    disappeared_at: datetime | None = None


@dataclass
class Run:
    """One run against a (source, collection): a sync, or a single-show run."""
    id: int | None
    source_name: str
    collection_name: str
    started_at: datetime
    finished_at: datetime | None = None
    items_discovered: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)
    stale: bool = False  # source enumeration failed; existing items retained
    # Why this run's failure alert was not delivered; not resent (§spec:durable-handoff).
    failure_notice_error: str | None = None
    kind: RunKind = RunKind.SYNC


@dataclass
class OperationOutcome:
    """Durable user-facing recovery state for one operation scope."""
    id: int | None
    operation: OperationType
    status: OperationStatus
    source_name: str
    collection_name: str
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    phase: str | None = None
    affected_item: str | None = None
    affected_path: str | None = None
    completed_work: dict[str, Any] = field(default_factory=dict)
    preservation_context: str | None = None
    deletion_context: str | None = None
    safe_next_action: str | None = None
    errors: list[str] = field(default_factory=list)
    resolved_at: datetime | None = None
