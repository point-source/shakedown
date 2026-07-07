"""Source plugin contract. PRD §7."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest


@dataclass(frozen=True)
class ItemDescriptor:
    """Lightweight descriptor returned by discover().

    Carries enough to:
    - identify the item uniquely (identifier),
    - decide whether we have it (manifest comparison via .manifest),
    - render staging templates (.metadata),
    - flag restricted items without a fetch attempt (.is_restricted/.restriction_reason).
    """
    identifier: str
    manifest: Manifest
    metadata: dict[str, Any] = field(default_factory=dict)
    is_restricted: bool = False
    restriction_reason: str | None = None


@dataclass
class FetchResult:
    """Outcome of fetch() for a single item.

    On failure, plugins classify whether the fault is transient so the core can
    apply bounded retries with backoff (SPEC.md §spec:failure-behavior):
    - `retriable` — a checksum mismatch, truncated download, rate limit, or other
      transient fault worth another attempt. Permanent faults (restricted item,
      malformed metadata) leave it False so the core fails fast.
    - `retry_after` — seconds the source asked us to wait (e.g. an HTTP
      `Retry-After` on a 429). The core honors it in place of its own backoff.
    """
    success: bool
    bytes_downloaded: int
    error: str | None = None
    retriable: bool = False
    retry_after: float | None = None


@dataclass
class VerifyResult:
    """Outcome of cheap verify() for a single item.

    `ok` means: every file the manifest expects is present on disk.
    Plugins MUST NOT hash bytes here — that's `verify --deep`'s job.
    """
    ok: bool
    missing_files: list[str] = field(default_factory=list)


class SourcePlugin(ABC):
    """Abstract source. One instance per (source, plugin) pair."""

    #: Identifier used in `type:` config keys (e.g., "ia").
    type_name: str

    def __init__(self, source_config: SourceConfig) -> None:
        self.source_config = source_config

    @abstractmethod
    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        """Enumerate items currently matching the collection's query."""

    @abstractmethod
    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        """Download files matching filters into dest_dir, verifying source checksums.

        `dest_dir` is a fresh, core-owned temporary directory. The plugin only
        writes the item's files there and reports the outcome; it MUST NOT touch
        the final archive location. The core performs the atomic rename of
        `dest_dir` into the archive on success and sweeps it on failure, so no
        plugin can commit partial state to the archive (SPEC.md §spec:sync-workflow).

        Implementations must be idempotent: re-running on a populated dest_dir
        should verify and skip matching files (the IA library does this natively).
        """

    @abstractmethod
    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        """Cheap re-check: every expected file present on disk."""

    # Optional: declared metadata fields available for library_layout templates.
    #: subclass may override to advertise template variables it surfaces.
    template_fields: tuple[str, ...] = ()
