"""In-memory fake source plugin for testing sync end-to-end without the network."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from shakedown.config import CollectionConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemDescriptor,
    ItemLookup,
    ListedItem,
    SourcePlugin,
    VerifyResult,
)


@dataclass
class FakeFile:
    name: str
    content: bytes
    format: str | None = None
    refused: bool = False
    derived_from: str | None = None

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def md5(self) -> str:
        return hashlib.md5(self.content).hexdigest()


@dataclass
class FakeItem:
    identifier: str
    files: list[FakeFile] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    is_restricted: bool = False
    restriction_reason: str | None = None
    change_signal: str | None = None


class FakePlugin(SourcePlugin):
    type_name = "fake"

    # `identifier` is per-item-unique; `title` is a known-but-shared field, so a layout
    # keyed only on `{title}` is collision-risky (the readiness layout probe fails it).
    template_fields = ("identifier", "title")
    per_item_unique_fields = ("identifier",)

    # Class-level state so tests can mutate the source between sync runs.
    items: ClassVar[dict[str, FakeItem]] = {}
    fetch_count: ClassVar[dict[str, int]] = {}
    describe_count: ClassVar[dict[str, int]] = {}
    # Source names whose enumeration should raise, simulating an unreachable source —
    # exactly as a real source fault would — to exercise the readiness reachability probe.
    unreachable_sources: ClassVar[set[str]] = set()
    # Every lookup_item call as (identifier, timeout).
    lookup_calls: ClassVar[list[tuple[str, float]]] = []
    # Per-identifier lookup replies, in order; the last one repeats. An exception is
    # raised, an ItemLookup returned. Identifiers without an entry answer from `items`.
    lookup_faults: ClassVar[dict[str, list[BaseException | ItemLookup]]] = {}

    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        if self.source_config.name in self.unreachable_sources:
            raise RuntimeError(f"source {self.source_config.name!r} is unreachable")
        for identifier, it in self.items.items():
            yield ListedItem(identifier, it.change_signal)

    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        self.lookup_calls.append((identifier, timeout))
        replies = self.lookup_faults.get(identifier)
        if replies:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, BaseException):
                raise reply
            return reply
        it = self.items[identifier]
        return ItemLookup(
            metadata={**it.metadata, "identifier": it.identifier},
            files=[
                {
                    "name": f.name,
                    "size": f.size,
                    "md5": f.md5,
                    "format": f.format,
                    "refused": f.refused,
                    "derived_from": f.derived_from,
                }
                for f in it.files
            ],
            restriction_reason=it.restriction_reason if it.is_restricted else None,
        )

    def enumerate_items(self, collection: CollectionConfig) -> Iterator[str]:
        if self.source_config.name in self.unreachable_sources:
            raise RuntimeError(f"source {self.source_config.name!r} is unreachable")
        yield from self.items

    def enumerate_with_signals(
        self, collection: CollectionConfig
    ) -> Iterator[tuple[str, str | None]]:
        for identifier, it in self.items.items():
            yield identifier, it.change_signal

    def describe_item(self, identifier: str, collection: CollectionConfig) -> ItemDescriptor | None:
        self.describe_count[identifier] = self.describe_count.get(identifier, 0) + 1
        it = self.items.get(identifier)
        if it is None:
            return None
        files = tuple(ManifestFile(name=f.name, size=f.size, md5=f.md5) for f in it.files)
        return ItemDescriptor(
            identifier=it.identifier,
            manifest=Manifest(files=files),
            metadata={**it.metadata, "identifier": it.identifier},
            is_restricted=it.is_restricted,
            restriction_reason=it.restriction_reason,
        )

    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        for identifier in self.enumerate_items(collection):
            desc = self.describe_item(identifier, collection)
            if desc is not None:
                yield desc

    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        self.fetch_count[item.identifier] = self.fetch_count.get(item.identifier, 0) + 1
        src = self.items[item.identifier]
        dest_dir.mkdir(parents=True, exist_ok=True)
        bytes_total = 0
        for f in src.files:
            p = dest_dir / f.name
            p.write_bytes(f.content)
            bytes_total += f.size
        return FetchResult(success=True, bytes_downloaded=bytes_total)

    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        missing = [f.name for f in item.manifest.files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)


def reset_fake() -> None:
    FakePlugin.items.clear()
    FakePlugin.fetch_count.clear()
    FakePlugin.describe_count.clear()
    FakePlugin.unreachable_sources.clear()
    FakePlugin.lookup_calls.clear()
    FakePlugin.lookup_faults.clear()


def install_fake() -> None:
    """Register FakePlugin and extend SourceConfig to accept type='fake' for tests."""
    from shakedown.config import SourceConfig as _SC
    from shakedown.plugins import registry

    registry.register(FakePlugin)
    # Allow type='fake' through config validation. Tests construct SourceConfig directly,
    # so we don't need to extend the Literal — they bypass the YAML loader.
    _ = _SC  # keep the import; explicit no-op
