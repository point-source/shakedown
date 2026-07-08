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
    # source-provided change signal for incremental discovery (§spec:incremental-discovery);
    # a token that moves whenever the item's contents change. None when the source exposes
    # no cheap signal.
    change_signal: str | None = None


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

    def enumerate_items(self, collection: CollectionConfig) -> Iterator[str] | None:
        """Cheaply list item identifiers so the core can fan `describe_item()` across
        the shared per-source budget (SPEC §spec:discovery-pipeline). Return None
        (the default) to signal this source has no cheap enumerate/describe split;
        the core then drains `discover()` serially. Raising signals an enumeration
        failure (the core marks the collection stale), exactly like `discover()`.
        """
        return None

    def enumerate_with_signals(
        self, collection: CollectionConfig
    ) -> Iterator[tuple[str, str | None]] | None:
        """Cheap enumeration paired with each item's source change signal
        (§spec:incremental-discovery): the same single search as enumerate_items,
        but also yielding the change signal per item so the core can skip the
        per-item metadata fetch when the stored signal is unchanged. Return None
        (the default) when the source has no cheap change signal; the core then
        always resolves the full descriptor via describe_item(). Raising signals an
        enumeration failure, exactly like enumerate_items()."""
        return None

    def describe_item(
        self, identifier: str, collection: CollectionConfig
    ) -> ItemDescriptor | None:
        """Resolve one enumerated identifier to a descriptor — the expensive per-item
        metadata fetch. Only called when `enumerate_items()` returns an iterator.
        Return None to skip the item (a transient fault already handled). Sources that
        do not support the enumerate/describe split need not implement this.
        """
        raise NotImplementedError

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

    # Subset of ``template_fields`` guaranteed to distinguish any two items in a
    # collection — the source's per-item identity fields. The config layer uses this
    # to guard against lossy `library_layout` templates that would render two items to
    # the same staging path (§spec:layout-collision-safety). Empty means the plugin
    # vouches for no field's uniqueness.
    per_item_unique_fields: tuple[str, ...] = ()
