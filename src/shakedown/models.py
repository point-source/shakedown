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


@dataclass(frozen=True)
class ManifestFile:
    """One file in a source's manifest. Identity-bearing fields only."""
    name: str
    size: int | None
    md5: str | None

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "md5": self.md5}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ManifestFile:
        return cls(name=raw["name"], size=raw.get("size"), md5=raw.get("md5"))


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


@dataclass
class Run:
    """A single sync invocation against a (source, collection)."""
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
    # Recordings dropped this run because their library_layout rendered to a path
    # another item already staged (§spec:layout-collision-safety). Distinct from
    # items_failed: the archive copy is intact, only the library link was dropped.
    collisions_dropped: int = 0
    collision_paths: list[str] = field(default_factory=list)


@dataclass
class OperationOutcome:
    """Latest actionable outcome for a user-visible operation."""
    source_name: str
    collection_name: str
    operation: str
    status: str
    phase: str
    message: str
    next_action: str
    started_at: datetime
    finished_at: datetime | None = None
    item_identifier: str | None = None
